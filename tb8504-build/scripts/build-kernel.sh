#!/usr/bin/env bash

# GitHub Actions TB8504 kernel build.
# Build/audit only. Never flashes a device.

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

clone_exact() {
    local url="$1"
    local sha="$2"
    local dest="$3"
    local label="$4"

    git init -q "$dest" || return 1
    git -C "$dest" remote add origin "$url" || return 1
    git -C "$dest" fetch -q --depth 1 origin "$sha" || return 1
    git -C "$dest" checkout -q --detach FETCH_HEAD || return 1

    local actual
    actual="$(git -C "$dest" rev-parse HEAD 2>/dev/null)"
    echo "${label}_ACTUAL_SHA=$actual"

    [ "$actual" = "$sha" ]
}

echo "=== TB8504 CLOUD KERNEL BUILD ==="
echo "NO FLASH / NO DEVICE WRITE"
echo "KERNEL_REF=$KERNEL_REF"
echo "KERNEL_SHA=$KERNEL_SHA"
echo "TOOLCHAIN_REF=$TOOLCHAIN_REF"
echo "TOOLCHAIN_SHA=$TOOLCHAIN_SHA"
echo "DEFCONFIG=$DEFCONFIG"

{
    uname -a
    cat /etc/os-release 2>/dev/null || true
    python3 --version
    openssl version
} > "$REPORT_DIR/runner_environment.txt" 2>&1

clone_exact "$KERNEL_REPO" "$KERNEL_SHA" "$KERNEL_DIR" KERNEL
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 10 "exact kernel clone/checkout failed rc=$RC sha=$KERNEL_SHA"
    exit $?
fi

clone_exact "$TOOLCHAIN_REPO" "$TOOLCHAIN_SHA" "$TOOLCHAIN_DIR" TOOLCHAIN
RC=$?
if [ "$RC" -ne 0 ]; then
    fail 12 "exact toolchain clone/checkout failed rc=$RC sha=$TOOLCHAIN_SHA"
    exit $?
fi

ACTUAL_KERNEL_SHA="$(git -C "$KERNEL_DIR" rev-parse HEAD)"
ACTUAL_TOOLCHAIN_SHA="$(git -C "$TOOLCHAIN_DIR" rev-parse HEAD)"

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
ld --version | head -n 1 | tee -a "$REPORT_DIR/toolchain.txt"
openssl version | tee -a "$REPORT_DIR/toolchain.txt"

make -C "$KERNEL_DIR" O="$KERNEL_OUT" "$DEFCONFIG"
DEF_RC=$?
echo "DEFCONFIG_RC=$DEF_RC"
if [ "$DEF_RC" -ne 0 ]; then
    fail 20 "defconfig failed rc=$DEF_RC"
    exit $?
fi

CONFIG="$KERNEL_OUT/.config"
for REQUIRED in     'CONFIG_MACH_LENOVO_TB8504=y'     'CONFIG_ARCH_MSM8937=y'     'CONFIG_EXT4_ENCRYPTION=y'     'CONFIG_FS_ENCRYPTION=y'     'CONFIG_KEYS=y'     'CONFIG_CRYPTO_AES=y'     'CONFIG_CRYPTO_XTS=y'     'CONFIG_CRYPTO_CTS=y'     'CONFIG_CRYPTO_CBC=y'     'CONFIG_MODULE_SIG_FORCE=y'
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

if [ "$IMAGE_SIZE" -ge "$BOOT_PARTITION_SIZE" ]; then
    fail 34 "kernel payload alone exceeds boot partition"
    exit $?
fi

if [ "$MODULE_COUNT" -ne "$EXPECTED_MODULE_COUNT" ]; then
    fail 35 "module count mismatch expected=$EXPECTED_MODULE_COUNT actual=$MODULE_COUNT"
    exit $?
fi

KERNEL_RELEASE_FILE="$KERNEL_OUT/include/config/kernel.release"
if [ ! -s "$KERNEL_RELEASE_FILE" ]; then
    fail 36 "kernel.release missing from kernel build"
    exit $?
fi
KERNEL_RELEASE="$(cat "$KERNEL_RELEASE_FILE")"
if [ -z "$KERNEL_RELEASE" ]; then
    fail 36 "kernel.release is empty"
    exit $?
fi

SIGNING_CERT="$KERNEL_OUT/signing_key.x509"
if [ ! -s "$SIGNING_CERT" ]; then
    fail 36 "public module-signing certificate missing from kernel build"
    exit $?
fi

if [ -e "$KERNEL_OUT/signing_key.priv" ]; then
    echo "PRIVATE_SIGNING_KEY_PRESENT_IN_WORKSPACE=YES"
else
    fail 37 "kernel build did not retain expected private signing key in private workspace"
    exit $?
fi

cp -f "$IMAGE" "$ARTIFACT_DIR/Image.gz-dtb"
cp -f "$DTB" "$ARTIFACT_DIR/$EXPECTED_DTB"
cp -f "$CONFIG" "$ARTIFACT_DIR/kernel.config"
cp -f "$KERNEL_RELEASE_FILE" "$ARTIFACT_DIR/kernel.release"
cp -f "$SIGNING_CERT" "$ARTIFACT_DIR/module-signing.x509"
mkdir -p "$ARTIFACT_DIR/modules"
find "$MODULES_OUT" -type f -name '*.ko' -exec cp -f {} "$ARTIFACT_DIR/modules/" \;

python3 "$PROJECT_DIR/scripts/audit-kernel.py" "$ARTIFACT_DIR"     2>&1 | tee "$REPORT_DIR/kernel_artifact_audit.txt"
AUDIT_RC=${PIPESTATUS[0]}
echo "KERNEL_AUDIT_RC=$AUDIT_RC"
if [ "$AUDIT_RC" -ne 0 ]; then
    fail 40 "post-build audit failed rc=$AUDIT_RC"
    exit $?
fi

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
    echo "KERNEL_RELEASE=$KERNEL_RELEASE"
    echo "MODULE_SIGNING_CERT_SHA256=$(sha256sum "$ARTIFACT_DIR/module-signing.x509" | awk '{print $1}')"
    echo "PRIVATE_SIGNING_KEY_EXPORTED=NO"
    echo "BOOT_PARTITION_SIZE=$BOOT_PARTITION_SIZE"
    echo "FLASH_PERFORMED=NO"
    echo "KERNEL_AUDIT_RC=0"
    echo "FINAL_RC=0"
    sha256sum "$ARTIFACT_DIR/Image.gz-dtb"
    sha256sum "$ARTIFACT_DIR/$EXPECTED_DTB"
    grep '^MODULE_UNSIGNED_SHA256=' "$REPORT_DIR/kernel_artifact_audit.txt" || true
} | tee "$REPORT_DIR/SUMMARY.txt"

echo "TB8504_CLOUD_KERNEL=PASS"
echo "FINAL_RC=0" > "$REPORT_DIR/STATUS.txt"
exit 0
