#!/usr/bin/env python3
from pathlib import Path
import hashlib
import sys

BOOT_LIMIT = 67_108_864
MIN_KERNEL = 4 * 1024 * 1024
EXPECTED_DTB = "tb8504-msm8917-pmi8937-qrd-sku5.dtb"
REQUIRED_CONFIG = (
    "CONFIG_MACH_LENOVO_TB8504=y",
    "CONFIG_ARCH_MSM8937=y",
    "CONFIG_EXT4_ENCRYPTION=y",
    "CONFIG_FS_ENCRYPTION=y",
)

root = Path(sys.argv[1]).resolve()
image = root / "Image.gz-dtb"
dtb = root / EXPECTED_DTB
config = root / "kernel.config"
modules = sorted((root / "modules").glob("*.ko"))

errors = []

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

if not image.is_file():
    errors.append("Image.gz-dtb missing")
else:
    size = image.stat().st_size
    print(f"AUDIT_IMAGE_SIZE={size}")
    print(f"AUDIT_IMAGE_SHA256={sha256(image)}")
    if size < MIN_KERNEL:
        errors.append(f"Image.gz-dtb implausibly small: {size}")
    if size >= BOOT_LIMIT:
        errors.append(f"Image.gz-dtb exceeds 64MiB boot partition: {size}")

if not dtb.is_file() or dtb.stat().st_size == 0:
    errors.append(f"DTB missing/empty: {EXPECTED_DTB}")
else:
    print(f"AUDIT_DTB_SIZE={dtb.stat().st_size}")
    print(f"AUDIT_DTB_SHA256={sha256(dtb)}")

if not config.is_file():
    errors.append("kernel.config missing")
else:
    cfg = set(config.read_text(errors="replace").splitlines())
    for required in REQUIRED_CONFIG:
        if required not in cfg:
            errors.append(f"config missing: {required}")
        else:
            print(f"AUDIT_CONFIG_OK={required}")

print(f"AUDIT_MODULE_COUNT={len(modules)}")
if not modules:
    errors.append("no modules in artifact")

for module in modules:
    if module.stat().st_size == 0:
        errors.append(f"empty module: {module.name}")

if errors:
    for error in errors:
        print(f"AUDIT_FAIL={error}")
    print("KERNEL_ARTIFACT_AUDIT=FAIL")
    sys.exit(1)

print("KERNEL_ARTIFACT_AUDIT=PASS")
sys.exit(0)
