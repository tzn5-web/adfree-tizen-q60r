#!/usr/bin/env bash
set -euo pipefail

# Samsung Tizen 5.0 TV emulator smoke test.
# Usage:
#   TIZEN_HOME=/path/to/tizen-studio \
#   WGT=/path/to/tube-tizen-5.0.wgt \
#   ./scripts/emulator-smoke.sh
#
# Optional:
#   SERIAL=emulator-26101
#   APP_ID=Q60AdFree1.Tube
#   PACKAGE_NAME=tube-tizen-5.0.wgt

: "${TIZEN_HOME:?Set TIZEN_HOME to your Tizen Studio installation}"
: "${WGT:?Set WGT to the generated tube-tizen-5.0.wgt}"

SERIAL="${SERIAL:-emulator-26101}"
APP_ID="${APP_ID:-Q60AdFree1.Tube}"
PACKAGE_NAME="${PACKAGE_NAME:-$(basename "$WGT")}"

TIZEN="${TIZEN_HOME}/tools/ide/bin/tizen"
SDB="${TIZEN_HOME}/tools/sdb"

test -x "$TIZEN" || { echo "Missing Tizen CLI: $TIZEN" >&2; exit 2; }
test -x "$SDB" || { echo "Missing SDB: $SDB" >&2; exit 2; }
test -s "$WGT" || { echo "Missing/empty WGT: $WGT" >&2; exit 2; }

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
echo "== Install permission =="
"$TIZEN" install-permit -s "$SERIAL"

echo
echo "== Install WGT =="
"$TIZEN" install -s "$SERIAL" --name "$PACKAGE_NAME" -- "$WGT"

echo
echo "== Launch application =="
"$TIZEN" run -s "$SERIAL" -p "$APP_ID"

echo
echo "Smoke test launch command completed."
echo "Use Web Inspector for runtime errors and player/network diagnostics."
