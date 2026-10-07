#!/usr/bin/env bash
# TB8504 Android 16 local finalization gate.
# Applies only the runtime cleanup already validated in GitHub, audits the
# real local workspace, then performs the integrated LineageOS build.
# NO ADB / NO FASTBOOT / NO FLASH / NO DEVICE ACCESS.

set -Eeo pipefail

ROOT="${1:-/home/dre/android16-tb8504/lineage-23.2}"
DEVICE="$ROOT/device/lenovo/TB8504"
VENDOR="$ROOT/vendor/lenovo/TB8504"
KERNEL="$ROOT/kernel/lenovo/msm8917"
PRODUCT_OUT="$ROOT/out/target/product/TB8504"
MODULE_INFO="$PRODUCT_OUT/module-info.json"

GITHUB_REPO="tzn5-web/adfree-tizen-q60r"
GITHUB_REF="56e6907c03bf40f1417844200be5458f86de6b26"
EXPECTED_KERNEL_HEAD="d242d540d9f5328919e189235e74e433418f6d81"
SEED_SHA256="cb229cf709ba3ba66f0a4bb18678923730e421b2dfe4223d59d22d74bf719651"

STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/TB8504_LOCAL_FINAL_${STAMP}"
TOOLS="$REPORT/tools"
PRE="$REPORT/preflight"
PREAUDIT="$REPORT/prebuild-audit"
POSTAUDIT="$REPORT/postbuild-audit"

mkdir -p "$TOOLS" "$PRE" "$PREAUDIT" "$POSTAUDIT"

exec > >(tee "$REPORT/FULL.log") 2>&1

fail() {
    local code="$1"
    shift
    echo "BLOCKER=$*"
    echo "FINAL_RC=$code" > "$REPORT/STATUS.txt"
    echo "FINAL_STATUS=BLOCKED" >> "$REPORT/STATUS.txt"
    echo "REPORT=$REPORT"
    exit "$code"
}

echo "============================================================"
echo "TB8504 ANDROID 16 LOCAL FINALIZATION GATE"
echo "NO ADB / NO FASTBOOT / NO FLASH / NO DEVICE ACCESS"
echo "============================================================"
echo "ROOT=$ROOT"
echo "REPORT=$REPORT"
echo "TOOLING_REF=$GITHUB_REF"
echo "SEED_SHA256=$SEED_SHA256"

for cmd in git curl python3 readelf sha256sum awk grep sed find nproc; do
    command -v "$cmd" >/dev/null 2>&1 || fail 2 "required command missing: $cmd"
done

for path in "$ROOT/build/envsetup.sh" "$DEVICE" "$VENDOR" "$KERNEL"; do
    [ -e "$path" ] || fail 3 "required workspace path missing: $path"
done

[ -f "$MODULE_INFO" ] || fail 4 "existing module-info.json missing: $MODULE_INFO"

DEVICE_HEAD="$(git -C "$DEVICE" rev-parse HEAD 2>/dev/null || true)"
VENDOR_HEAD="$(git -C "$VENDOR" rev-parse HEAD 2>/dev/null || true)"
KERNEL_HEAD="$(git -C "$KERNEL" rev-parse HEAD 2>/dev/null || true)"

echo "DEVICE_HEAD=$DEVICE_HEAD"
echo "VENDOR_HEAD=$VENDOR_HEAD"
echo "KERNEL_HEAD=$KERNEL_HEAD"

[ "$KERNEL_HEAD" = "$EXPECTED_KERNEL_HEAD" ] ||
    fail 5 "kernel HEAD drifted: local=$KERNEL_HEAD expected=$EXPECTED_KERNEL_HEAD"

{
    echo "DEVICE_HEAD=$DEVICE_HEAD"
    echo "VENDOR_HEAD=$VENDOR_HEAD"
    echo "KERNEL_HEAD=$KERNEL_HEAD"
    echo "SEED_SHA256=$SEED_SHA256"
    echo "TOOLING_REF=$GITHUB_REF"
    echo "CREATED_AT=$(date -Iseconds)"
} > "$PRE/identity.txt"

