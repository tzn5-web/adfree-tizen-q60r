#!/usr/bin/env bash
set -euo pipefail

# Samsung Tizen 5.0 TV emulator smoke test.
#
# The CI artifact is intentionally unsigned for the Q60R Homebrew path.
# Samsung's Tizen CLI requires a valid certificate profile for packaging
# an installable test WGT, so this script signs a temporary copy first.
#
# Usage:
#   TIZEN_HOME=/path/to/tizen-studio \
#   WGT=/path/to/tube-tizen-5.0.wgt \
#   CERT_PROFILE=myCert \
#   bash ./scripts/emulator-smoke.sh
#
# Optional:
#   SERIAL=emulator-26101
#   APP_ID=Q60AdFree1.Tube

: "${TIZEN_HOME:?Set TIZEN_HOME to your Tizen Studio installation}"
: "${WGT:?Set WGT to the generated tube-tizen-5.0.wgt}"
: "${CERT_PROFILE:?Set CERT_PROFILE to a valid Tizen certificate profile}"

SERIAL="${SERIAL:-emulator-26101}"
APP_ID="${APP_ID:-Q60AdFree1.Tube}"

TIZEN="${TIZEN_HOME}/tools/ide/bin/tizen"
SDB="${TIZEN_HOME}/tools/sdb"

test -x "$TIZEN" || { echo "Missing Tizen CLI: $TIZEN" >&2; exit 2; }
test -x "$SDB" || { echo "Missing SDB: $SDB" >&2; exit 2; }
test -s "$WGT" || { echo "Missing/empty WGT: $WGT" >&2; exit 2; }

TMP_ROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMP_ROOT"; }
trap cleanup EXIT

APP_DIR="${TMP_ROOT}/app"
mkdir -p "$APP_DIR"

echo "== Connected targets =="
"$SDB" devices

echo
echo "== Waiting for $SERIAL =="
for _ in $(seq 1 30); do
  if "$SDB" devices | awk 'NR>1 {print $1}' | grep -qx "$SERIAL"; then
    break
  fi
  sleep 2
done
"$SDB" devices | awk 'NR>1 {print $1}' | grep -qx "$SERIAL" || {
  echo "Target $SERIAL not found. Start the Samsung Tizen 5.0 TV emulator first." >&2
  exit 3
}

echo
echo "== Unpack unsigned WGT =="
unzip -q "$WGT" -d "$APP_DIR"
rm -f "$APP_DIR/author-signature.xml" "$APP_DIR/signature1.xml"

echo
echo "== Sign/package temporary WGT =="
(
  cd "$APP_DIR"
  "$TIZEN" package -t wgt -s "$CERT_PROFILE" -- "$APP_DIR"
)

SIGNED_WGT="$(find "$APP_DIR" -maxdepth 1 -type f -name '*.wgt' -print -quit)"
test -s "$SIGNED_WGT" || {
  echo "No signed WGT was produced by Tizen CLI." >&2
  exit 4
}

echo
echo "== Install permission =="
"$TIZEN" install-permit -s "$SERIAL"

echo
echo "== Install signed WGT =="
"$TIZEN" install -s "$SERIAL" --name "$(basename "$SIGNED_WGT")" -- "$APP_DIR"

echo
echo "== Launch application =="
"$TIZEN" run -s "$SERIAL" -p "$APP_ID"

echo
echo "Smoke test launch command completed."
echo "Use Samsung Web Inspector/runtime logs for startup, network, player and AdBlock diagnostics."
