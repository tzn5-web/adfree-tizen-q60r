#!/usr/bin/env python3
"""Apply the audited TB8504 Android 16 runtime cleanup to an exported device tree.

The transform is deliberately narrow and fail-closed:
  * exactly the 25 init service definitions proven absent from both install
    rules and the local build's module-info are removed;
  * the stale mm-qcamera-daemon process SDK override is removed;
  * the VINTF declaration for the removed Qualcomm Wi-Fi Display HAL is removed;
  * stale camera-daemon comments are rewritten without deleting camera data dirs.

It emits a standard unified patch that can later be applied to the local
device/lenovo/TB8504 checkout before the next integrated PC build.
"""

from __future__ import annotations

import argparse
import difflib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

DEAD_SERVICES = {
    "iop",
    "vendor.qmuxd",
    "vendor.ipacm-diag",
    "vendor.dataadpl",
    "vendor.sensors",
    "fstman",
    "fstman_wlan0",
    "wigighalsvc",
    "wigignpt",
    "ptt_ffbm",
    "wifi_ftmd",
    "wifi-sdio-on",
    "wifi-crda",
    "vendor.ssr_diag",
    "diag_mdlog_start",
    "diag_mdlog_stop",
    "vm_bms",
    "vendor.LKCore-dbg",
    "vendor.LKCore-rel",
    "poweroffhandler",
    "vendor.hbtp",
    "qcamerasvr",
    "perfd",
    "gamed",
    "hbtp",
}

SERVICE_RE = re.compile(r"^service\s+(\S+)\s+")
WFD_HAL = "com.qualcomm.qti.wifidisplayhal"


def fail(message: str) -> None:
    print(f"RUNTIME_CLEANUP_FAIL={message}")
    raise SystemExit(2)


def remove_dead_services(path: Path) -> set[str]:
    lines = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    removed: set[str] = set()
    i = 0
    while i < len(lines):
        raw = lines[i]
        match = SERVICE_RE.match(raw)
        if match and match.group(1) in DEAD_SERVICES:
            name = match.group(1)
            if name in removed:
                fail(f"duplicate dead service definition while cleaning: {name}")
            removed.add(name)
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "":
                    i += 1
                    break
                if nxt[:1].isspace():
                    i += 1
                    continue
                break
            continue
        out.append(raw)
        i += 1

    if removed:
        path.write_text("".join(out), encoding="utf-8")
    return removed


def remove_camera_sdk_override(path: Path) -> int:
    lines = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    count = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("TARGET_PROCESS_SDK_VERSION_OVERRIDE") and (
            ":=" in stripped
        ):
            if i + 1 >= len(lines):
                fail("truncated TARGET_PROCESS_SDK_VERSION_OVERRIDE")
            next_stripped = lines[i + 1].strip()
            if next_stripped == "/vendor/bin/mm-qcamera-daemon=23":
                count += 1
                i += 2
                continue
        out.append(lines[i])
        i += 1

    if count != 1:
        fail(f"expected exactly one mm-qcamera-daemon SDK override, found {count}")
    path.write_text("".join(out), encoding="utf-8")
    return count


def remove_wfd_hal(path: Path) -> int:
    text = path.read_text("utf-8", errors="replace")
    blocks = list(re.finditer(r"(?ms)^[ \t]*<hal\b.*?</hal>[ \t]*\n?", text))
    targets = [m for m in blocks if f"<name>{WFD_HAL}</name>" in m.group(0)]
    if len(targets) != 1:
        fail(f"expected exactly one {WFD_HAL} HAL block, found {len(targets)}")
    target = targets[0]
    new = text[: target.start()] + text[target.end() :]
    path.write_text(new, encoding="utf-8")

    try:
        ET.parse(path)
    except ET.ParseError as exc:
        fail(f"manifest malformed after WFD HAL removal: {exc}")
    return 1