git -C "$DEVICE" status --short > "$PRE/device-status-before.txt" || true
git -C "$VENDOR" status --short > "$PRE/vendor-status-before.txt" || true
git -C "$KERNEL" status --short > "$PRE/kernel-status-before.txt" || true
git -C "$DEVICE" diff --binary > "$PRE/device-before.patch" || true
git -C "$VENDOR" diff --binary > "$PRE/vendor-before.patch" || true
git -C "$KERNEL" diff --binary > "$PRE/kernel-before.patch" || true
git -C "$DEVICE" ls-files --others --exclude-standard > "$PRE/device-untracked.txt" || true
git -C "$VENDOR" ls-files --others --exclude-standard > "$PRE/vendor-untracked.txt" || true
git -C "$KERNEL" ls-files --others --exclude-standard > "$PRE/kernel-untracked.txt" || true

(
    cd "$ROOT"
    repo status
) > "$PRE/repo-status-before.txt" 2>&1 || true

FREE_KB="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
echo "FREE_KB=$FREE_KB" | tee "$PRE/disk.txt"
if [[ "$FREE_KB" =~ ^[0-9]+$ ]] && [ "$FREE_KB" -lt 15728640 ]; then
    fail 6 "less than 15 GiB free on workspace filesystem"
fi

fetch_tool() {
    local name="$1"
    local dest="$TOOLS/$name"
    curl -fsSL         "https://raw.githubusercontent.com/$GITHUB_REPO/$GITHUB_REF/tb8504-build/scripts/$name"         -o "$dest" || fail 10 "cannot fetch pinned helper: $name"
    test -s "$dest" || fail 11 "downloaded helper is empty: $name"
}

fetch_tool apply-runtime-cleanup.py
fetch_tool audit-stage8n.py
fetch_tool audit-runtime-contracts.py

python3 -m py_compile     "$TOOLS/apply-runtime-cleanup.py"     "$TOOLS/audit-stage8n.py"     "$TOOLS/audit-runtime-contracts.py" ||
    fail 12 "pinned helper syntax audit failed"

if grep -RniE '(^|[[:space:]])(adb|fastboot)[[:space:]]|/dev/(block|snd)|dd[[:space:]].*of=/dev/' "$TOOLS"; then
    fail 13 "device-write primitive unexpectedly present in helper set"
fi

echo
echo "=== CHECK/APPLY AUDITED RUNTIME CLEANUP ==="

# The previous run may already have applied the validated overlay before a
# later build-environment failure. Detect that state with the same runtime
# contract auditor and do not attempt destructive/redundant re-application.
set +e
python3 "$TOOLS/audit-runtime-contracts.py" \
    --vendor "$VENDOR" \
    --device "$DEVICE" \
    --report-dir "$PREAUDIT/pre-cleanup-state" \
    --module-info "$MODULE_INFO" \
    > "$PREAUDIT/pre-cleanup-state.log" 2>&1
PRE_CLEAN_RC=$?
set -e

if [ "$PRE_CLEAN_RC" -eq 0 ] && \
   grep -Fx 'RUNTIME_CONTRACT_FAILURES=0' "$PREAUDIT/pre-cleanup-state.log" >/dev/null && \
   grep -Fx 'STALE_CAMERA_SEPOLICY_REFERENCES=0' "$PREAUDIT/pre-cleanup-state.log" >/dev/null && \
   grep -Fx 'STALE_VINTF_HAL_DECLARATIONS=0' "$PREAUDIT/pre-cleanup-state.log" >/dev/null && \
   grep -Fx 'REMOVED_SERVICE_REFERENCES=0' "$PREAUDIT/pre-cleanup-state.log" >/dev/null; then
    echo "RUNTIME_CLEANUP_STATE=ALREADY_APPLIED"
    cp "$PREAUDIT/pre-cleanup-state.log" "$PREAUDIT/runtime-cleanup.log"
    {
        echo "RUNTIME_CLEANUP_STATE=ALREADY_APPLIED"
        echo "NO_REAPPLY=YES"
        echo "RUNTIME_CLEANUP=PASS"
    } > "$PREAUDIT/runtime-cleanup.txt"
