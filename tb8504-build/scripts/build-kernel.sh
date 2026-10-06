#!/usr/bin/env bash

# GitHub Actions TB8504 kernel build.
# This builds and audits only. It never flashes a device.

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck disable=SC1091
source "$PROJECT_DIR/config/sources.env"

WORK_DIR="$PROJECT_DIR/out/work"
ARTIFACT_DIR="$PROJECT_DIR/out/artifact"
REPORT_DIR="$ARTIFACT_DIR/report"
KERNEL_DIR="$WORK_DIR/kernel"
TOOLCHAIN_DIR="$WORK_DIR/toolchain"
KERNEL_OUT="$WORK_DIR/kernel-out"
MODULES_OUT="$WORK_DIR/modules"

rm -rf "$PROJECT_DIR/out"
mkdir -p "$WORK_DIR" "$ARTIFACT_DIR" "$REPORT_DIR" "$MODULES_OUT"

exec > >(tee "$REPORT_DIR/FULL.log") 2>&1

fail() {
    local code="$1"
    shift
    echo "BLOCKER=$*"
    echo "FINAL_RC=$code" > "$REPORT_DIR/STATUS.txt"
    return "$code"
}

echo "=== TB8504 CLOUD KERNEL BUILD ==="
echo "NO FLASH / NO DEVICE WRITE"
echo "KERNEL_SHA=$KERNEL_SHA"
echo "TOOLCHAIN_SHA=$TOOLCHAIN_SHA"
echo "DEFCONFIG=$DEFCONFIG"

git clone --no-tags --depth 1 --branch "$KERNEL_REF" "$KERNEL_REPO" "$KERNEL_DIR"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 10 "kernel clone failed rc=$RC"
    exit $?
fi

ACTUAL_KERNEL_SHA="$(git -C "$KERNEL_DIR" rev-parse HEAD)"
echo "ACTUAL_KERNEL_SHA=$ACTUAL_KERNEL_SHA"
if [ "$ACTUAL_KERNEL_SHA" != "$KERNEL_SHA" ]; then
    fail 11 "kernel SHA mismatch expected=$KERNEL_SHA actual=$ACTUAL_KERNEL_SHA"
    exit $?
fi

git clone --no-tags --depth 1 --branch "$TOOLCHAIN_REF" "$TOOLCHAIN_REPO" "$TOOLCHAIN_DIR"
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 12 "toolchain clone failed rc=$RC"
    exit $?
fi

ACTUAL_TOOLCHAIN_SHA="$(git -C "$TOOLCHAIN_DIR" rev-parse HEAD)"
echo "ACTUAL_TOOLCHAIN_SHA=$ACTUAL_TOOLCHAIN_SHA"
if [ "$ACTUAL_TOOLCHAIN_SHA" != "$TOOLCHAIN_SHA" ]; then
    fail 13 "toolchain SHA mismatch expected=$TOOLCHAIN_SHA actual=$ACTUAL_TOOLCHAIN_SHA"
    exit $?
fi

CROSS_COMPILE="$TOOLCHAIN_DIR/bin/aarch64-linux-android-"
if [ ! -x "${CROSS_COMPILE}gcc" ]; then
    fail 14 "aarch64 GCC missing"
    exit $?
fi

export ARCH=arm64
export SUBARCH=arm64
export CROSS_COMPILE
export KBUILD_BUILD_USER=tb8504-cloud
export KBUILD_BUILD_HOST=github-actions
export KBUILD_BUILD_TIMESTAMP="Tue Oct 06 12:00:00 UTC 2026"

"${CROSS_COMPILE}gcc" --version | head -n 1 | tee "$REPORT_DIR/toolchain.txt"
gcc --version | head -n 1 | tee -a "$REPORT_DIR/toolchain.txt"
ld.lld --version | head -n 1 | tee -a "$REPORT_DIR/toolchain.txt"

make -C "$KERNEL_DIR" O="$KERNEL_OUT" "$DEFCONFIG"
DEF_RC=$?
echo "DEFCONFIG_RC=$DEF_RC"
if [ "$DEF_RC" -ne 0 ]; then
    fail 20 "defconfig failed rc=$DEF_RC"
    exit $?
fi

CONFIG="$KERNEL_OUT/.config"
for REQUIRED in     'CONFIG_MACH_LENOVO_TB8504=y'     'CONFIG_ARCH_MSM8937=y'     'CONFIG_EXT4_ENCRYPTION=y'     'CONFIG_FS_ENCRYPTION=y'
do
    if ! grep -Fxq "$REQUIRED" "$CONFIG"; then
        fail 21 "required kernel config missing: $REQUIRED"
        exit $?
    fi
    echo "CONFIG_OK=$REQUIRED"
