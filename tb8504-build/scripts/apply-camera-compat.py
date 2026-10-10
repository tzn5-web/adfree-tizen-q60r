#!/usr/bin/env python3
"""Guard two Qualcomm-only HAL1 callbacks in the existing VANILLA_HAL build.

Android 16 has no CAMERA_MSG_META_DATA extension. Other callbacks in this
Qualcomm HAL already use #ifndef VANILLA_HAL; the OTP and dual-camera paths
missed that guard. Preserve their code for non-vanilla builds and leave all
standard preview, capture and face-detection callbacks unchanged.
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"CAMERA_COMPAT_FAIL={message}")


def transform(text: str) -> str:
    otp_start = "    if ( msgTypeEnabled(CAMERA_MSG_META_DATA) && mCameraId == 0 ) {\n"
    otp_end = '    LOGI("X rc = %d", rc);\n'
    dual_start = "    cam_dimension_t dim;\n\n    if ( msgTypeEnabled(CAMERA_MSG_META_DATA) ) {\n"
    function = "int32_t QCamera2HardwareInterface::processDualCameraUpdate("
    if text.count(otp_start) != 1 or text.count(function) != 1:
        fail("unexpected OTP or dual-camera source layout")

    start = text.index(otp_start)
    end = text.index(otp_end, start)
    block = text[start:end]
    if block.count("CAMERA_MSG_META_DATA") != 2:
        fail("unexpected OTP callback block")
    guard = "#ifndef VANILLA_HAL\n"
    guarded = text[max(0, start-len(guard)):start] == guard
    if guarded:
        if not block.endswith("#endif\n"):
            fail("OTP guard has no matching end")
    else:
        if not block.endswith(("    }\n", "    }\n\n")):
            fail("unexpected OTP callback boundary")
        text = text[:start] + guard + block + "#endif\n" + text[end:]

    function_start = text.index(function)
    start = text.index(dual_start, function_start)
    end = text.index("    return rc;\n}\n", start)
    block = text[start:end]
    if block.count("CAMERA_MSG_META_DATA") != 2:
        fail("unexpected dual-camera callback block")
    guarded = text[max(0, start-len(guard)):start] == guard
    if guarded:
        if not block.endswith("#endif\n"):
            fail("dual-camera guard has no matching end")
    else:
        text = text[:start] + guard + block + "#endif\n" + text[end:]
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--patch-out", required=True, type=Path)
    ap.add_argument("--report-out", required=True, type=Path)
    args = ap.parse_args()
    device = args.device.resolve()
    camera = device / "camera/QCamera2"
    if "-DVANILLA_HAL" not in (camera / "Android.mk").read_text("utf-8"):
        fail("expected VANILLA_HAL build flag missing")
    source = camera / "HAL/QCamera2HWI.cpp"
    before = source.read_text("utf-8")
    after = transform(before)
    if transform(after) != after:
        fail("transform is not idempotent")
    changed = before != after
    if changed:
        source.write_text(after, encoding="utf-8")
    patch = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/camera/QCamera2/HAL/QCamera2HWI.cpp",
        tofile="b/camera/QCamera2/HAL/QCamera2HWI.cpp",
    ))
    args.patch_out.parent.mkdir(parents=True, exist_ok=True)
    args.patch_out.write_text(patch, encoding="utf-8")
    report = (
        f"CAMERA_COMPAT_STATE={'APPLIED' if changed else 'ALREADY_APPLIED'}\n"
        f"CHANGED_FILES={int(changed)}\n"
        "CAMERA_OTP_VANILLA_GUARD=PASS\n"
        "CAMERA_DUAL_VANILLA_GUARD=PASS\n"
        "CAMERA_COMPAT=PASS\n"
    )
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
