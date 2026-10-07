#!/usr/bin/env python3
"""Migrate TB8504 legacy product build identity overrides for Android 16.

Android 16 gen_build_prop validates PRODUCT_BUILD_PROP_OVERRIDES against the
Soong product-config keys. The old PRIVATE_BUILD_DESC/TARGET_DEVICE names are
no longer valid; their current equivalents are BuildDesc/DeviceName.

The stock BUILD_FINGERPRINT assignment remains unchanged.
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path

OLD = '''PRODUCT_BUILD_PROP_OVERRIDES += \\
    PRIVATE_BUILD_DESC="msm8937_64-user 8.1.0 OPM1.171019.019 7 release-keys" \\
    TARGET_DEVICE="TB-8504X"
'''

NEW = '''PRODUCT_BUILD_PROP_OVERRIDES += \\
    BuildDesc="msm8937_64-user 8.1.0 OPM1.171019.019 7 release-keys" \\
    DeviceName=TB-8504X
'''

FINGERPRINT = (
    "BUILD_FINGERPRINT := "
    "Lenovo/TB-8504X/TB-8504X:8.1.0/OPM1.171019.019/"
    "8504X_S001031_191204_ROW:user/release-keys"
)

def fail(msg: str) -> None:
    print(f"PRODUCT_COMPAT_FAIL={msg}")
    raise SystemExit(2)

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--patch-out", required=True, type=Path)
    ap.add_argument("--report-out", required=True, type=Path)
    args=ap.parse_args()

    device=args.device.resolve()
    path=device/"lineage_TB8504.mk"
    if not path.is_file():
        fail(f"missing {path}")

    original=path.read_text("utf-8", errors="replace")
    text=original

    if OLD in text:
        if text.count(OLD) != 1:
            fail("legacy product override block is ambiguous")
        text=text.replace(OLD, NEW)
        state="APPLIED"
    elif NEW in text:
        state="ALREADY_APPLIED"
    else:
        fail("neither expected legacy nor migrated product override block found")

    if FINGERPRINT not in text:
        fail("stock BUILD_FINGERPRINT changed or missing")

    forbidden=("PRIVATE_BUILD_DESC=", "TARGET_DEVICE=")
    for token in forbidden:
        if token in text:
            fail(f"legacy product override remains: {token}")

    for token in (
        'BuildDesc="msm8937_64-user 8.1.0 OPM1.171019.019 7 release-keys"',
        "DeviceName=TB-8504X",
    ):
        if token not in text:
            fail(f"required modern product override missing: {token}")

    if text != original:
        path.write_text(text, encoding="utf-8")

    before=original.splitlines(keepends=True)
    after=text.splitlines(keepends=True)
    patch="".join(difflib.unified_diff(
        before, after,
        fromfile="a/lineage_TB8504.mk",
        tofile="b/lineage_TB8504.mk",
        lineterm="\n",
    ))
    args.patch_out.parent.mkdir(parents=True, exist_ok=True)
    args.patch_out.write_text(patch, encoding="utf-8")

    report=[
        f"PRODUCT_COMPAT_STATE={state}",
        "LEGACY_PRIVATE_BUILD_DESC=ABSENT",
        "LEGACY_TARGET_DEVICE=ABSENT",
        "BUILD_DESC_OVERRIDE=PRESENT",
        "DEVICE_NAME_OVERRIDE=PRESENT",
        "STOCK_BUILD_FINGERPRINT=PRESENT",
        "PRODUCT_COMPAT=PASS",
    ]
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text("\n".join(report)+"\n", encoding="utf-8")
    print("\n".join(report))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