done

JOBS="${JOBS:-2}"
make -C "$KERNEL_DIR" O="$KERNEL_OUT" -j"$JOBS" Image.gz-dtb dtbs modules
BUILD_RC=$?
echo "KERNEL_BUILD_RC=$BUILD_RC"
if [ "$BUILD_RC" -ne 0 ]; then
    fail 30 "kernel build failed rc=$BUILD_RC"
    exit $?
fi

make -C "$KERNEL_DIR" O="$KERNEL_OUT"     INSTALL_MOD_PATH="$MODULES_OUT" modules_install
MOD_RC=$?
echo "MODULES_INSTALL_RC=$MOD_RC"
if [ "$MOD_RC" -ne 0 ]; then
    fail 31 "modules_install failed rc=$MOD_RC"
    exit $?
fi

IMAGE="$KERNEL_OUT/arch/arm64/boot/Image.gz-dtb"
if [ ! -s "$IMAGE" ]; then
    fail 32 "Image.gz-dtb missing or empty"
    exit $?
fi

DTB="$(find "$KERNEL_OUT/arch/arm64/boot/dts" -type f -name "$EXPECTED_DTB" -print -quit 2>/dev/null)"
if [ -z "$DTB" ] || [ ! -s "$DTB" ]; then
    fail 33 "expected TB8504 DTB missing: $EXPECTED_DTB"
    exit $?
fi

IMAGE_SIZE="$(stat -c '%s' "$IMAGE")"
DTB_SIZE="$(stat -c '%s' "$DTB")"
MODULE_COUNT="$(find "$MODULES_OUT" -type f -name '*.ko' | wc -l)"

echo "IMAGE_GZ_DTB_SIZE=$IMAGE_SIZE"
echo "DTB_SIZE=$DTB_SIZE"
echo "MODULE_COUNT=$MODULE_COUNT"

# Kernel payload is not boot.img, but it must still leave room for ramdisk/header.
if [ "$IMAGE_SIZE" -ge "$BOOT_PARTITION_SIZE" ]; then
    fail 34 "kernel payload alone exceeds boot partition"
    exit $?
fi

if [ "$MODULE_COUNT" -lt 1 ]; then
    fail 35 "no kernel modules produced"
    exit $?
fi

cp -f "$IMAGE" "$ARTIFACT_DIR/Image.gz-dtb"
cp -f "$DTB" "$ARTIFACT_DIR/$EXPECTED_DTB"
cp -f "$CONFIG" "$ARTIFACT_DIR/kernel.config"
mkdir -p "$ARTIFACT_DIR/modules"
find "$MODULES_OUT" -type f -name '*.ko' -exec cp -f {} "$ARTIFACT_DIR/modules/" \;

{
    echo "KERNEL_REPO=$KERNEL_REPO"
    echo "KERNEL_REF=$KERNEL_REF"
    echo "KERNEL_SHA=$ACTUAL_KERNEL_SHA"
    echo "TOOLCHAIN_REPO=$TOOLCHAIN_REPO"
    echo "TOOLCHAIN_REF=$TOOLCHAIN_REF"
    echo "TOOLCHAIN_SHA=$ACTUAL_TOOLCHAIN_SHA"
    echo "DEFCONFIG=$DEFCONFIG"
    echo "IMAGE_GZ_DTB_SIZE=$IMAGE_SIZE"
    echo "DTB=$EXPECTED_DTB"
    echo "DTB_SIZE=$DTB_SIZE"
    echo "MODULE_COUNT=$MODULE_COUNT"
    echo "BOOT_PARTITION_SIZE=$BOOT_PARTITION_SIZE"
    echo "FLASH_PERFORMED=NO"
    echo "FINAL_RC=0"
    sha256sum "$ARTIFACT_DIR/Image.gz-dtb"
    sha256sum "$ARTIFACT_DIR/$EXPECTED_DTB"
    find "$ARTIFACT_DIR/modules" -type f -name '*.ko' -print0 | sort -z | xargs -0 -r sha256sum
} | tee "$REPORT_DIR/SUMMARY.txt"

python3 "$PROJECT_DIR/scripts/audit-kernel.py" "$ARTIFACT_DIR"
AUDIT_RC=$?
echo "KERNEL_AUDIT_RC=$AUDIT_RC"
if [ "$AUDIT_RC" -ne 0 ]; then
    fail 40 "post-build audit failed rc=$AUDIT_RC"
    exit $?
fi

echo "TB8504_CLOUD_KERNEL=PASS"
echo "FINAL_RC=0" > "$REPORT_DIR/STATUS.txt"
exit 0
