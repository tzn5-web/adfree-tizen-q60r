#!/usr/bin/env python3
"""Idempotent, fail-closed TB8504 Android 16 runtime cleanup.

Known validated repairs:
  * remove the 25 init service definitions proven absent from the intended
    runtime payload;
  * remove the stale mm-qcamera-daemon process SDK override;
  * remove the stale Qualcomm Wi-Fi Display VINTF HAL;
  * clear the legacy standalone mm-qcamerad SELinux policy when it contains
    only mm-qcamerad rules;
  * rewrite stale camera-daemon comments without touching data directories.

The transform may be run repeatedly. It accepts original, partially repaired,
or fully repaired state, but always validates the final state.
"""

from __future__ import annotations

import argparse
import difflib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

DEAD_SERVICES = {
    "iop", "vendor.qmuxd", "vendor.ipacm-diag", "vendor.dataadpl",
    "vendor.sensors", "fstman", "fstman_wlan0", "wigighalsvc", "wigignpt",
    "ptt_ffbm", "wifi_ftmd", "wifi-sdio-on", "wifi-crda", "vendor.ssr_diag",
    "diag_mdlog_start", "diag_mdlog_stop", "vm_bms", "vendor.LKCore-dbg",
    "vendor.LKCore-rel", "poweroffhandler", "vendor.hbtp", "qcamerasvr",
    "perfd", "gamed", "hbtp",
}
SERVICE_RE = re.compile(r"^service\s+(\S+)\s+")
WFD_HAL = "com.qualcomm.qti.wifidisplayhal"
CAMERA_OVERRIDE = "/vendor/bin/mm-qcamera-daemon=23"


def fail(message: str) -> None:
    print(f"RUNTIME_CLEANUP_FAIL={message}")
    raise SystemExit(2)


def snapshot(paths: list[Path], device: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in paths:
        if p.is_file():
            out[str(p.relative_to(device))] = p.read_text(
                "utf-8", errors="replace"
            ).splitlines(keepends=True)
    return out


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
                before[rel], after[rel],
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="\n",
            )
        )
    patch_out.parent.mkdir(parents=True, exist_ok=True)
    patch_out.write_text("".join(chunks), encoding="utf-8")


def remove_dead_services(path: Path) -> set[str]:
    lines = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    removed: set[str] = set()
    i = 0
    while i < len(lines):
        m = SERVICE_RE.match(lines[i])
        if not m or m.group(1) not in DEAD_SERVICES:
            out.append(lines[i])
            i += 1
            continue

        name = m.group(1)
        if name in removed:
            fail(f"duplicate dead service definition in {path}: {name}")
        removed.add(name)
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                i += 1
                break
            if nxt[:1].isspace():
                i += 1
                continue
            break

    if removed:
        path.write_text("".join(out), encoding="utf-8")
    return removed


def remove_camera_sdk_override(path: Path) -> int:
    lines = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("TARGET_PROCESS_SDK_VERSION_OVERRIDE") and ":=" in stripped:
            if i + 1 < len(lines) and lines[i + 1].strip() == CAMERA_OVERRIDE:
                removed += 1
                i += 2
                continue
        out.append(lines[i])
        i += 1
    if removed > 1:
        fail(f"ambiguous camera SDK override count: {removed}")
    if removed:
        path.write_text("".join(out), encoding="utf-8")
    return removed


def remove_wfd_hal(path: Path) -> int:
    text = path.read_text("utf-8", errors="replace")
    blocks = list(re.finditer(r"(?ms)^[ \t]*<hal\b.*?</hal>[ \t]*\n?", text))
    targets = [m for m in blocks if f"<name>{WFD_HAL}</name>" in m.group(0)]
    if len(targets) > 1:
        fail(f"ambiguous {WFD_HAL} HAL block count: {len(targets)}")
    if not targets:
        ET.parse(path)
        return 0
    target = targets[0]
    path.write_text(text[:target.start()] + text[target.end():], encoding="utf-8")
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
    active = [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]
    if not active:
        return 0
    if not all("mm-qcamerad" in x for x in active):
        fail("camera-daemon sepolicy contains non-daemon active policy")
    path.write_text(
        "# Legacy standalone camera-daemon policy removed; "
        "camera HAL runs in-process.\n",
        encoding="utf-8",
    )
    return 1


