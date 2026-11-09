#!/bin/bash
#
# Record the commit this deployment is built from, and refuse to stamp a
# checkout that is not the committed tree.
#
# WHY BOTH HALVES EXIST (measured 2026-11-07, review panel #5 on PR #64):
#
# 1. The AGPL-3.0 section 13 offer tells a reader that the archive for the
#    running build's commit "is the exact tree of the running build". The
#    image is built from the Docker build CONTEXT (the Dockerfile ends with
#    `COPY . .`), while the advertised archive is the COMMITTED tree.
#    `git pull` never removes untracked files, so a checkout with local
#    changes produces a running image that contains files the archive does
#    not. Refusing a dirty checkout makes the claim true by construction
#    instead of leaving it to chance.
#
# 2. The commit has to reach the running service with no human in the loop.
#    docker-compose.yml gives the `api` service `env_file: .env`, so a
#    DEPLOYED_COMMIT line written into .env travels WITH the container and
#    comes back out of GET /api/v1/status as the `commit` field.
#
# IT DELETES NOTHING. A dirty deployment checkout is a human problem: this
# script says exactly what is wrong and stops. It never runs `git clean`,
# `git checkout`, `rm` on the tree, or anything else that could destroy
# work.
#
# Usage: run it from the project directory, AFTER `git pull` and BEFORE the
# image build. See the "Deploying to Elestio" section of AGENTS.md.
#
# Run it as `bash ops/scripts/record_deployed_commit.sh`, NOT as
# `./ops/scripts/record_deployed_commit.sh`: every tracked script in this
# repository is mode 100644 (there is not one 100755 file in it), so the file
# is not executable after a checkout and `./...` fails with "Permission
# denied" on the deploy host -- silently leaving the commit unstamped.

set -euo pipefail

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "record_deployed_commit: this is not inside a git repository." >&2
    echo "  Run it from the project directory (the one holding .git and docker-compose.yml)." >&2
    exit 1
fi

PROJECT_ROOT=$(git rev-parse --show-toplevel)
cd "$PROJECT_ROOT"

DEPLOYED_COMMIT=$(git rev-parse HEAD)
ENV_FILE="$PROJECT_ROOT/.env"

# --- refuse a checkout that is not the committed tree ------------------------
# .env, data/ and logs/ are gitignored, so they do not appear here: a
# deployment with its environment file and its data in place is not "dirty".
DIRTY=$(git status --porcelain)
if [ -n "$DIRTY" ]; then
    echo "record_deployed_commit: REFUSING to stamp a checkout that is not the committed tree." >&2
    echo >&2
    echo "The containers are built from the Docker build context, while the" >&2
    echo "published source is the committed tree. These paths differ:" >&2
    echo >&2
    printf '%s\n' "$DIRTY" >&2
    echo >&2
    echo "Build from a clean checkout so the source we publish is exactly the" >&2
    echo "source that is running." >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    echo >&2
    echo "  git status --porcelain   # the same list" >&2
    echo "  git stash -u             # park the changes; git stash pop brings them back" >&2
    echo "  git checkout -- <path>   # discard ONE path's changes, deliberately" >&2
    exit 1
fi

# --- the environment file must already exist --------------------------------
# It holds the live secrets and is gitignored, so it is the operator's file,
# not this script's. Creating a stub .env with nothing but the stamp would
# start the application with no configuration at all -- a silent outage --
# so this stops instead.
if [ ! -f "$ENV_FILE" ]; then
    echo "record_deployed_commit: there is no .env in $PROJECT_ROOT." >&2
    echo "  That file holds the live configuration and it is gitignored, so it" >&2
    echo "  is the operator's file, not the repository's. It must exist before" >&2
    echo "  a deployment: docker-compose.yml hands it to the api service as" >&2
    echo "  env_file, and a container with no configuration is worse than a" >&2
    echo "  deployment that stops." >&2
    echo "  NOT A SINGLE FILE HAS BEEN CHANGED." >&2
    exit 1
fi

# --- record the commit, idempotently ----------------------------------------
ENV_TMP=$(mktemp)
trap 'rm -f "$ENV_TMP"' EXIT

if grep -q '^DEPLOYED_COMMIT=' "$ENV_FILE"; then
    # Replace the value in place, keeping that line's own line ending: a CRLF
    # .env must not lose its carriage returns, and an LF one must not gain any.
    awk -v sha="$DEPLOYED_COMMIT" '
        /^DEPLOYED_COMMIT=/ {
            line = $0
            cr = ""
            if (sub(/\r$/, "", line)) { cr = "\r" }
            print "DEPLOYED_COMMIT=" sha cr
            next
        }
        { print }
    ' "$ENV_FILE" > "$ENV_TMP"
else
    # Append, keeping the endings the file already has and making sure the
    # last line is terminated before the new one goes on.
    awk -v sha="$DEPLOYED_COMMIT" '
        { print }
        END { print "DEPLOYED_COMMIT=" sha }
    ' "$ENV_FILE" > "$ENV_TMP"
fi

cat "$ENV_TMP" > "$ENV_FILE"
echo "DEPLOYED_COMMIT=$DEPLOYED_COMMIT"
