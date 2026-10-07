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

    # Parse only the continued PRODUCT_BUILD_PROP_OVERRIDES assignment.
    # Stop at the first line without a trailing backslash so a following
    # standalone BUILD_FINGERPRINT assignment cannot be misclassified.
    keys=[]
    lines=text.splitlines()
    block_start=None
    for i, raw in enumerate(lines):
        if raw.strip().startswith("PRODUCT_BUILD_PROP_OVERRIDES") and "+=" in raw:
            block_start=i
            break
    if block_start is None:
        failures.append("PRODUCT_BUILD_PROP_OVERRIDES block missing")
    else:
        raw=lines[block_start]
        if not raw.rstrip().endswith("\\"):
            failures.append("PRODUCT_BUILD_PROP_OVERRIDES has no continued body")
        else:
            i=block_start+1
            while i < len(lines):
                raw=lines[i]
                item=raw.strip()
                if not item:
                    break
                continued=item.endswith("\\")
                item=item.rstrip("\\").strip()
                if "=" not in item:
                    failures.append(
                        f"malformed PRODUCT_BUILD_PROP_OVERRIDES item: {item}"
                    )
                    break
                key=item.split("=",1)[0].strip()
                keys.append(key)
                if key not in VALID_KEYS:
                    failures.append(
                        f"unrecognized Android 16 product override key: {key}"
                    )
                i += 1
                if not continued:
                    break

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