def rewrite_stale_comments(device: Path) -> int:
    changed = 0
    replacements = {
        device / "rootdir/init.target.rc": {
            "#Create folder for mm-qcamera-daemon": "# Camera runtime data directory",
            "#start camera server as daemon":
                "# Legacy camera daemon removed; camera HAL runs in-process",
        },
        device / "rootdir/init.lenovo.rc": {
            "#Create legacy folder for mm-qcamera-daemon":
                "# Camera compatibility data directory",
        },
    }
    for path, mapping in replacements.items():
        if not path.is_file():
            continue
        text = path.read_text("utf-8", errors="replace")
        original = text
        for old, new in mapping.items():
            text = text.replace(old, new)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
    return changed


def active_service_names(device: Path) -> set[str]:
    names: set[str] = set()
    for rc in device.rglob("*.rc"):
        if ".pre_" in rc.name or rc.name.startswith("init.recovery"):
            continue
        for raw in rc.read_text("utf-8", errors="replace").splitlines():
            m = SERVICE_RE.match(raw)
            if m:
                names.add(m.group(1))
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--patch-out", required=True, type=Path)
    ap.add_argument("--report-out", required=True, type=Path)
    args = ap.parse_args()

    device = args.device.resolve()
    if not device.is_dir():
        fail(f"device tree missing: {device}")

    board = device / "BoardConfig.mk"
    manifest = device / "manifest.xml"
    required = [
        board, manifest, device / "rootdir/init.target.rc",
        device / "sepolicy/mm-qcamerad.te",
    ]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        fail(f"required runtime-cleanup inputs missing: {missing}")

    rc_files = [
        p for p in sorted(device.rglob("*.rc"))
        if ".pre_" not in p.name and not p.name.startswith("init.recovery")
    ]
    tracked = list(dict.fromkeys(
        rc_files + [
            board, manifest, device / "rootdir/init.lenovo.rc",
            device / "sepolicy/mm-qcamerad.te",
        ]
    ))
    before = snapshot(tracked, device)

    removed_services: set[str] = set()
    for rc in rc_files:
        removed_services |= remove_dead_services(rc)

    camera_override = remove_camera_sdk_override(board)
    wfd_hal = remove_wfd_hal(manifest)
    camera_sepolicy = clear_camera_daemon_sepolicy(device)
    comment_files = rewrite_stale_comments(device)

    remaining = active_service_names(device) & DEAD_SERVICES
    if remaining:
        fail(f"dead services remain after cleanup: {sorted(remaining)}")

    board_text = board.read_text("utf-8", errors="replace")
    if CAMERA_OVERRIDE in board_text:
        fail("stale mm-qcamera-daemon SDK override remains")

    root = ET.parse(manifest).getroot()
    stale = [
        (hal.findtext("name") or "").strip()
        for hal in root.findall("hal")
        if (hal.findtext("name") or "").strip() == WFD_HAL
    ]
    if stale:
        fail("stale WFD HAL remains after cleanup")

    policy = (device / "sepolicy/mm-qcamerad.te").read_text(
        "utf-8", errors="replace"
    )
    active_policy = [
        x for x in policy.splitlines()
        if x.strip() and not x.lstrip().startswith("#")
    ]
    if any("mm-qcamerad" in x for x in active_policy):
        fail("stale camera-daemon SELinux rules remain")

    after = snapshot(tracked, device)
    write_patch(before, after, args.patch_out)
    changed = sorted(rel for rel in before if before[rel] != after[rel])
    state = "ALREADY_APPLIED" if not changed else "APPLIED"

    report = [
        f"RUNTIME_CLEANUP_STATE={state}",
        f"REMOVED_INIT_SERVICES_THIS_RUN={len(removed_services)}",
        f"REMOVED_SERVICE_NAMES_THIS_RUN={','.join(sorted(removed_services))}",
        f"REMOVED_CAMERA_SDK_OVERRIDE_THIS_RUN={camera_override}",
        f"REMOVED_WFD_VINTF_HAL_THIS_RUN={wfd_hal}",
        f"CLEARED_CAMERA_DAEMON_SEPOLICY_THIS_RUN={camera_sepolicy}",
        f"REWRITTEN_COMMENT_FILES_THIS_RUN={comment_files}",
        f"CHANGED_FILES={len(changed)}",
        *[f"CHANGED_FILE={x}" for x in changed],
        "FINAL_DEAD_SERVICES=0",
        "FINAL_CAMERA_SDK_OVERRIDE=0",
        "FINAL_WFD_VINTF_HAL=0",
        "FINAL_CAMERA_DAEMON_ACTIVE_SEPOLICY=0",
        "NO_FLASH=YES",
        "RUNTIME_CLEANUP=PASS",
    ]
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
