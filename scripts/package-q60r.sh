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

cp -f tube-tizen-5.0.wgt "$OUT_DIR/adfree-tizen-q60r-1.0.0-tizen5.0.wgt"
sha256sum "$OUT_DIR/adfree-tizen-q60r-1.0.0-tizen5.0.wgt" > "$OUT_DIR/adfree-tizen-q60r-1.0.0-tizen5.0.wgt.sha256"

node "$ROOT_DIR/scripts/q60r-smoke.mjs" "$UPSTREAM_DIR" "$OUT_DIR/adfree-tizen-q60r-1.0.0-tizen5.0.wgt"
