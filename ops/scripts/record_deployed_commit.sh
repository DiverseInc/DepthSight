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
# AND WHY THE PUBLISHED CHECK IS AS NARROW AS IT IS (measured 2026-11-09,
# review panel on PR #64, finding F1 raised by 3 of 6 engines):
#
# 3. The offer publishes
#      https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz
#    and says the commit is public there, and frontend/source.html says the
#    running service is deployed from the `main` branch of THAT repository.
#    Three ways the old check broke that promise, all measured:
#
#    a. `git branch -r --contains HEAD` accepts a commit contained by ANY
#       remote-tracking branch of ANY remote -- origin/some-feature, or a
#       ref on a different remote -- so a commit that is not on `main` was
#       stamped and the page's claim about `main` became false.
#    b. Nothing ever checked WHICH repository `origin` points at. A private
#       fork, another project or another host all produce an advertised
#       archive URL that is a 404 while the stamp is written happily.
#    c. `git branch -r --contains HEAD` reads the LOCAL remote-tracking
#       cache, which is stale by construction: `git fetch origin main`
#       (what update.sh:47 does) refreshes origin/main and leaves every
#       other remote-tracking ref exactly as it was, so a branch deleted
#       upstream still looks published here forever.
#
#    So this script now asks three separate questions and refuses unless
#    the answer to all of them is yes:
#      * is `origin` the public repository the offer names?
#      * is the commit in the history of refs/remotes/origin/main -- that
#        ref, not some other branch and not some other remote?
#      * does the LIVE remote agree? `git ls-remote origin main` says what
#        the remote's main branch is right now, and the commit has to be an
#        ancestor of it. A remote that cannot be reached is a REFUSAL, not
#        a licence to assume the commit is published.
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

# --- the origin remote must BE the public repository ------------------------
# The offer names ONE repository, and the archive URL it publishes lives on
# that repository. If `origin` points anywhere else -- a private fork, a
# different project, another host -- the stamp would advertise a 404 as the
# source of the running build, so the URL is checked before anything else.
#
# The DECLARED url is read with `git config --get-all remote.origin.url` and
# NOT with `git remote get-url origin`: get-url expands url.<base>.insteadOf
# rewrites, so it reports the transport this machine happens to use rather
# than the repository the operator pointed origin at. The repository named
# in the offer is the declared one. (The deploy host itself uses an
# insteadOf rewrite to normalise the ssh form -- see AGENTS.md -- so the
# declared url is exactly the thing to compare against.)
ORIGIN_URLS=$(git config --get-all remote.origin.url 2>/dev/null || true)

if [ -z "$ORIGIN_URLS" ]; then
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "This checkout has no 'origin' remote at all, so there is no public" >&2
    echo "repository to check the commit against, and the archive URL the offer" >&2
    echo "publishes for it would be a 404:" >&2
    echo "  https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz" >&2
    echo >&2
    echo "  git remote -v" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

ORIGIN_IS_PUBLIC=yes
for ORIGIN_URL in $ORIGIN_URLS; do
    case "$ORIGIN_URL" in
        https://github.com/DiverseInc/DepthSight|https://github.com/DiverseInc/DepthSight.git|git@github.com:DiverseInc/DepthSight|git@github.com:DiverseInc/DepthSight.git|ssh://git@github.com/DiverseInc/DepthSight|ssh://git@github.com/DiverseInc/DepthSight.git)
            ;;
        *)
            ORIGIN_IS_PUBLIC=no
            ;;
    esac
done

if [ "$ORIGIN_IS_PUBLIC" != "yes" ]; then
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "The 'origin' remote of this checkout is not the public repository the" >&2
    echo "source offer names, so the archive URL the offer publishes for this" >&2
    echo "commit would be a 404:" >&2
    echo "  https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz" >&2
    echo >&2
    echo "The 'origin' remote of this checkout is:" >&2
    printf '  %s\n' "$ORIGIN_URLS" >&2
    echo >&2
    echo "It has to be the public repository:" >&2
    echo "  git remote set-url origin https://github.com/DiverseInc/DepthSight.git" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

# --- the commit must be PUBLISHED ---------------------------------------------
# The offer points a reader at
#   https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz
# A commit that was never pushed has no archive there: that URL is a 404, and
# the page's promise that the commit is public would be false. So refuse to
# stamp a commit that no pushed ref contains.
PUBLISHED_REFS=""
if ! PUBLISHED_REFS=$(git branch -r --contains HEAD 2>/dev/null); then
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "This checkout's history is too shallow to say whether the commit has" >&2
    echo "been pushed, so the script cannot promise the reader a working" >&2
    echo "archive URL for it. Fetch the history and deploy again:" >&2
    echo >&2
    echo "  git fetch --unshallow origin" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

if [ -z "$PUBLISHED_REFS" ]; then
    echo "record_deployed_commit: REFUSING to stamp a commit that is not published." >&2
    echo >&2
    echo "The source offer points readers at the archive of this commit:" >&2
    echo "  https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz" >&2
    echo "A commit that was never pushed has no archive there -- that URL is a" >&2
    echo "404, and the page says the commit is public." >&2
    echo >&2
    echo "No pushed branch contains this commit. The pushed branches are:" >&2
    git branch -r >&2
    echo >&2
    echo "  git push origin HEAD:main   # publish it, then deploy again" >&2
    echo "  git fetch origin && git reset --hard origin/main   # or deploy a published commit" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

