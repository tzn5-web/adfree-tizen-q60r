#!/usr/bin/env python3
"""Audit TB8504 low-end graphics properties required for Android 16."""
from __future__ import annotations

import argparse
from pathlib import Path


def fail(msg: str) -> None:
    print(f"PERFORMANCE_AUDIT_FAIL={msg}")
    raise SystemExit(2)


def count_prop(text: str, key: str) -> tuple[int,list[str]]:
    rows=[]
    for raw in text.splitlines():
        s=raw.strip().rstrip("\\").strip()
        if s.startswith(key+"="):
            rows.append(s)
    return len(rows),rows


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--device",required=True,type=Path)
    args=ap.parse_args()
    prop=args.device.resolve()/"vendor_prop.mk"
    if not prop.is_file():
        fail(f"vendor_prop.mk missing: {prop}")
    text=prop.read_text("utf-8",errors="replace")

    wanted={
        "ro.surface_flinger.supports_background_blur":"0",
        "ro.config.avoid_gfx_accel":"true",
    }
    for key,value in wanted.items():
        count,rows=count_prop(text,key)
        if count!=1 or rows[0] != f"{key}={value}":
            fail(f"{key} expected exactly {value}; found {rows}")
        print(f"PERFORMANCE_PROP_OK={key}={value}")

    stale=("debug.sf.disable_backpressure",)
    for key in stale:
        count,rows=count_prop(text,key)
        if count:
            fail(f"stale Android graphics property remains: {rows}")
        print(f"PERFORMANCE_PROP_ABSENT={key}")

    # These legacy Qualcomm settings existed already in the Android-10-era
    # tree. Keep them unchanged until device-side frame timing proves a reason
    # to alter them.
    for key in ("debug.egl.hw","debug.sf.hw","debug.hwui.use_buffer_age"):
        count,rows=count_prop(text,key)
        if count!=1:
            fail(f"expected inherited graphics property missing/duplicated: {key}")
        print(f"INHERITED_GRAPHICS_PROP={rows[0]}")

    print("TB8504_PERFORMANCE_SOURCE_AUDIT=PASS")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