else
    echo "RUNTIME_CLEANUP_STATE=NEEDS_APPLY"
    python3 "$TOOLS/apply-runtime-cleanup.py" \
        --device "$DEVICE" \
        --patch-out "$PREAUDIT/device-runtime-cleanup.patch" \
        --report-out "$PREAUDIT/runtime-cleanup.txt" \
        2>&1 | tee "$PREAUDIT/runtime-cleanup.log"
    APPLY_RC=${PIPESTATUS[0]}
    [ "$APPLY_RC" -eq 0 ] || fail 20 "runtime cleanup failed rc=$APPLY_RC"

    grep -Fx 'RUNTIME_CLEANUP=PASS' "$PREAUDIT/runtime-cleanup.txt" >/dev/null ||
        fail 21 "runtime cleanup did not report PASS"
    grep -Fx 'REMOVED_INIT_SERVICES=25' "$PREAUDIT/runtime-cleanup.txt" >/dev/null ||
        fail 22 "runtime cleanup did not remove exact validated service set"
    grep -Fx 'REMOVED_CAMERA_SDK_OVERRIDE=1' "$PREAUDIT/runtime-cleanup.txt" >/dev/null ||
        fail 23 "camera SDK override cleanup mismatch"
    grep -Fx 'REMOVED_WFD_VINTF_HAL=1' "$PREAUDIT/runtime-cleanup.txt" >/dev/null ||
        fail 24 "WFD VINTF cleanup mismatch"
    grep -Fx 'CLEARED_CAMERA_DAEMON_SEPOLICY=1' "$PREAUDIT/runtime-cleanup.txt" >/dev/null ||
        fail 25 "camera daemon SELinux cleanup mismatch"
fi

echo
echo "=== PRE-BUILD STAGE8N ELF AUDIT ==="
python3 "$TOOLS/audit-stage8n.py"     --vendor "$VENDOR"     --device "$DEVICE"     --report-dir "$PREAUDIT"     2>&1 | tee "$PREAUDIT/stage8n.log"
ELF_RC=${PIPESTATUS[0]}
[ "$ELF_RC" -eq 0 ] || fail 30 "pre-build STAGE8N audit failed rc=$ELF_RC"

grep -Fx 'UNRESOLVED_EDGES=0' "$PREAUDIT/stage8n.log" >/dev/null ||
    fail 31 "pre-build unresolved ELF edges are nonzero"
grep -Fx 'WRONG_BITNESS_EDGES=0' "$PREAUDIT/stage8n.log" >/dev/null ||
    fail 32 "pre-build wrong-bitness ELF edges are nonzero"
grep -Fx 'STAGE8N_GITHUB_SOURCE_AUDIT=PASS' "$PREAUDIT/stage8n.log" >/dev/null ||
    fail 33 "pre-build STAGE8N did not report PASS"

echo
echo "=== PRE-BUILD RUNTIME CONTRACT AUDIT ==="
python3 "$TOOLS/audit-runtime-contracts.py"     --vendor "$VENDOR"     --device "$DEVICE"     --report-dir "$PREAUDIT"     --module-info "$MODULE_INFO"     2>&1 | tee "$PREAUDIT/runtime-contracts.log"
RUNTIME_RC=${PIPESTATUS[0]}
[ "$RUNTIME_RC" -eq 0 ] || fail 40 "pre-build runtime contract audit failed rc=$RUNTIME_RC"

grep -Fx 'RUNTIME_CONTRACT_FAILURES=0' "$PREAUDIT/runtime-contracts.log" >/dev/null ||
    fail 41 "pre-build runtime contract failures are nonzero"
grep -Fx 'TB8504_RUNTIME_CONTRACT_AUDIT=PASS' "$PREAUDIT/runtime-contracts.log" >/dev/null ||
    fail 42 "pre-build runtime contract audit did not report PASS"

git -C "$DEVICE" status --short > "$PRE/device-status-after-cleanup.txt" || true
git -C "$DEVICE" diff --binary > "$PRE/device-after-cleanup.patch" || true

echo
echo "============================================================"
echo "PREBUILD_GATE=PASS"
echo "Starting integrated LineageOS 23.2 Android 16 build."
echo "No clean/installclean is performed; this is an incremental full build."
echo "============================================================"

