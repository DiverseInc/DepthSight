#!/usr/bin/env bash
# Build the frontend in a Linux environment.
#
# WHY THIS EXISTS
# ---------------
# On Windows, `npm run build` in frontend/ fails at the chunk-render step:
#
#     [vite:esbuild-transpile] remove <temp file>: Access is denied.
#
# Reproduced 3x, including after repointing TEMP/TMP, so it is not a path
# problem -- something on this machine locks esbuild's temp file. Every attempt
# reached "✓ 4927 modules transformed" first, so it is never a source error.
#
# Production builds in Docker on Linux, where this does not occur. So: verify
# frontend changes HERE, on Linux, which is the platform that actually matters.
# `vite build` also does not typecheck (esbuild strips types without checking),
# so run `npx tsc --noEmit` separately -- there are 162 pre-existing errors.
#
# USAGE
#   tools/build_frontend_linux.sh          # build merged working tree
#   tools/build_frontend_linux.sh --tsc    # typecheck too
#
# Requires no sudo. If WSL has no node, a portable build is bootstrapped into
# ~/node (needs curl + tar, both present on this box).

set -euo pipefail

# Derive the repo from this script's own location -- the clone is NOT in the
# current session's workspace, so a hardcoded path would be wrong on any other
# machine or session.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND="$REPO/frontend"
WORK="${HOME}/fe_build"
NODE_DIR="${HOME}/node"

# --- node -------------------------------------------------------------------
if [ ! -x "$NODE_DIR/bin/node" ]; then
  echo "[build] bootstrapping portable node into $NODE_DIR ..."
  mkdir -p "$NODE_DIR"
  curl -fsSL -o /tmp/node.tar.xz \
    https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
  tar -xJf /tmp/node.tar.xz -C "$NODE_DIR" --strip-components=1
fi
# NB: export the linux PATH explicitly. Do NOT rely on the inherited Windows
# PATH -- it contains "Program Files (x86)" and the parentheses break bash
# parsing, which silently produces an empty run with no error.
export PATH="$NODE_DIR/bin:/usr/bin:/bin"
echo "[build] node $(node --version) / npm $(npm --version)"

# --- stage sources on the linux filesystem (node_modules must be linux) ------
echo "[build] staging sources to $WORK ..."
rm -rf "$WORK"
mkdir -p "$WORK"
cd "$FRONTEND"
tar -cf - --exclude=node_modules --exclude=dist . 2>/dev/null | (cd "$WORK" && tar -xf -)

cd "$WORK"
if [ ! -d node_modules ]; then
  # --legacy-peer-deps is REQUIRED: eslint@10 conflicts with
  # eslint-plugin-react-hooks@7.0.1 (which caps at eslint 9), so a plain
  # install fails with ERESOLVE. frontend/Dockerfile.prod does the same thing.
  echo "[build] installing dependencies ..."
  npm install --legacy-peer-deps --no-audit --no-fund
fi

if [ "${1:-}" = "--tsc" ]; then
  echo "[build] typechecking (expect pre-existing errors) ..."
  npx tsc --noEmit || echo "[build] tsc reported errors (see above)"
fi

echo "[build] building ..."
npm run build
echo "[build] OK -> $WORK/dist"
