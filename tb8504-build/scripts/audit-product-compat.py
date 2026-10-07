#!/usr/bin/env python3
"""Audit TB8504 Android 16 product-build identity compatibility."""
from __future__ import annotations
import argparse, re
from pathlib import Path

VALID_KEYS = {
    "BuildDesc", "BuildFingerprint", "BuildId", "BuildNumber",
    "DeviceName", "DeviceProduct", "ProductBrand", "ProductManufacturer",
    "ProductModel", "SystemBrand", "SystemDevice", "SystemManufacturer",
    "SystemModel", "SystemName",
}

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    args=ap.parse_args()
    path=args.device.resolve()/"lineage_TB8504.mk"
    text=path.read_text("utf-8", errors="replace")

    failures=[]
    for token in ("PRIVATE_BUILD_DESC", "TARGET_DEVICE"):
        if re.search(rf"^\s*{re.escape(token)}\s*=", text, re.M):
            failures.append(f"legacy invalid PRODUCT_BUILD_PROP_OVERRIDES key remains: {token}")

    # Parse only the TB8504 override block to guard against another late
    # gen_build_prop failure.
    block_match=re.search(
        r"PRODUCT_BUILD_PROP_OVERRIDES\s*\+=\s*\\\n"
        r"(?P<body>(?:\s+[^\n]+(?:\\)?\n)+)",
        text,
    )
    keys=[]
    if not block_match:
        failures.append("PRODUCT_BUILD_PROP_OVERRIDES block missing")
    else:
        for raw in block_match.group("body").splitlines():
            item=raw.strip().rstrip("\\").strip()
            if not item or "=" not in item:
                continue
            key=item.split("=",1)[0].strip()
            keys.append(key)
            if key not in VALID_KEYS:
                failures.append(f"unrecognized Android 16 product override key: {key}")

    if "BuildDesc" not in keys:
        failures.append("BuildDesc override missing")
    if "DeviceName" not in keys:
        failures.append("DeviceName override missing")

    expected_fp=(
        "BUILD_FINGERPRINT := "
        "Lenovo/TB-8504X/TB-8504X:8.1.0/OPM1.171019.019/"
        "8504X_S001031_191204_ROW:user/release-keys"
    )
    if expected_fp not in text:
        failures.append("stock BUILD_FINGERPRINT missing or changed")

    print("=== TB8504 PRODUCT BUILD COMPAT AUDIT ===")
    print("PRODUCT_OVERRIDE_KEYS="+",".join(keys))
    print(f"PRODUCT_COMPAT_FAILURES={len(failures)}")
    for x in failures:
        print("FAIL="+x)
    if failures:
        print("TB8504_PRODUCT_COMPAT_AUDIT=NEEDS_REVIEW")
        return 1
    print("TB8504_PRODUCT_COMPAT_AUDIT=PASS")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
