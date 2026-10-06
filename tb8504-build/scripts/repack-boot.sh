#!/usr/bin/env bash

# Repack a proven Android 16 boot ramdisk with the cloud-built TB8504 kernel.
# No adb, no fastboot, no device writes.

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$PROJECT_DIR/config/boot-tools.env"

SEED="${1:-}"
KERNEL="${2:-}"
OUT_ROOT="${3:-$PROJECT_DIR/out/boot-repack}"

if [ -z "$SEED" ] || [ -z "$KERNEL" ]; then
    echo "usage: $0 <boot_seed.img> <Image.gz-dtb> [out-dir]"
    exit 2
fi

if [ ! -f "$SEED" ] || [ ! -f "$KERNEL" ]; then
    echo "BLOCKER=seed or kernel missing"
    exit 3
fi

rm -rf "$OUT_ROOT"
mkdir -p "$OUT_ROOT/work" "$OUT_ROOT/artifact/report"

WORK="$OUT_ROOT/work"
ART="$OUT_ROOT/artifact"
REPORT="$ART/report"
TOOLS="$WORK/mkbootimg"
SEED_OUT="$WORK/seed"
NEW_OUT="$WORK/repacked"
BOOT_IMG="$ART/boot.img"

exec > >(tee "$REPORT/FULL.log") 2>&1

fail() {
    local code="$1"
    shift
    echo "BLOCKER=$*"
    echo "FINAL_RC=$code" > "$REPORT/STATUS.txt"
    return "$code"
}

audit_legacy_header() {
    local image="$1"
    local label="$2"

    python3 - "$image" "$label" <<'PY_HEADER'
from pathlib import Path
import struct
import sys

path = Path(sys.argv[1])
label = sys.argv[2]
data = path.read_bytes()

if len(data) < 48 or data[:8] != b"ANDROID!":
    print(f"{label}_HEADER_FAIL=bad-magic-or-truncated")
    raise SystemExit(1)

fields = struct.unpack_from("<9I", data, 8)
(
    kernel_size,
    kernel_addr,
    ramdisk_size,
    ramdisk_addr,
    second_size,
    second_addr,
    tags_addr,
    page_size,
    header_version,
) = fields

print(f"{label}_HEADER_VERSION={header_version}")
print(f"{label}_PAGE_SIZE={page_size}")
print(f"{label}_KERNEL_SIZE={kernel_size}")
print(f"{label}_KERNEL_ADDR=0x{kernel_addr:08x}")
print(f"{label}_RAMDISK_SIZE={ramdisk_size}")
print(f"{label}_RAMDISK_ADDR=0x{ramdisk_addr:08x}")
print(f"{label}_SECOND_SIZE={second_size}")
print(f"{label}_TAGS_ADDR=0x{tags_addr:08x}")

expected = {
    "header_version": (header_version, 0),
    "page_size": (page_size, 2048),
    "kernel_addr": (kernel_addr, 0x80008000),
    "ramdisk_addr": (ramdisk_addr, 0x81000000),
    "tags_addr": (tags_addr, 0x80000100),
}

for key, (actual, wanted) in expected.items():
    if actual != wanted:
        print(f"{label}_HEADER_FAIL={key}:expected={wanted:#x}:actual={actual:#x}")
        raise SystemExit(2)

if kernel_size == 0 or ramdisk_size == 0 or second_size != 0:
    print(f"{label}_HEADER_FAIL=payload-layout")
    raise SystemExit(3)

print(f"{label}_HEADER_AUDIT=PASS")
PY_HEADER
}

audit_legacy_header "$SEED" SEED
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 4 "seed boot header/layout audit failed rc=$RC"
    exit $?
fi

git init -q "$TOOLS"
git -C "$TOOLS" remote add origin "$MKBOOTIMG_REPO"
git -C "$TOOLS" fetch -q --depth 1 origin "$MKBOOTIMG_SHA"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 10 "mkbootimg exact commit fetch failed rc=$RC sha=$MKBOOTIMG_SHA"
    exit $?
fi

git -C "$TOOLS" checkout -q --detach FETCH_HEAD
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 10 "mkbootimg exact commit checkout failed rc=$RC"
    exit $?
fi

ACTUAL_SHA="$(git -C "$TOOLS" rev-parse HEAD)"
echo "MKBOOTIMG_SHA=$ACTUAL_SHA"
if [ "$ACTUAL_SHA" != "$MKBOOTIMG_SHA" ]; then
    fail 11 "mkbootimg SHA mismatch expected=$MKBOOTIMG_SHA actual=$ACTUAL_SHA"
    exit $?
fi

python3 "$TOOLS/unpack_bootimg.py" --boot_img "$SEED" --out "$SEED_OUT" --format=mkbootimg -0 > "$WORK/mkbootimg_args.bin"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 12 "seed unpack failed rc=$RC"
    exit $?
fi

python3 "$TOOLS/unpack_bootimg.py" --boot_img "$SEED" --out "$WORK/seed-info-files" --format=info > "$REPORT/seed_boot_info.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 13 "seed boot info failed rc=$RC"
    exit $?
fi

declare -a ARGS=()
while IFS= read -r -d '' ARG; do
    ARGS+=("$ARG")
