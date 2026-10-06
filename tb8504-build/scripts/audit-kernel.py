#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import hashlib
import struct
import sys

BOOT_LIMIT = 67_108_864
MIN_KERNEL = 4 * 1024 * 1024
EXPECTED_DTB = "tb8504-msm8917-pmi8937-qrd-sku5.dtb"
EXPECTED_MODULES = {
    "ansi_cprng.ko",
    "backlight.ko",
    "br_netfilter.ko",
    "evbug.ko",
    "generic_bl.ko",
    "lcd.ko",
    "mmc_block_test.ko",
    "mmc_test.ko",
    "rdbg.ko",
    "test-iosched.ko",
    "ufs_test.ko",
    "wil6210.ko",
}
REQUIRED_CONFIG = (
    "CONFIG_MACH_LENOVO_TB8504=y",
    "CONFIG_ARCH_MSM8937=y",
    "CONFIG_EXT4_ENCRYPTION=y",
    "CONFIG_FS_ENCRYPTION=y",
    "CONFIG_KEYS=y",
    "CONFIG_CRYPTO_AES=y",
    "CONFIG_CRYPTO_XTS=y",
    "CONFIG_CRYPTO_CTS=y",
    "CONFIG_CRYPTO_CBC=y",
    "CONFIG_CRYPTO_SHA256=y",
    "CONFIG_MODULE_SIG=y",
    "CONFIG_MODULE_SIG_FORCE=y",
    "CONFIG_MODULE_SIG_ALL=y",
)

MODULE_SIG_MAGIC = b"~Module signature appended~\n"
MODULE_SIG_INFO_SIZE = 12


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_module_signature(data: bytes) -> bytes:
    if not data.endswith(MODULE_SIG_MAGIC):
        raise ValueError("module signature marker missing")

    info_end = len(data) - len(MODULE_SIG_MAGIC)
    info_start = info_end - MODULE_SIG_INFO_SIZE
    if info_start < 0:
        raise ValueError("module signature footer truncated")

    try:
        algo, digest, ident, signer_len, keyid_len, sig_len = struct.unpack(
            ">BBBBB3xI", data[info_start:info_end]
        )
    except struct.error as exc:
        raise ValueError(f"bad module signature footer: {exc}") from exc

    unsigned_end = info_start - signer_len - keyid_len - sig_len
    if unsigned_end <= 0:
        raise ValueError("invalid signed-module tail lengths")

    sig_start = info_start - sig_len
    if sig_len < 2 or sig_start < 0:
        raise ValueError("invalid module signature length")

    declared_rsa_len = struct.unpack(">H", data[sig_start:sig_start + 2])[0]
    if declared_rsa_len != sig_len - 2:
        raise ValueError(
            f"signature length mismatch footer={sig_len} rsa={declared_rsa_len}"
        )

    if algo != 1 or ident != 1:
        raise ValueError(f"unexpected signature metadata algo={algo} ident={ident}")

    return data[:unsigned_end]


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <artifact-dir>")
        return 2

    root = Path(sys.argv[1]).resolve()
    image = root / "Image.gz-dtb"
    dtb = root / EXPECTED_DTB
    config = root / "kernel.config"
    module_dir = root / "modules"
    modules = sorted(module_dir.glob("*.ko"))
    errors: list[str] = []

    if not image.is_file():
        errors.append("Image.gz-dtb missing")
    else:
        image_data = image.read_bytes()
        size = len(image_data)
        print(f"AUDIT_IMAGE_SIZE={size}")
        print(f"AUDIT_IMAGE_SHA256={sha256_bytes(image_data)}")
        if size < MIN_KERNEL:
            errors.append(f"Image.gz-dtb implausibly small: {size}")
        if size >= BOOT_LIMIT:
            errors.append(f"Image.gz-dtb exceeds 64MiB boot partition: {size}")

    if not dtb.is_file() or dtb.stat().st_size == 0:
        errors.append(f"DTB missing/empty: {EXPECTED_DTB}")
    else:
        dtb_data = dtb.read_bytes()
        print(f"AUDIT_DTB_SIZE={len(dtb_data)}")
        print(f"AUDIT_DTB_SHA256={sha256_bytes(dtb_data)}")
        if image.is_file():
            offset = image.read_bytes().find(dtb_data)
            print(f"AUDIT_EXPECTED_DTB_OFFSET={offset}")
            if offset < 0:
                errors.append("expected TB8504 DTB is not embedded in Image.gz-dtb")

    if not config.is_file():
        errors.append("kernel.config missing")
    else:
        cfg = set(config.read_text(errors="replace").splitlines())
        for required in REQUIRED_CONFIG:
            if required not in cfg:
                errors.append(f"config missing: {required}")
            else:
                print(f"AUDIT_CONFIG_OK={required}")

    actual_module_names = {p.name for p in modules}
    print(f"AUDIT_MODULE_COUNT={len(modules)}")
    print("AUDIT_MODULE_NAMES=" + ",".join(sorted(actual_module_names)))

    missing_modules = sorted(EXPECTED_MODULES - actual_module_names)
    extra_modules = sorted(actual_module_names - EXPECTED_MODULES)

    if missing_modules:
        errors.append("modules missing: " + ",".join(missing_modules))
    if extra_modules:
        errors.append("unexpected modules: " + ",".join(extra_modules))

    for module in modules:
        if module.stat().st_size == 0:
            errors.append(f"empty module: {module.name}")
            continue

        data = module.read_bytes()
        print(f"MODULE_SHA256={module.name}:{sha256_bytes(data)}")

        try:
            unsigned = strip_module_signature(data)
        except ValueError as exc:
            errors.append(f"{module.name}: {exc}")
            continue

        print(
            f"MODULE_UNSIGNED_SHA256={module.name}:{sha256_bytes(unsigned)}:"
            f"unsigned_size={len(unsigned)}"
        )

    if errors:
        for error in errors:
            print(f"AUDIT_FAIL={error}")
        print("KERNEL_ARTIFACT_AUDIT=FAIL")
        return 1

    print("KERNEL_ARTIFACT_AUDIT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
