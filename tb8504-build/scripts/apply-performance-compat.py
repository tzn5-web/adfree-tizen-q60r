#!/usr/bin/env python3
"""Idempotent TB8504 low-end graphics compatibility transform for Android 16."""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path


def fail(msg: str) -> None:
    print(f"PERFORMANCE_COMPAT_FAIL={msg}")
    raise SystemExit(2)


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--device",required=True,type=Path)
    ap.add_argument("--patch-out",required=True,type=Path)
    ap.add_argument("--report-out",required=True,type=Path)
    args=ap.parse_args()

    device=args.device.resolve()
    prop=device/"vendor_prop.mk"
    if not prop.is_file():
        fail(f"vendor_prop.mk missing: {prop}")

    before=prop.read_text("utf-8",errors="strict")
    after=before

    blur_on="    ro.surface_flinger.supports_background_blur=1 \\\n"
    blur_off="    ro.surface_flinger.supports_background_blur=0 \\\n"
    blur_on_count=after.count(blur_on)
    blur_off_count=after.count(blur_off)
    if blur_on_count==1 and blur_off_count==0:
        after=after.replace(blur_on,blur_off,1)
    elif blur_on_count==0 and blur_off_count==1:
        pass
    else:
        fail(
            "ambiguous supports_background_blur property "
            f"on={blur_on_count} off={blur_off_count}"
        )

    stale="    debug.sf.disable_backpressure=1 \\\n"
    stale_count=after.count(stale)
    if stale_count>1:
        fail(f"duplicate debug.sf.disable_backpressure entries: {stale_count}")
    if stale_count==1:
        after=after.replace(stale,"",1)

    required=(
        "    ro.surface_flinger.supports_background_blur=0 \\\n",
        "    ro.config.avoid_gfx_accel=true\n",
    )
    for token in required:
        if token not in after:
            # avoid_gfx_accel may be continued if no longer last item.
            if token.startswith("    ro.config.avoid_gfx_accel=") and (
                "    ro.config.avoid_gfx_accel=true \\\n" in after
            ):
                continue
            fail(f"required low-end graphics contract missing: {token.strip()}")

    if "debug.sf.disable_backpressure=" in after:
        fail("stale debug.sf.disable_backpressure property remains")
    if "ro.surface_flinger.supports_background_blur=1" in after:
        fail("background blur support remains enabled")

    changed=after!=before
    if changed:
        prop.write_text(after,encoding="utf-8")

    args.patch_out.parent.mkdir(parents=True,exist_ok=True)
    patch="".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/device/lenovo/TB8504/vendor_prop.mk",
        tofile="b/device/lenovo/TB8504/vendor_prop.mk",
    ))
    args.patch_out.write_text(patch,encoding="utf-8")

    state="APPLIED" if changed else "ALREADY_APPLIED"
    report=(
        f"PERFORMANCE_COMPAT_STATE={state}\n"
        f"CHANGED_FILES={1 if changed else 0}\n"
        "BACKGROUND_BLUR_SUPPORT=0\n"
        "STALE_DISABLE_BACKPRESSURE=0\n"
        "AVOID_GFX_ACCEL=true\n"
        "PERFORMANCE_COMPAT=PASS\n"
    )
    args.report_out.parent.mkdir(parents=True,exist_ok=True)
    args.report_out.write_text(report,encoding="utf-8")
    print(report,end="")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
