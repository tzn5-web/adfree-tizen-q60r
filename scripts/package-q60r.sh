#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_DIR="${UPSTREAM_DIR:-$ROOT_DIR/.upstream}"
OUT_DIR="$ROOT_DIR/dist"

mkdir -p "$OUT_DIR"

cd "$UPSTREAM_DIR"

npm ci
node "$ROOT_DIR/scripts/apply-q60r.mjs" "$UPSTREAM_DIR"
npm run doctor
npm run version:check
npm run build
npm test
npm run package

SOURCE_WGT="$UPSTREAM_DIR/release/tube-tizen-5.0.wgt"
VERSION=$(node -p "require('./.upstream/package.json').version")
DEST_WGT="$OUT_DIR/adfree-tizen-q60r-${VERSION}-tizen5.0.wgt"

test -s "$SOURCE_WGT"
cp -f "$SOURCE_WGT" "$DEST_WGT"
sha256sum "$DEST_WGT" > "$DEST_WGT.sha256"

node "$ROOT_DIR/scripts/q60r-smoke.mjs" "$UPSTREAM_DIR" "$DEST_WGT"
node "$ROOT_DIR/tests/wgt-regression.mjs" "$DEST_WGT"
