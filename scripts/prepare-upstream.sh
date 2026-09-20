#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$ROOT_DIR/q60r.config.json"
UPSTREAM_DIR="${UPSTREAM_DIR:-$ROOT_DIR/.upstream}"

REPO=$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).upstream.repository)' "$CFG")
COMMIT=$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")).upstream.commit)' "$CFG")

rm -rf "$UPSTREAM_DIR"
git clone --filter=blob:none --no-checkout "$REPO" "$UPSTREAM_DIR"
git -C "$UPSTREAM_DIR" checkout --detach "$COMMIT"

echo "Prepared upstream $COMMIT in $UPSTREAM_DIR"
