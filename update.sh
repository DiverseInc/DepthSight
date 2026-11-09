#!/bin/bash

# --- DepthSight Auto-Updater ---
# This script pulls the latest changes from the repository,
# rebuilds the necessary containers, and applies database migrations.

set -e

# Colors for UI
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}------------------------------------------------"
echo "    ____             __  __   _____ _       __    __ "
echo "   / __ \___  ____  / /_/ /_ / ___/(_)___ _/ /_  / /_"
echo "  / / / / _ \/ __ \/ __/ __ \\\\__ \/ / __ \`/ __ \/ __/"
echo " / /_/ /  __/ /_/ / /_/ / / /__/ / / /_/ / / / / /_  "
echo "/_____/\___/ .___/\__/_/ /_/____/_/\__, /_/ /_/\__/  "
echo "          /_/                     /____/             "
echo -e "------------------------------------------------${NC}"
echo -e "${GREEN}[*] Starting DepthSight Update Process...${NC}"

# 1. Root Check
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[!] Please run as root (use sudo).${NC}"
  exit 1
fi

# 2. Determine Project Directory
if [ -f "docker-compose.yml" ] && [ -d ".git" ]; then
    PROJECT_DIR=$(pwd)
else
    PROJECT_DIR="/opt/app/depthsight"
fi

if [ ! -d "$PROJECT_DIR" ]; then
    echo -e "${RED}[!] Project directory not found at $PROJECT_DIR.${NC}"
    exit 1
fi

cd "$PROJECT_DIR"

# 3. Pull Latest Code
echo -e "${BLUE}[*] Fetching latest updates from GitHub...${NC}"
git fetch origin main
# Reset hard to match the remote (Warning: overwrites local code changes, but keeps .env and data intact)
git reset --hard origin/main

# Self-healing CRLF just in case
find . -type f -name "*.sh" -exec sed -i 's/\r$//' {} +
find . -type f -name "Caddyfile" -exec sed -i 's/\r$//' {} +

# 3b. Record the commit this deployment is built from.
#
# The AGPL-3.0 section 13 offer has to point at the source of the RUNNING
# version, and that version's commit has to be locatable from a public URL
# with no human in the loop. .env is what docker-compose.yml hands to the
# api service as env_file, so the value written there travels WITH the
# container and comes back out of GET /api/v1/status as the `commit` field.
#
# It is written BEFORE the build on purpose: if the build fails, no new
# container is created, the old container keeps running and keeps reporting
# the OLD commit -- which is the truth. Writing it after the build would
# leave .env claiming a commit that is not what is serving.
#
# The logic lives in ONE place -- ops/scripts/record_deployed_commit.sh --
# because the manual deploy in AGENTS.md must do exactly the same thing. It
# also refuses a checkout that is not the committed tree: the image is built
# from the Docker build context, so a checkout with local changes would put
# code in the running containers that is not in the published source.
bash ops/scripts/record_deployed_commit.sh

# 4. Rebuild and Restart
echo -e "${BLUE}[*] Rebuilding and restarting containers (this may take a few minutes)...${NC}"
# Determine if Bitcart is running
if [ -f "docker-compose.bitcart.yml" ] && docker compose ps | grep -q bitcart; then
    docker compose -f docker-compose.yml -f docker-compose.bitcart.yml up -d --build
else
    docker compose up -d --build
fi

# 4b. The AGPL-3.0 section 13 source offer must be reachable after a deploy.
#
# Caddy reads its Caddyfile only at startup, and `docker compose up -d` does
# NOT recreate a container whose bind-mounted file changed (AGENTS.md:97-98).
# A deploy that adds a route therefore leaves the path on the React SPA
# catch-all, which answers 200 with the app shell instead of the offer, so
# the BODY is checked and not just the status. If the offer is missing,
# caddy is restarted -- that is what loads the new Caddyfile -- and the
# check is repeated.
echo -e "${BLUE}[*] Verifying the AGPL source offer is reachable...${NC}"
PUBLIC_BASE_URL=""
if [ -f .env ]; then
    PUBLIC_BASE_URL=$(grep -E '^PUBLIC_BASE_URL=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
fi
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://depthsight.diverseinc.net}"

source_offer_is_live() {
    curl -sk --max-time 15 "$PUBLIC_BASE_URL/source" 2>/dev/null | grep -q "DiverseInc/DepthSight"
}

# The rest of the site has to answer too. A caddy restart that leaves the
# whole site down must not be reported as a successful deploy, so the root
# is checked after the restart -- with a BOUNDED number of attempts, and a
# loud failure when they are used up.
site_root_is_live() {
    curl -sk --max-time 15 -o /dev/null -w "%{http_code}" "$PUBLIC_BASE_URL/" 2>/dev/null | grep -qE "^[23][0-9][0-9]$"
}

OFFER_LIVE=0
for _ in 1 2 3; do
    if source_offer_is_live; then
        OFFER_LIVE=1
        break
    fi
    sleep 5
done

if [ "$OFFER_LIVE" -eq 0 ]; then
    echo -e "${BLUE}[*] The source offer is not being served; restarting caddy...${NC}"
    docker compose restart caddy
    sleep 3
    if ! source_offer_is_live; then
        echo -e "${RED}[!] The AGPL source offer is NOT reachable at $PUBLIC_BASE_URL/source${NC}"
        echo -e "${RED}[!] Check 'docker compose logs caddy | tail -50'${NC}"
        exit 1
    fi
    # The restart just bounced the proxy in front of the WHOLE site, so the
    # rest of the service has to answer too. Bounded: three attempts, then a
    # loud failure. A failed check never carries on as a success.
    SITE_ROOT_LIVE=0
    for _ in 1 2 3; do
        if site_root_is_live; then
            SITE_ROOT_LIVE=1
            break
        fi
        sleep 5
    done
    if [ "$SITE_ROOT_LIVE" -eq 0 ]; then
        echo -e "${RED}[!] The site root is NOT answering at $PUBLIC_BASE_URL/ after the caddy restart${NC}"
        echo -e "${RED}[!] Check 'docker compose logs caddy | tail -50' and 'docker compose ps'${NC}"
        exit 1
    fi
fi
echo -e "${GREEN}[+] Source offer is live at $PUBLIC_BASE_URL/source${NC}"

# 5. Cleanup
echo -e "${BLUE}[*] Cleaning up old unused Docker images to free up space...${NC}"
docker image prune -f > /dev/null 2>&1

echo -e "${GREEN}------------------------------------------------"
echo "[+] UPDATE COMPLETE! All services are running the latest version."
echo "------------------------------------------------${NC}"
