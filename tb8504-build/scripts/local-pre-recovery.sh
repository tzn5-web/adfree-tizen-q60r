#!/usr/bin/env bash

# Historical/support runner. Normal operation must use tb8504-autopilot.py.
if [ "${TB8504_ALLOW_LEGACY_RUNNER:-0}" != "1" ]; then
    echo "LEGACY_RUNNER_LOCKED=YES" >&2
    echo "Use tb8504-build/scripts/tb8504-autopilot.py as the single supported entry point." >&2
    exit 64
fi
# TB8504 Android 16 pre-recovery convergence gate.
# Applies only cloud-audited source transforms, re-audits the real workspace,
# and STOPS before any build/device access.
set -Eeo pipefail

ROOT="${1:-/home/dre/android16-tb8504/lineage-23.2}"
DEVICE="$ROOT/device/lenovo/TB8504"
VENDOR="$ROOT/vendor/lenovo/TB8504"
PRODUCT_OUT="$ROOT/out/target/product/TB8504"
MODULE_INFO="$PRODUCT_OUT/module-info.json"

REPO="tzn5-web/adfree-tizen-q60r"
TOOLING_REF="66cbb6e641868efe706d5b38e733eead67f1797f"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/TB8504_PRE_RECOVERY_${STAMP}"
TOOLS="$REPORT/tools"

mkdir -p "$TOOLS" "$REPORT/backup" "$REPORT/audit"
exec > >(tee "$REPORT/FULL.log") 2>&1

fail() {
  local rc="$1"; shift
  echo "BLOCKER=$*"
  {
    echo "FINAL_RC=$rc"
    echo "PRE_RECOVERY=BLOCKED"
    echo "NO_BUILD=YES"
    echo "NO_FLASH=YES"
    echo "REPORT=$REPORT"
  } > "$REPORT/STATUS.txt"
  exit "$rc"
}

echo "============================================================"
echo "TB8504 PRE-RECOVERY CONVERGENCE GATE"
echo "NO BUILD / NO ADB / NO FASTBOOT / NO FLASH"
echo "============================================================"
echo "ROOT=$ROOT"
echo "TOOLING_REF=$TOOLING_REF"
echo "REPORT=$REPORT"

for x in git curl python3 readelf bash; do
  command -v "$x" >/dev/null || fail 2 "missing command: $x"
done
for p in "$DEVICE" "$VENDOR" "$MODULE_INFO"; do
  [ -e "$p" ] || fail 3 "missing required path: $p"
done

git -C "$DEVICE" status --short > "$REPORT/backup/device-status-before.txt" || true
git -C "$VENDOR" status --short > "$REPORT/backup/vendor-status-before.txt" || true
git -C "$DEVICE" diff --binary > "$REPORT/backup/device-before.patch" || true
git -C "$VENDOR" diff --binary > "$REPORT/backup/vendor-before.patch" || true

fetch_tool() {
  local n="$1"
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$TOOLING_REF/tb8504-build/scripts/$n"     -o "$TOOLS/$n" || fail 10 "cannot fetch $n"
  test -s "$TOOLS/$n" || fail 11 "empty helper: $n"
}

for n in   apply-residual-cleanup.py   audit-residual-contracts.py   apply-product-compat.py   audit-product-compat.py   audit-stage8n.py   audit-runtime-contracts.py; do
  fetch_tool "$n"
done

