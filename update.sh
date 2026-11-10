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
#
# The one-click installer (deploy.sh) clones the project into /opt/depthsight
# and its cron runs THIS script with a working directory that is NOT a git
# checkout -- so the fallback below is the path the auto-update actually
# takes, and it has to work on BOTH kinds of host.
#
# /opt/app/depthsight is where the maintainer's host keeps the project;
# /opt/depthsight is where the one-click installer puts it. Accepting only
# one of them means the auto-update is DEAD on the other one: the cron runs
# the script, the script cannot find the project, and nothing updates any
# more. So both are accepted, in that order, and if neither exists the
# script stops and says BOTH paths it looked at.
resolve_project_dir() {
    local candidate
    for candidate in "$@"; do
        if [ -d "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    echo "[!] Project directory not found. Looked for: $*" >&2
    return 1
}

if [ -f "docker-compose.yml" ] && [ -d ".git" ]; then
    PROJECT_DIR=$(pwd)
else
    PROJECT_DIR=$(resolve_project_dir /opt/app/depthsight /opt/depthsight) || exit 1
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

# 4a. The api container is RE-CREATED, not merely left running.
#
# docker-compose.yml gives the api service `env_file: .env`, and a
# container's environment is fixed when the container is CREATED --
# `docker compose up -d --build` does not re-create a container whose image
# id did not change (a bot-only change, or a build the cache reused). The
# api would then keep publishing the PREVIOUS DEPLOYED_COMMIT while the
# rest of the deployment already runs the new code, and the archive the
# AGPL offer names would not contain the running code.
#
# --force-recreate builds nothing: it re-creates the container so that it
# picks up the stamp that was just written into .env.
docker compose up -d --no-deps --force-recreate api

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
    curl -s --max-time 15 "$PUBLIC_BASE_URL/source" 2>/dev/null | grep -q "DiverseInc/DepthSight"
}

# The offer is a PUBLIC document, so it has to be served over a certificate
# that verifies. `curl -k` (used for the site-root liveness check below)
# skips verification, so it cannot tell the real offer from one served
# through an impersonated certificate. This check verifies, and it reports
# its OWN failure, so a broken certificate is never reported to the operator
# as "the offer is missing" -- which would send them looking in the wrong
# place, or make them restart caddy for no reason.
if ! curl -s --max-time 15 -o /dev/null "$PUBLIC_BASE_URL/source" 2>/dev/null; then
    echo -e "${RED}[!] The TLS certificate at $PUBLIC_BASE_URL did not verify${NC}"
    echo -e "${RED}[!] The source offer cannot be served publicly over an unverified certificate${NC}"
    echo -e "${RED}[!] Check the certificate, and check that this host has a CA bundle (ca-certificates)${NC}"
    exit 1
fi

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

# 4c. The offer PROMISES a downloadable archive of the exact tree of the
# running build -- and nothing has ever FETCHED that archive. Every gate
# above only proved that the PAGE is served: a 404 archive, an empty
# archive, or an archive of a commit that does not exist all passed.
#
# The commit is the STAMPED one -- the same DEPLOYED_COMMIT the api
# publishes as the `commit` field of GET /api/v1/status, and the same one
# the offer turns into the archive URL. A deployment from before the stamp
# existed has no commit to check, so the check is SKIPPED with a clear
# message (there is nothing to check, not a failure). A stamped commit
# whose archive does not resolve is a LOUD failure: the offer is promising
# a file that is not there.
source_archive_is_available() {
    local commit url code tmp stamp_key
    commit=""
    # The .env key the stamp script writes. update.sh READS the stamp here
    # (to fetch the archive the offer names) and never WRITES it: the
    # stamping lives in ops/scripts/record_deployed_commit.sh, and nowhere
    # else.
    stamp_key="DEPLOYED_COMMIT"
    if [ -f .env ]; then
        commit=$(grep -E "^${stamp_key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | tr -d '"' | tr -d "'" || true)
    fi
    if [ -z "$commit" ]; then
        echo "SKIP: no DEPLOYED_COMMIT in .env, so there is no source archive to fetch"
        return 0
    fi
    url="https://github.com/DiverseInc/DepthSight/archive/${commit}.tar.gz"
    tmp=$(mktemp)
    code=$(curl -sL --max-time 60 -o "$tmp" -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    if [ "$code" != "200" ]; then
        rm -f "$tmp"
        echo "FAIL: the source archive the offer names is not available: GET $url returned HTTP $code"
        return 1
    fi
    if [ ! -s "$tmp" ]; then
        rm -f "$tmp"
        echo "FAIL: the source archive the offer names is empty: GET $url returned HTTP 200 and no bytes"
        return 1
    fi
    rm -f "$tmp"
    echo "OK: the source archive the offer names is reachable and not empty: $url"
    return 0
}

echo -e "${BLUE}[*] Verifying the source archive the offer names really exists...${NC}"
if ! source_archive_is_available; then
    echo -e "${RED}[!] The AGPL source offer names an archive that does not resolve${NC}"
    echo -e "${RED}[!] The offer promises https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz for the stamped commit${NC}"
    echo -e "${RED}[!] Either that commit is not published, or the offer names a commit the running build is not${NC}"
    exit 1
fi

# 5. Cleanup
echo -e "${BLUE}[*] Cleaning up old unused Docker images to free up space...${NC}"
docker image prune -f > /dev/null 2>&1

echo -e "${GREEN}------------------------------------------------"
echo "[+] UPDATE COMPLETE! All services are running the latest version."
echo "------------------------------------------------${NC}"
