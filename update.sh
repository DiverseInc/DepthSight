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
    PROJECT_DIR="/opt/depthsight"
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
fi
echo -e "${GREEN}[+] Source offer is live at $PUBLIC_BASE_URL/source${NC}"

# 5. Cleanup
echo -e "${BLUE}[*] Cleaning up old unused Docker images to free up space...${NC}"
docker image prune -f > /dev/null 2>&1

echo -e "${GREEN}------------------------------------------------"
echo "[+] UPDATE COMPLETE! All services are running the latest version."
echo "------------------------------------------------${NC}"