python3 -m py_compile "$TOOLS"/*.py || fail 12 "helper syntax failure"

if grep -RniE '(^|[[:space:]])(adb|fastboot)[[:space:]]|/dev/(block|snd)|dd[[:space:]].*of=/dev/' "$TOOLS"; then
  fail 13 "device-write primitive found in helper set"
fi

echo
echo "=== APPLY RESIDUAL IMS/WFD/INIT FIX ==="
python3 "$TOOLS/apply-residual-cleanup.py"   --device "$DEVICE"   --vendor "$VENDOR"   --patch-out "$REPORT/audit/residual-cleanup.patch"   --report-out "$REPORT/audit/residual-cleanup.txt"   2>&1 | tee "$REPORT/audit/residual-cleanup.log"
RC=${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 20 "residual cleanup failed rc=$RC"
grep -Fx 'RESIDUAL_CLEANUP=PASS' "$REPORT/audit/residual-cleanup.txt" >/dev/null ||
  fail 21 "residual cleanup did not report PASS"

echo
echo "=== APPLY ANDROID 16 PRODUCT PROP MIGRATION ==="
python3 "$TOOLS/apply-product-compat.py"   --device "$DEVICE"   --patch-out "$REPORT/audit/product-compat.patch"   --report-out "$REPORT/audit/product-compat.txt"   2>&1 | tee "$REPORT/audit/product-compat.log"
RC=${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 30 "product compatibility migration failed rc=$RC"
grep -Fx 'PRODUCT_COMPAT=PASS' "$REPORT/audit/product-compat.txt" >/dev/null ||
  fail 31 "product compatibility migration did not report PASS"

echo
echo "=== SHELL SYNTAX ==="
for f in   "$DEVICE/rootdir/etc/init.class_main.sh"   "$DEVICE/rootdir/etc/init.qcom.post_boot.sh"   "$DEVICE/rootdir/etc/init.qcom.sensors.sh"   "$DEVICE/rootdir/etc/init.qcom.sh"; do
  echo "BASH_N=$f"
  bash -n "$f" || fail 40 "shell syntax failure: $f"
done

echo
echo "=== PRODUCT PROP AUDIT ==="
python3 "$TOOLS/audit-product-compat.py" --device "$DEVICE"   2>&1 | tee "$REPORT/audit/product-compat-audit.log"
RC=${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 41 "product compatibility audit failed rc=$RC"

echo
echo "=== STAGE8N ELF AUDIT ==="
python3 "$TOOLS/audit-stage8n.py"   --vendor "$VENDOR" --device "$DEVICE" --report-dir "$REPORT/audit"   2>&1 | tee "$REPORT/audit/stage8n.log"
RC=${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 42 "STAGE8N audit failed rc=$RC"

echo
echo "=== RUNTIME INIT/VINTF AUDIT ==="
python3 "$TOOLS/audit-runtime-contracts.py"   --vendor "$VENDOR" --device "$DEVICE"   --report-dir "$REPORT/audit" --module-info "$MODULE_INFO"   2>&1 | tee "$REPORT/audit/runtime-contracts.log"
RC=${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 43 "runtime contracts failed rc=$RC"

echo
echo "=== RESIDUAL CROSS-LAYER AUDIT ==="
python3 "$TOOLS/audit-residual-contracts.py"   --vendor "$VENDOR" --device "$DEVICE"   2>&1 | tee "$REPORT/audit/residual-contracts.log"
RC=${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 44 "residual contracts failed rc=$RC"

git -C "$DEVICE" status --short > "$REPORT/backup/device-status-after.txt" || true
git -C "$VENDOR" status --short > "$REPORT/backup/vendor-status-after.txt" || true
git -C "$DEVICE" diff --binary > "$REPORT/backup/device-after.patch" || true
git -C "$VENDOR" diff --binary > "$REPORT/backup/vendor-after.patch" || true

{
  echo "FINAL_RC=0"
  echo "PRE_RECOVERY=PASS"
  echo "PRODUCT_COMPAT=PASS"
  echo "STAGE8N=PASS"
  echo "RUNTIME_CONTRACTS=PASS"
  echo "RESIDUAL_CONTRACTS=PASS"
  echo "NO_BUILD=YES"
  echo "NO_FLASH=YES"
  echo "REPORT=$REPORT"
} | tee "$REPORT/STATUS.txt"

echo "============================================================"
echo "TB8504_PRE_RECOVERY=PASS"
echo "NO_BUILD=YES"
echo "NEXT=recoveryimage"
echo "REPORT=$REPORT"
echo "============================================================"