def clear_camera_daemon_sepolicy(device: Path) -> int:
    path = device / "sepolicy/mm-qcamerad.te"
    if not path.is_file():
        fail("expected sepolicy/mm-qcamerad.te is missing")

    lines = path.read_text("utf-8", errors="replace").splitlines()
    active = [
        line.strip() for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not active:
        fail("camera-daemon sepolicy is already empty; refusing ambiguous cleanup")
    if not all("mm-qcamerad" in line for line in active):
        fail("camera-daemon sepolicy contains non-daemon policy; refusing cleanup")

    path.write_text(
        "# Legacy standalone camera-daemon policy removed; "
        "camera HAL runs in-process.\n",
        encoding="utf-8",
    )
    return 1


def rewrite_stale_comments(device: Path) -> int:
    changes = 0
    replacements = {
        device / "rootdir/init.target.rc": {
            "#Create folder for mm-qcamera-daemon": "# Camera runtime data directory",
            "#start camera server as daemon": "# Legacy camera daemon removed; camera HAL runs in-process",
        },
        device / "rootdir/init.lenovo.rc": {
            "#Create legacy folder for mm-qcamera-daemon": "# Camera compatibility data directory",
        },
    }
    for path, mapping in replacements.items():
        text = path.read_text("utf-8", errors="replace")
        original = text
        for old, new in mapping.items():
            text = text.replace(old, new)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changes += 1
    return changes


def active_service_names(device: Path) -> set[str]:
    names: set[str] = set()
    for rc in device.rglob("*.rc"):
        if ".pre_" in rc.name or rc.name.startswith("init.recovery"):
            continue
        for raw in rc.read_text("utf-8", errors="replace").splitlines():
            match = SERVICE_RE.match(raw)
            if match:
                names.add(match.group(1))
    return names


def snapshot(paths: list[Path], device: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in paths:
        rel = str(path.relative_to(device))
        result[rel] = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
    return result


def write_patch(
    before: dict[str, list[str]],
    after: dict[str, list[str]],
    patch_out: Path,
) -> None:
    chunks: list[str] = []
    for rel in sorted(before):
        if before[rel] == after[rel]:
            continue
        chunks.extend(
            difflib.unified_diff(
                before[rel],
                after[rel],
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="\n",
            )
        )
    patch_out.parent.mkdir(parents=True, exist_ok=True)
    patch_out.write_text("".join(chunks), encoding="utf-8")
    if patch_out.stat().st_size == 0:
        fail("cleanup produced an empty patch")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--patch-out", required=True, type=Path)
    ap.add_argument("--report-out", required=True, type=Path)
    args = ap.parse_args()

    device = args.device.resolve()
    if not device.is_dir():
        fail(f"device tree missing: {device}")

    rc_files = [
        p for p in sorted(device.rglob("*.rc"))
        if ".pre_" not in p.name and not p.name.startswith("init.recovery")
    ]
    board = device / "BoardConfig.mk"
    manifest = device / "manifest.xml"
    tracked = rc_files + [
        board,
        manifest,
        device / "rootdir/init.lenovo.rc",
        device / "sepolicy/mm-qcamerad.te",
    ]
    tracked = list(dict.fromkeys(p for p in tracked if p.is_file()))
    before = snapshot(tracked, device)

    removed: set[str] = set()
    for rc in rc_files:
        removed |= remove_dead_services(rc)

    missing_expected = DEAD_SERVICES - removed
    unexpected = removed - DEAD_SERVICES
    if missing_expected or unexpected:
        fail(
            "dead-service set mismatch "
            f"missing={sorted(missing_expected)} unexpected={sorted(unexpected)}"
        )

    remove_camera_sdk_override(board)
    remove_wfd_hal(manifest)
    clear_camera_daemon_sepolicy(device)
    rewrite_stale_comments(device)

    remaining = active_service_names(device) & DEAD_SERVICES
    if remaining:
        fail(f"dead services remain after cleanup: {sorted(remaining)}")

    board_text = board.read_text("utf-8", errors="replace")
    if "/vendor/bin/mm-qcamera-daemon=23" in board_text:
        fail("stale mm-qcamera-daemon SDK override remains")

    sepolicy_text = (device / "sepolicy/mm-qcamerad.te").read_text(
        "utf-8", errors="replace"
    )
    active_sepolicy = [
        line for line in sepolicy_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if any("mm-qcamerad" in line for line in active_sepolicy):
        fail("stale camera-daemon SELinux rules remain")

    manifest_root = ET.parse(manifest).getroot()
    stale_hal = [
        (hal.findtext("name") or "").strip()
        for hal in manifest_root.findall("hal")
        if (hal.findtext("name") or "").strip() == WFD_HAL
    ]
    if stale_hal:
        fail("stale WFD HAL remains after cleanup")

    after = snapshot(tracked, device)
    write_patch(before, after, args.patch_out)

    changed_files = [rel for rel in before if before[rel] != after[rel]]
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(
        "\n".join(
            [
                f"REMOVED_INIT_SERVICES={len(removed)}",
                f"REMOVED_SERVICE_NAMES={','.join(sorted(removed))}",
                "REMOVED_CAMERA_SDK_OVERRIDE=1",
                "REMOVED_WFD_VINTF_HAL=1",
                "CLEARED_CAMERA_DAEMON_SEPOLICY=1",
                f"CHANGED_FILES={len(changed_files)}",
                *[f"CHANGED_FILE={rel}" for rel in changed_files],
                "NO_FLASH=YES",
                "RUNTIME_CLEANUP=PASS",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"REMOVED_INIT_SERVICES={len(removed)}")
    print("REMOVED_CAMERA_SDK_OVERRIDE=1")
    print("REMOVED_WFD_VINTF_HAL=1")
    print("CLEARED_CAMERA_DAEMON_SEPOLICY=1")
    print(f"CHANGED_FILES={len(changed_files)}")
    print(f"PATCH_OUT={args.patch_out}")
    print("RUNTIME_CLEANUP=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