done < "$WORK/mkbootimg_args.bin"

KERNEL_REPLACED=0
for ((I=0; I<${#ARGS[@]}; I++)); do
    if [ "${ARGS[$I]}" = "--kernel" ]; then
        NEXT=$((I + 1))
        ARGS[$NEXT]="$KERNEL"
        KERNEL_REPLACED=1
        break
    fi
done

if [ "$KERNEL_REPLACED" -ne 1 ]; then
    fail 14 "unpacked mkbootimg arguments contain no kernel"
    exit $?
fi

python3 "$TOOLS/mkbootimg.py" "${ARGS[@]}" --output "$BOOT_IMG"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 20 "mkbootimg repack failed rc=$RC"
    exit $?
fi

SIZE="$(stat -c '%s' "$BOOT_IMG")"
echo "BOOT_IMG_SIZE=$SIZE"
if [ "$SIZE" -le 0 ] || [ "$SIZE" -gt "$BOOT_PARTITION_SIZE" ]; then
    fail 21 "boot.img invalid size=$SIZE limit=$BOOT_PARTITION_SIZE"
    exit $?
fi

audit_legacy_header "$BOOT_IMG" REPACKED
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 22 "repacked boot header/layout audit failed rc=$RC"
    exit $?
fi

python3 "$TOOLS/unpack_bootimg.py" --boot_img "$BOOT_IMG" --out "$NEW_OUT" --format=info > "$REPORT/repacked_boot_info.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 23 "repacked boot unpack failed rc=$RC"
    exit $?
fi

rm -rf "$NEW_OUT"
python3 "$TOOLS/unpack_bootimg.py" --boot_img "$BOOT_IMG" --out "$NEW_OUT" --format=mkbootimg > "$REPORT/repacked_mkbootimg_args.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 24 "repacked component extraction failed rc=$RC"
    exit $?
fi

SEED_KERNEL_SHA="$(sha256sum "$SEED_OUT/kernel" | awk '{print $1}')"
CLOUD_KERNEL_SHA="$(sha256sum "$KERNEL" | awk '{print $1}')"
NEW_KERNEL_SHA="$(sha256sum "$NEW_OUT/kernel" | awk '{print $1}')"

echo "SEED_KERNEL_SHA256=$SEED_KERNEL_SHA"
echo "CLOUD_KERNEL_SHA256=$CLOUD_KERNEL_SHA"
echo "REPACKED_KERNEL_SHA256=$NEW_KERNEL_SHA"

if [ "$NEW_KERNEL_SHA" != "$CLOUD_KERNEL_SHA" ]; then
    fail 25 "repacked kernel does not match cloud kernel"
    exit $?
fi

for COMPONENT in ramdisk second recovery_dtbo dtb; do
    A="$SEED_OUT/$COMPONENT"
    B="$NEW_OUT/$COMPONENT"

    if [ -e "$A" ] || [ -e "$B" ]; then
        if [ ! -f "$A" ] || [ ! -f "$B" ]; then
            fail 26 "component presence changed: $COMPONENT"
            exit $?
        fi

        SHA_A="$(sha256sum "$A" | awk '{print $1}')"
        SHA_B="$(sha256sum "$B" | awk '{print $1}')"
        echo "COMPONENT=$COMPONENT SEED_SHA=$SHA_A NEW_SHA=$SHA_B"

        if [ "$SHA_A" != "$SHA_B" ]; then
            fail 27 "component changed unexpectedly: $COMPONENT"
            exit $?
        fi
    fi
done

# v0 header: kernel size/hash may change; all user-visible header semantics must not.
grep -vE '^(kernel_size:|boot magic:)' "$REPORT/seed_boot_info.txt" > "$WORK/seed_info_filtered.txt"
grep -vE '^(kernel_size:|boot magic:)' "$REPORT/repacked_boot_info.txt" > "$WORK/new_info_filtered.txt"

if ! diff -u "$WORK/seed_info_filtered.txt" "$WORK/new_info_filtered.txt" > "$REPORT/header_semantics.diff"; then
    cat "$REPORT/header_semantics.diff"
    fail 28 "boot header semantics changed unexpectedly"
    exit $?
fi

{
    echo "BOOT_REPACK=PASS"
    echo "NO_FLASH=YES"
    echo "BOOT_HEADER_VERSION=0"
    echo "BOOT_PAGE_SIZE=2048"
    echo "BOOT_IMG_SIZE=$SIZE"
    echo "BOOT_PARTITION_SIZE=$BOOT_PARTITION_SIZE"
    echo "SEED_BOOT_SHA256=$(sha256sum "$SEED" | awk '{print $1}')"
    echo "SEED_KERNEL_SHA256=$SEED_KERNEL_SHA"
    echo "CLOUD_KERNEL_SHA256=$CLOUD_KERNEL_SHA"
    echo "BOOT_IMG_SHA256=$(sha256sum "$BOOT_IMG" | awk '{print $1}')"
    echo "MKBOOTIMG_SHA=$ACTUAL_SHA"
    echo "FINAL_RC=0"
} | tee "$REPORT/SUMMARY.txt"

echo "FINAL_RC=0" > "$REPORT/STATUS.txt"
exit 0
