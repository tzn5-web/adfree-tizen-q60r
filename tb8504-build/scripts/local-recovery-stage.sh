#!/usr/bin/env bash
# TB8504 Android 16 staged recovery build.
# Applies only GitHub-validated source transforms, reruns local audits,
# builds recoveryimage only, and audits the resulting recovery image.
# NO ADB / NO FASTBOOT / NO FLASH.

set -Eeo pipefail

ROOT="${1:-/home/dre/android16-tb8504/lineage-23.2}"
DEVICE="$ROOT/device/lenovo/TB8504"
VENDOR="$ROOT/vendor/lenovo/TB8504"
PRODUCT_OUT="$ROOT/out/target/product/TB8504"
TOOLING_REF="386d5aa733b726b3a14e16c04c2754aa1c2a9805"
REPO="tzn5-web/adfree-tizen-q60r"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$HOME/TB8504_RECOVERY_STAGE_$STAMP"
TOOLS="$REPORT/tools"

mkdir -p "$TOOLS"
exec > >(tee "$REPORT/FULL.log") 2>&1

fail() {
  local rc="$1"; shift
  echo "BLOCKER=$*"
  {
    echo "FINAL_RC=$rc"
    echo "FINAL_STATUS=BLOCKED"
    echo "NO_FLASH=YES"
    echo "REPORT=$REPORT"
  } > "$REPORT/STATUS.txt"
  exit "$rc"
}

echo "============================================================"
echo "TB8504 ANDROID 16 RECOVERY STAGE"
echo "NO ADB / NO FASTBOOT / NO FLASH"
echo "============================================================"
echo "ROOT=$ROOT"
echo "TOOLING_REF=$TOOLING_REF"
echo "REPORT=$REPORT"

for c in git curl python3 readelf sha256sum awk grep sed find stat bash; do
  command -v "$c" >/dev/null 2>&1 || fail 2 "missing command: $c"
done

for p in "$ROOT/build/envsetup.sh" "$DEVICE" "$VENDOR"; do
  [ -e "$p" ] || fail 3 "missing required path: $p"
done

fetch_tool() {
  local name="$1"
  curl -fsSL \
    "https://raw.githubusercontent.com/$REPO/$TOOLING_REF/tb8504-build/scripts/$name" \
    -o "$TOOLS/$name" || fail 10 "cannot fetch helper: $name"
  test -s "$TOOLS/$name" || fail 11 "empty helper: $name"
}

for f in \
  apply-residual-cleanup.py \
  audit-residual-contracts.py \
  apply-product-compat.py \
  audit-product-compat.py \
  audit-stage8n.py \
  audit-runtime-contracts.py; do
  fetch_tool "$f"
done