# --- and it has to be on the MAIN branch of that repository ------------------
# `git branch -r --contains HEAD` above only says that SOME remote-tracking
# branch contains the commit. The offer says the running service is deployed
# from `main` and that `main` contains the code the reader is talking to, so
# the commit has to be in the history of refs/remotes/origin/main -- that
# exact ref, not origin/some-feature, not a tag, not a branch of another
# remote.
MAIN_RC=0
git merge-base --is-ancestor "$DEPLOYED_COMMIT" refs/remotes/origin/main 2>/dev/null || MAIN_RC=$?

if [ "$MAIN_RC" -ne 0 ]; then
    if [ "$MAIN_RC" -eq 1 ]; then
        echo "record_deployed_commit: REFUSING to stamp a commit that is not on the main branch." >&2
        echo >&2
        echo "The offer tells the reader that the running service is deployed" >&2
        echo "from the 'main' branch of the public repository, and that 'main'" >&2
        echo "contains the code they are talking to. This commit is not in the" >&2
        echo "history of refs/remotes/origin/main, so stamping it would make" >&2
        echo "that promise false." >&2
        echo >&2
        echo "The pushed branches that DO contain this commit are:" >&2
        git branch -r --contains HEAD >&2
        echo >&2
        echo "If the LIVE main branch of the public repository already contains" >&2
        echo "this commit, then the local copy of origin/main is behind the" >&2
        echo "remote, and it has to be refreshed before this script can check" >&2
        echo "it:" >&2
        echo >&2
        echo "  git fetch origin main" >&2
        echo >&2
        echo "Otherwise the commit has to be published on main first:" >&2
        echo >&2
        echo "  git push origin HEAD:main   # publish it on main, then deploy again" >&2
        echo "  git fetch origin && git reset --hard origin/main   # or deploy a published commit" >&2
        echo >&2
        echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
        exit 1
    fi
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "This checkout has no usable refs/remotes/origin/main, so the script" >&2
    echo "cannot tell whether this commit is on the main branch the running" >&2
    echo "service is deployed from. Fetch main and deploy again:" >&2
    echo >&2
    echo "  git fetch origin main" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

# --- and the LIVE remote has to confirm it ----------------------------------
# Everything above reads the LOCAL cache of the remote, and that cache is
# stale by construction: `git fetch origin main` refreshes origin/main and
# leaves every other remote-tracking ref exactly as it was, so a branch that
# was deleted upstream still looks published here forever. So the remote
# itself is asked what its main branch is right now, and the commit has to
# be an ancestor of that answer.
#
# A remote that cannot be reached is a REFUSAL, never an assumption: the
# whole point of this check is that the local cache cannot answer it.
REMOTE_LISTING_FAILED=0
REMOTE_MAIN_RAW=""
if ! REMOTE_MAIN_RAW=$(git ls-remote origin main 2>/dev/null); then
    REMOTE_LISTING_FAILED=1
fi

if [ "$REMOTE_LISTING_FAILED" -ne 0 ]; then
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "The live remote could not be asked what its main branch is, so this" >&2
    echo "script cannot tell whether the commit is published on the public" >&2
    echo "main branch. It will not assume that it is: an unpublished commit" >&2
    echo "has no archive, and the offer would publish a 404 as the source of" >&2
    echo "the running build." >&2
    echo >&2
    echo "  git ls-remote origin main   # the same question, by hand" >&2
    echo >&2
    echo "Check this host's network access to github.com and its credentials," >&2
    echo "then deploy again. If the host is offline, the deploy has to run" >&2
    echo "from one that can reach the public repository." >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

REMOTE_MAIN_SHA=$(printf '%s\n' "$REMOTE_MAIN_RAW" | awk '$2 == "refs/heads/main" { print $1; exit }')

if [ -z "$REMOTE_MAIN_SHA" ]; then
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "The remote answered, but it has no 'main' branch, so there is no" >&2
    echo "branch to say whether this commit is published on. The offer tells" >&2
    echo "the reader that the running service comes from 'main' of" >&2
    echo "  https://github.com/DiverseInc/DepthSight" >&2
    echo "so the script will not promise that on its behalf." >&2
    echo >&2
    echo "  git ls-remote origin        # every ref the remote has" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
    exit 1
fi

ANCESTOR_RC=0
git merge-base --is-ancestor "$DEPLOYED_COMMIT" "$REMOTE_MAIN_SHA" 2>/dev/null || ANCESTOR_RC=$?

if [ "$ANCESTOR_RC" -ne 0 ]; then
    if [ "$ANCESTOR_RC" -eq 1 ]; then
        echo "record_deployed_commit: REFUSING to stamp a commit that is not published." >&2
        echo >&2
        echo "The live 'main' branch of the public repository does not contain" >&2
        echo "this commit, so the archive the offer publishes for it would be" >&2
        echo "a 404:" >&2
        echo "  https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz" >&2
        echo >&2
        echo "The local cache of origin/main may still list the commit: that is" >&2
        echo "exactly the stale-cache case this check exists for, because the" >&2
        echo "branch or the commit was removed upstream and the local cache" >&2
        echo "was never refreshed for it." >&2
        echo >&2
        echo "  git push origin HEAD:main   # publish it on main, then deploy again" >&2
        echo "  git fetch origin main && git reset --hard origin/main   # or deploy a published commit" >&2
        echo >&2
        echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
        exit 1
    fi
    echo "record_deployed_commit: REFUSING to stamp a commit that cannot be checked." >&2
    echo >&2
    echo "The remote's main branch is at a commit this checkout does not have," >&2
    echo "so the script cannot tell whether this commit is part of that" >&2
    echo "history. It will not assume that it is. Fetch main and deploy again:" >&2
    echo >&2
    echo "  git fetch origin main" >&2
    echo >&2
    echo "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT." >&2
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