cd "$ROOT"
# Android/Lineage envsetup uses intentionally unset shell variables such as TOP.
# Keep nounset disabled for envsetup/lunch/build; errexit and pipefail remain on.
set +u
# shellcheck disable=SC1091
source build/envsetup.sh || fail 49 "source build/envsetup.sh failed"

if ! lunch lineage_TB8504-userdebug; then
    fail 50 "lunch lineage_TB8504-userdebug failed"
fi

set +e
mka bacon 2>&1 | tee "$REPORT/BUILD.log"
BUILD_RC=${PIPESTATUS[0]}
set -e

echo "BUILD_RC=$BUILD_RC" | tee "$REPORT/BUILD_STATUS.txt"
[ "$BUILD_RC" -eq 0 ] || fail 51 "integrated Android 16 build failed rc=$BUILD_RC"

[ -f "$PRODUCT_OUT/module-info.json" ] ||
    fail 52 "fresh module-info.json missing after successful build"
[ -s "$PRODUCT_OUT/boot.img" ] ||
    fail 53 "boot.img missing after successful build"

find "$PRODUCT_OUT" -maxdepth 1 -type f     \( -name 'lineage-23.2-*.zip' -o -name 'boot.img' -o -name 'recovery.img'        -o -name 'system.img' -o -name 'vendor.img' \)     -printf '%f\t%s\n' | sort | tee "$REPORT/build-artifacts.txt"

sha256sum "$PRODUCT_OUT/boot.img" | tee "$REPORT/boot.img.sha256"

echo
echo "=== POST-BUILD STAGE8N ELF AUDIT ==="
python3 "$TOOLS/audit-stage8n.py"     --vendor "$VENDOR"     --device "$DEVICE"     --report-dir "$POSTAUDIT"     2>&1 | tee "$POSTAUDIT/stage8n.log"
POST_ELF_RC=${PIPESTATUS[0]}
[ "$POST_ELF_RC" -eq 0 ] || fail 60 "post-build STAGE8N audit failed rc=$POST_ELF_RC"

echo
echo "=== POST-BUILD RUNTIME CONTRACT AUDIT ==="
python3 "$TOOLS/audit-runtime-contracts.py"     --vendor "$VENDOR"     --device "$DEVICE"     --report-dir "$POSTAUDIT"     --module-info "$PRODUCT_OUT/module-info.json"     2>&1 | tee "$POSTAUDIT/runtime-contracts.log"
POST_RUNTIME_RC=${PIPESTATUS[0]}
[ "$POST_RUNTIME_RC" -eq 0 ] || fail 61 "post-build runtime contract audit failed rc=$POST_RUNTIME_RC"

grep -Fx 'UNRESOLVED_EDGES=0' "$POSTAUDIT/stage8n.log" >/dev/null ||
    fail 62 "post-build unresolved ELF edges are nonzero"
grep -Fx 'WRONG_BITNESS_EDGES=0' "$POSTAUDIT/stage8n.log" >/dev/null ||
    fail 63 "post-build wrong-bitness ELF edges are nonzero"
grep -Fx 'RUNTIME_CONTRACT_FAILURES=0' "$POSTAUDIT/runtime-contracts.log" >/dev/null ||
    fail 64 "post-build runtime contract failures are nonzero"

{
    echo "FINAL_RC=0"
    echo "FINAL_STATUS=PASS"
    echo "PREBUILD_GATE=PASS"
    echo "BUILD_RC=0"
    echo "POSTBUILD_STAGE8N=PASS"
    echo "POSTBUILD_RUNTIME_CONTRACTS=PASS"
    echo "KERNEL_HEAD=$KERNEL_HEAD"
    echo "SEED_SHA256=$SEED_SHA256"
    echo "NO_ADB=YES"
    echo "NO_FASTBOOT=YES"
    echo "NO_FLASH=YES"
    echo "REPORT=$REPORT"
} | tee "$REPORT/STATUS.txt"

echo
echo "============================================================"
echo "TB8504_LOCAL_FINALIZATION=PASS"
echo "BUILD_COMPLETED=YES"
echo "NO_FLASH=YES"
echo "REPORT=$REPORT"
echo "============================================================"
exit 0