python3 -m py_compile "$TOOLS"/*.py || fail 12 "helper syntax failure"

echo
echo "=== APPLY RESIDUAL CLEANUP ==="
python3 "$TOOLS/apply-residual-cleanup.py" \
  --device "$DEVICE" \
  --vendor "$VENDOR" \
  --patch-out "$REPORT/residual-cleanup.patch" \
  --report-out "$REPORT/residual-cleanup.txt" \
  2>&1 | tee "$REPORT/residual-cleanup.log"
RC=\${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 20 "residual cleanup failed rc=$RC"

echo
echo "=== APPLY ANDROID 16 PRODUCT COMPAT ==="
python3 "$TOOLS/apply-product-compat.py" \
  --device "$DEVICE" \
  --patch-out "$REPORT/product-compat.patch" \
  --report-out "$REPORT/product-compat.txt" \
  2>&1 | tee "$REPORT/product-compat.log"
RC=\${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 21 "product compat migration failed rc=$RC"

echo
echo "=== SHELL SYNTAX ==="
for f in \
  "$DEVICE/rootdir/etc/init.class_main.sh" \
  "$DEVICE/rootdir/etc/init.qcom.post_boot.sh" \
  "$DEVICE/rootdir/etc/init.qcom.sensors.sh" \
  "$DEVICE/rootdir/etc/init.qcom.sh"; do
  echo "BASH_N=$f"
  bash -n "$f" || fail 22 "shell syntax failed: $f"
done

echo
echo "=== PRODUCT COMPAT AUDIT ==="
python3 "$TOOLS/audit-product-compat.py" --device "$DEVICE" \
  2>&1 | tee "$REPORT/product-compat-audit.log"
RC=\${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 23 "product compat audit failed rc=$RC"

echo
echo "=== RESIDUAL CONTRACT AUDIT ==="
python3 "$TOOLS/audit-residual-contracts.py" \
  --device "$DEVICE" --vendor "$VENDOR" \
  2>&1 | tee "$REPORT/residual-contracts.log"
RC=\${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 24 "residual contract audit failed rc=$RC"

echo
echo "=== ELF AUDIT ==="
python3 "$TOOLS/audit-stage8n.py" \
  --vendor "$VENDOR" \
  --device "$DEVICE" \
  --report-dir "$REPORT/elf" \
  2>&1 | tee "$REPORT/elf.log"
RC=\${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 25 "ELF audit failed rc=$RC"

MODULE_INFO="$PRODUCT_OUT/module-info.json"
[ -f "$MODULE_INFO" ] || fail 26 "module-info.json missing"

echo
echo "=== RUNTIME CONTRACT AUDIT ==="
python3 "$TOOLS/audit-runtime-contracts.py" \
  --vendor "$VENDOR" \
  --device "$DEVICE" \
  --report-dir "$REPORT/runtime" \
  --module-info "$MODULE_INFO" \
  2>&1 | tee "$REPORT/runtime.log"
RC=\${PIPESTATUS[0]}
[ "$RC" -eq 0 ] || fail 27 "runtime contract audit failed rc=$RC"

grep -Fx 'UNRESOLVED_EDGES=0' "$REPORT/elf.log" >/dev/null || fail 28 "ELF unresolved edges nonzero"
grep -Fx 'WRONG_BITNESS_EDGES=0' "$REPORT/elf.log" >/dev/null || fail 29 "ELF wrong-bitness edges nonzero"
grep -Fx 'RUNTIME_CONTRACT_FAILURES=0' "$REPORT/runtime.log" >/dev/null || fail 30 "runtime contract failures nonzero"
grep -Fx 'RESIDUAL_FAILURES=0' "$REPORT/residual-contracts.log" >/dev/null || fail 31 "residual failures nonzero"
grep -Fx 'PRODUCT_COMPAT_FAILURES=0' "$REPORT/product-compat-audit.log" >/dev/null || fail 32 "product compat failures nonzero"

echo
echo "PRE_RECOVERY_GATE=PASS"

cd "$ROOT"
set +u
# shellcheck disable=SC1091
source build/envsetup.sh || fail 40 "envsetup failed"

echo "LUNCH_PRODUCT=lineage_TB8504"
echo "LUNCH_RELEASE=trunk_staging"
echo "LUNCH_VARIANT=userdebug"

lunch lineage_TB8504 trunk_staging userdebug || fail 41 "lunch failed"

echo
echo "=== BUILD RECOVERYIMAGE ONLY ==="
set +e
mka recoveryimage 2>&1 | tee "$REPORT/BUILD.log"
BUILD_RC=\${PIPESTATUS[0]}
set -e
echo "BUILD_RC=$BUILD_RC" | tee "$REPORT/BUILD_STATUS.txt"
[ "$BUILD_RC" -eq 0 ] || fail 42 "recoveryimage build failed rc=$BUILD_RC"

RECOVERY="$PRODUCT_OUT/recovery.img"
[ -s "$RECOVERY" ] || fail 43 "recovery.img missing"

echo
echo "=== RECOVERY IMAGE AUDIT ==="
python3 - "$RECOVERY" "$ROOT" <<'PY' | tee "$REPORT/recovery-audit.log"
from pathlib import Path
import hashlib, struct, sys

img=Path(sys.argv[1])
root=Path(sys.argv[2])
data=img.read_bytes()
limit=67108864

def die(msg):
    print("RECOVERY_AUDIT=FAIL")
    print("FAIL="+msg)
    raise SystemExit(1)

if len(data) > limit:
    die(f"recovery size {len(data)} exceeds {limit}")
if data[:8] != b"ANDROID!":
    die("bad Android boot magic")
if len(data) < 48:
    die("truncated legacy boot header")

fields=struct.unpack_from("<9I", data, 8)
kernel_size,kernel_addr,ramdisk_size,ramdisk_addr,second_size,second_addr,tags_addr,page_size,header_version=fields

print(f"RECOVERY_SIZE={len(data)}")
print(f"RECOVERY_PARTITION_LIMIT={limit}")
print(f"RECOVERY_SHA256={hashlib.sha256(data).hexdigest()}")
print(f"KERNEL_SIZE={kernel_size}")
print(f"KERNEL_ADDR=0x{kernel_addr:08x}")
print(f"RAMDISK_SIZE={ramdisk_size}")
print(f"RAMDISK_ADDR=0x{ramdisk_addr:08x}")
print(f"SECOND_SIZE={second_size}")
print(f"TAGS_ADDR=0x{tags_addr:08x}")
print(f"PAGE_SIZE={page_size}")
print(f"HEADER_VERSION={header_version}")

expected={
    "kernel_addr":(kernel_addr,0x80008000),
    "ramdisk_addr":(ramdisk_addr,0x81000000),
    "tags_addr":(tags_addr,0x80000100),
    "page_size":(page_size,2048),
    "header_version":(header_version,0),
}
for k,(actual,wanted) in expected.items():
    if actual != wanted:
        die(f"{k} expected {wanted:#x} got {actual:#x}")
if kernel_size <= 0 or ramdisk_size <= 0 or second_size != 0:
    die("invalid recovery payload layout")

kernel_off=page_size
kernel=data[kernel_off:kernel_off+kernel_size]
kh=hashlib.sha256(kernel).hexdigest()
print(f"RECOVERY_KERNEL_SHA256={kh}")

candidates=[
    root/"out/target/product/TB8504/kernel",
    root/"out/target/product/TB8504/obj/KERNEL_OBJ/arch/arm64/boot/Image.gz-dtb",
]
match=False
for p in candidates:
    if p.is_file():
        h=hashlib.sha256(p.read_bytes()).hexdigest()
        print(f"KERNEL_CANDIDATE={p}")
        print(f"KERNEL_CANDIDATE_SHA256={h}")
        if h == kh:
            match=True
            print("RECOVERY_KERNEL_MATCH=YES")
if not match:
    die("recovery kernel does not match local freshly built kernel")

device=root/"device/lenovo/TB8504"
fstab=device/"rootdir/fstab.qcom"
rc=device/"rootdir/etc/init.recovery.qcom.rc"
for p in (fstab,rc):
    if not p.is_file():
        die(f"missing recovery source input: {p}")

ft=fstab.read_text("utf-8",errors="replace")
for mount in ("/system","/data","/misc","/persist","/cache","/vendor/dsp","/vendor/firmware_mnt","/boot","/recovery"):
    if mount not in ft:
        die(f"recovery fstab missing {mount}")

rt=rc.read_text("utf-8",errors="replace")
if "symlink /dev/block/platform/soc/\${ro.boot.bootdevice} /dev/block/bootdevice" not in rt:
    die("recovery bootdevice symlink contract missing")

print("RECOVERY_FSTAB_CONTRACT=PASS")
print("RECOVERY_INIT_CONTRACT=PASS")
print("RECOVERY_AUDIT=PASS")
PY
AUDIT_RC=\${PIPESTATUS[0]}
[ "$AUDIT_RC" -eq 0 ] || fail 44 "recovery image audit failed rc=$AUDIT_RC"

{
  echo "FINAL_RC=0"
  echo "FINAL_STATUS=PASS"
  echo "PRE_RECOVERY_GATE=PASS"
  echo "BUILD_RC=0"
  echo "RECOVERY_AUDIT=PASS"
  echo "NO_ADB=YES"
  echo "NO_FASTBOOT=YES"
  echo "NO_FLASH=YES"
  echo "REPORT=$REPORT"
} | tee "$REPORT/STATUS.txt"

echo
echo "============================================================"
echo "TB8504_RECOVERY_STAGE=PASS"
echo "NO_FLASH=YES"
echo "REPORT=$REPORT"
echo "============================================================"
