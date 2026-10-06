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

git clone --no-tags --depth 1 --branch "$MKBOOTIMG_REF" "$MKBOOTIMG_REPO" "$TOOLS"
RC=$?
if [ "$RC" -ne 0 ]; then
    echo "BLOCKER=mkbootimg clone failed rc=$RC"
    exit 10
fi

ACTUAL_SHA="$(git -C "$TOOLS" rev-parse HEAD)"
echo "MKBOOTIMG_SHA=$ACTUAL_SHA"
if [ "$ACTUAL_SHA" != "$MKBOOTIMG_SHA" ]; then
    echo "BLOCKER=mkbootimg SHA mismatch"
    exit 11
fi

python3 "$TOOLS/unpack_bootimg.py"     --boot_img "$SEED"     --out "$SEED_OUT"     --format=mkbootimg     -0 > "$WORK/mkbootimg_args.bin"
RC=$?
if [ "$RC" -ne 0 ]; then
    echo "BLOCKER=seed unpack failed rc=$RC"
    exit 12
fi

python3 "$TOOLS/unpack_bootimg.py"     --boot_img "$SEED"     --out "$WORK/seed-info-files"     --format=info > "$REPORT/seed_boot_info.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
    echo "BLOCKER=seed boot info failed rc=$RC"
    exit 13
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
    echo "BLOCKER=unpacked mkbootimg arguments contain no kernel"
    exit 14
fi

python3 "$TOOLS/mkbootimg.py" "${ARGS[@]}" --output "$BOOT_IMG"
RC=$?
if [ "$RC" -ne 0 ]; then
    echo "BLOCKER=mkbootimg repack failed rc=$RC"
    exit 20
fi

SIZE="$(stat -c '%s' "$BOOT_IMG")"
echo "BOOT_IMG_SIZE=$SIZE"
if [ "$SIZE" -le 0 ] || [ "$SIZE" -gt "$BOOT_PARTITION_SIZE" ]; then
    echo "BLOCKER=boot.img invalid size=$SIZE limit=$BOOT_PARTITION_SIZE"
    exit 21
fi

python3 "$TOOLS/unpack_bootimg.py"     --boot_img "$BOOT_IMG"     --out "$NEW_OUT"     --format=info > "$REPORT/repacked_boot_info.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
    echo "BLOCKER=repacked boot unpack failed rc=$RC"
    exit 22
fi

# Re-unpack the new image into a clean directory for byte-for-byte component audit.
rm -rf "$NEW_OUT"
python3 "$TOOLS/unpack_bootimg.py"     --boot_img "$BOOT_IMG"     --out "$NEW_OUT"     --format=mkbootimg > "$REPORT/repacked_mkbootimg_args.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
    echo "BLOCKER=repacked component extraction failed rc=$RC"
    exit 23
fi

SEED_KERNEL_SHA="$(sha256sum "$SEED_OUT/kernel" | awk '{print $1}')"
CLOUD_KERNEL_SHA="$(sha256sum "$KERNEL" | awk '{print $1}')"
NEW_KERNEL_SHA="$(sha256sum "$NEW_OUT/kernel" | awk '{print $1}')"

echo "SEED_KERNEL_SHA256=$SEED_KERNEL_SHA"
echo "CLOUD_KERNEL_SHA256=$CLOUD_KERNEL_SHA"
echo "REPACKED_KERNEL_SHA256=$NEW_KERNEL_SHA"

if [ "$NEW_KERNEL_SHA" != "$CLOUD_KERNEL_SHA" ]; then
    echo "BLOCKER=repacked kernel does not match cloud kernel"
    exit 24
fi

for COMPONENT in ramdisk second recovery_dtbo dtb; do
    A="$SEED_OUT/$COMPONENT"
    B="$NEW_OUT/$COMPONENT"

    if [ -e "$A" ] || [ -e "$B" ]; then
        if [ ! -f "$A" ] || [ ! -f "$B" ]; then
            echo "BLOCKER=component presence changed: $COMPONENT"
            exit 25
        fi

        SHA_A="$(sha256sum "$A" | awk '{print $1}')"
        SHA_B="$(sha256sum "$B" | awk '{print $1}')"
        echo "COMPONENT=$COMPONENT SEED_SHA=$SHA_A NEW_SHA=$SHA_B"

        if [ "$SHA_A" != "$SHA_B" ]; then
            echo "BLOCKER=component changed unexpectedly: $COMPONENT"
            exit 26
        fi
    fi
done

# Header semantics must be unchanged except fields derived from kernel size/hash.
grep -vE '^(kernel_size:|boot magic:)' "$REPORT/seed_boot_info.txt"     > "$WORK/seed_info_filtered.txt"
grep -vE '^(kernel_size:|boot magic:)' "$REPORT/repacked_boot_info.txt"     > "$WORK/new_info_filtered.txt"

if ! diff -u "$WORK/seed_info_filtered.txt" "$WORK/new_info_filtered.txt"     > "$REPORT/header_semantics.diff"; then
    echo "BLOCKER=boot header semantics changed unexpectedly"
    cat "$REPORT/header_semantics.diff"
    exit 27
fi

{
    echo "BOOT_REPACK=PASS"
    echo "NO_FLASH=YES"
    echo "BOOT_IMG_SIZE=$SIZE"
    echo "BOOT_PARTITION_SIZE=$BOOT_PARTITION_SIZE"
    echo "SEED_BOOT_SHA256=$(sha256sum "$SEED" | awk '{print $1}')"
    echo "CLOUD_KERNEL_SHA256=$CLOUD_KERNEL_SHA"
    echo "BOOT_IMG_SHA256=$(sha256sum "$BOOT_IMG" | awk '{print $1}')"
    echo "MKBOOTIMG_SHA=$ACTUAL_SHA"
} | tee "$REPORT/SUMMARY.txt"

echo "FINAL_RC=0" > "$REPORT/STATUS.txt"
exit 0
