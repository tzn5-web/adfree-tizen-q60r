#!/usr/bin/env bash

# Historical/support runner. Normal operation must use tb8504-autopilot.py.
if [ "${TB8504_ALLOW_LEGACY_RUNNER:-0}" != "1" ]; then
    echo "LEGACY_RUNNER_LOCKED=YES" >&2
    echo "Use tb8504-build/scripts/tb8504-autopilot.py as the single supported entry point." >&2
    exit 64
fi
# TB8504 staged local image build: boot OR recovery only.
# Requires the pre-recovery convergence gate to have been applied first.
# NO ADB / NO FASTBOOT / NO FLASH.
set -Eeo pipefail

KIND="${1:-}"
ROOT="${2:-/home/dre/android16-tb8504/lineage-23.2}"
case "$KIND" in
  boot|recovery) ;;
  *) echo "usage: $0 <boot|recovery> [android-root]"; exit 2 ;;
esac

DEVICE="$ROOT/device/lenovo/TB8504"
OUT="$ROOT/out/target/product/TB8504"
REPO="tzn5-web/adfree-tizen-q60r"
TOOLING_REF="66cbb6e641868efe706d5b38e733eead67f1797f"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/TB8504_${KIND^^}_STAGE_${STAMP}"
mkdir -p "$REPORT"
exec > >(tee "$REPORT/FULL.log") 2>&1

fail() {
  local rc="$1"; shift
  echo "BLOCKER=$*"
  {
    echo "FINAL_RC=$rc"
    echo "IMAGE_KIND=$KIND"
    echo "STAGE_STATUS=BLOCKED"
    echo "NO_FLASH=YES"
    echo "REPORT=$REPORT"
  } > "$REPORT/STATUS.txt"
  exit "$rc"
}

echo "============================================================"
echo "TB8504 STAGED IMAGE BUILD: $KIND"
echo "NO ADB / NO FASTBOOT / NO FLASH"
echo "============================================================"

for p in "$ROOT/build/envsetup.sh" "$DEVICE"; do
  [ -e "$p" ] || fail 3 "missing required path: $p"
done

# Refuse to build from the known legacy product-property state.
python3 - "$DEVICE/lineage_TB8504.mk" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])
t=p.read_text("utf-8",errors="replace")
bad=[x for x in ("PRIVATE_BUILD_DESC=", "TARGET_DEVICE=") if x in t]
need=("BuildDesc=", "DeviceName=TB-8504X")
missing=[x for x in need if x not in t]
if bad or missing:
    print("PRE_IMAGE_PRODUCT_COMPAT=FAIL")
    print("BAD="+",".join(bad))
    print("MISSING="+",".join(missing))
    raise SystemExit(1)
print("PRE_IMAGE_PRODUCT_COMPAT=PASS")
PY
[ "$?" -eq 0 ] || fail 4 "run local-pre-recovery gate first"

cd "$ROOT"
set +u
# shellcheck disable=SC1091
source build/envsetup.sh || fail 10 "envsetup failed"

lunch lineage_TB8504 trunk_staging userdebug || fail 11 "Android 16 lunch failed"

{
  echo "TARGET_PRODUCT=${TARGET_PRODUCT:-}"
  echo "TARGET_RELEASE=${TARGET_RELEASE:-}"
  echo "TARGET_VARIANT=${TARGET_BUILD_VARIANT:-}"
  echo "PLATFORM_VERSION=$(get_build_var PLATFORM_VERSION)"
  echo "PLATFORM_SDK_VERSION=$(get_build_var PLATFORM_SDK_VERSION)"
  echo "LINEAGE_VERSION=$(get_build_var LINEAGE_VERSION)"
  echo "BUILD_ID=$(get_build_var BUILD_ID)"
} | tee "$REPORT/release.txt"

grep -Fx 'TARGET_PRODUCT=lineage_TB8504' "$REPORT/release.txt" >/dev/null ||
  fail 12 "wrong product"
grep -Fx 'TARGET_RELEASE=trunk_staging' "$REPORT/release.txt" >/dev/null ||
  fail 13 "wrong release"
grep -Fx 'TARGET_VARIANT=userdebug' "$REPORT/release.txt" >/dev/null ||
  fail 14 "wrong variant"
grep -Fx 'PLATFORM_SDK_VERSION=36' "$REPORT/release.txt" >/dev/null ||
  fail 15 "not Android 16 SDK 36"
grep -E '^PLATFORM_VERSION=16([.]|$)' "$REPORT/release.txt" >/dev/null ||
  fail 16 "platform version is not Android 16"
grep -E '^LINEAGE_VERSION=23[.]2-' "$REPORT/release.txt" >/dev/null ||
  fail 17 "Lineage version is not 23.2"

TARGET="${KIND}image"
echo "BUILD_TARGET=$TARGET"
set +e
mka "$TARGET" 2>&1 | tee "$REPORT/BUILD.log"
BUILD_RC=${PIPESTATUS[0]}
set -e
echo "BUILD_RC=$BUILD_RC" | tee "$REPORT/BUILD_STATUS.txt"
[ "$BUILD_RC" -eq 0 ] || fail 20 "$TARGET build failed rc=$BUILD_RC"

IMAGE="$OUT/$KIND.img"
[ -s "$IMAGE" ] || fail 21 "missing image after successful build: $IMAGE"

AUDITOR="$REPORT/audit-local-image.py"
curl -fsSL   "https://raw.githubusercontent.com/$REPO/$TOOLING_REF/tb8504-build/scripts/audit-local-image.py"   -o "$AUDITOR" || fail 30 "cannot fetch pinned image auditor"
python3 -m py_compile "$AUDITOR" || fail 31 "image auditor syntax failure"

echo
echo "=== IMAGE AUDIT ==="
python3 "$AUDITOR"   --root "$ROOT"   --kind "$KIND"   --release-report "$REPORT/release.txt"   --image "$IMAGE"   2>&1 | tee "$REPORT/IMAGE_AUDIT.log"
AUDIT_RC=${PIPESTATUS[0]}
[ "$AUDIT_RC" -eq 0 ] || fail 32 "$KIND image audit failed rc=$AUDIT_RC"

sha256sum "$IMAGE" | tee "$REPORT/IMAGE.sha256"
stat -c 'IMAGE_SIZE=%s' "$IMAGE" | tee "$REPORT/IMAGE.size"

{
  echo "FINAL_RC=0"
  echo "IMAGE_KIND=$KIND"
  echo "STAGE_STATUS=PASS"
  echo "BUILD_RC=0"
  echo "IMAGE_AUDIT=PASS"
  echo "NO_ADB=YES"
  echo "NO_FASTBOOT=YES"
  echo "NO_FLASH=YES"
  echo "IMAGE=$IMAGE"
  echo "REPORT=$REPORT"
} | tee "$REPORT/STATUS.txt"

echo "============================================================"
echo "TB8504_${KIND^^}_STAGE=PASS"
echo "NO_FLASH=YES"
echo "REPORT=$REPORT"
echo "============================================================"
