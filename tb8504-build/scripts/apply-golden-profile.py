#!/usr/bin/env python3
"""Idempotent port of observed crDroid 10 TB8504 governor settings."""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path

GOLDEN_CAPTURE_SHA256 = "a630e1798ec820d279af91fc56d9e0bbfd557d9a04bce52c779a1a84f6fbbc92"
BEGIN = "# BEGIN TB8504 CRDROID10 GOLDEN 20261003"
END = "# END TB8504 CRDROID10 GOLDEN 20261003"
INTERACTIVE = {
    "above_hispeed_delay": "19000 1094400:39000",
    "align_windows": "0", "boost": "0", "boostpulse_duration": "80000",
    "enable_prediction": "0", "fast_ramp_down": "0",
    "go_hispeed_load": "85", "hispeed_freq": "1094400",
    "ignore_hispeed_on_notif": "0", "io_is_busy": "0", "max_freq_hysteresis": "0",
    "min_sample_time": "40000", "target_loads": "1 960000:85 1094400:90",
    "timer_rate": "20000", "timer_slack": "80000",
    "use_migration_notif": "1", "use_sched_load": "1",
}


def fail(message: str) -> None:
    raise SystemExit(f"GOLDEN_PROFILE_FAIL={message}")


def golden_block() -> str:
    rows = [BEGIN, f"# Observed device_perf.txt SHA256: {GOLDEN_CAPTURE_SHA256}",
        "# Apply after inherited Qualcomm setup. Preserve thermal protection.",
        "# ART/LMKD policy and transient measurements are not ported across Android versions.",
        "tb8504_golden_write() {",
        '    if [ -w "$1" ]; then',
        '        printf "%s\\n" "$2" > "$1" || echo "TB8504_GOLDEN_WRITE_FAILED: $1" >&2',
        "    else", '        echo "TB8504_GOLDEN_NODE_UNAVAILABLE: $1" >&2', "    fi", "}",
        'if [ "$(getprop ro.product.device)" = "TB8504" ]; then',
        "    tb8504_golden_write /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 'interactive'",
    ]
    for key, value in INTERACTIVE.items():
        rows.append(f"    tb8504_golden_write /sys/devices/system/cpu/cpufreq/interactive/{key} '{value}'")
    for path, value in (
        ("/sys/devices/system/cpu/cpu0/core_ctl/disable", "1"),
        ("/proc/sys/vm/swappiness", "100"), ("/proc/sys/vm/page-cluster", "3"),
        ("/sys/block/mmcblk0/queue/scheduler", "cfq"),
        ("/sys/block/mmcblk0/queue/read_ahead_kb", "128"),
        ("/sys/class/kgsl/kgsl-3d0/devfreq/governor", "msm-adreno-tz"),
        ("/sys/class/kgsl/kgsl-3d0/default_pwrlevel", "3"),
    ):
        rows.append(f"    tb8504_golden_write {path} '{value}'")
    rows += ["fi", END]
    return "\n".join(rows) + "\n"


def transform(before: str) -> str:
    block = golden_block()
    if BEGIN in before or END in before:
        if before.count(BEGIN) != 1 or before.count(END) != 1 or not before.endswith(block):
            fail("golden override is duplicated, altered, or no longer last")
        return before
    if "function 8917_sched_dcvs_hmp()" not in before:
        fail("expected Qualcomm 8917 governor setup missing")
    return before.rstrip() + "\n\n" + block


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--patch-out", required=True, type=Path)
    ap.add_argument("--report-out", required=True, type=Path)
    args = ap.parse_args()
    file = args.device.resolve() / "rootdir/etc/init.qcom.post_boot.sh"
    before = file.read_text("utf-8", errors="strict")
    after = transform(before)
    changed = after != before
    if changed:
        file.write_text(after, encoding="utf-8")
    rel = "device/lenovo/TB8504/rootdir/etc/init.qcom.post_boot.sh"
    patch = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile="a/" + rel, tofile="b/" + rel))
    args.patch_out.parent.mkdir(parents=True, exist_ok=True)
    args.patch_out.write_text(patch, encoding="utf-8")
    report = (
        f"GOLDEN_PROFILE_STATE={'APPLIED' if changed else 'ALREADY_APPLIED'}\n"
        f"CHANGED_FILES={int(changed)}\n"
        f"GOLDEN_CAPTURE_SHA256={GOLDEN_CAPTURE_SHA256}\n"
        "GOLDEN_PROFILE=PASS\n"
        "GOLDEN_RUNTIME_VERIFICATION=PENDING_DEVICE_TEST\n"
    )
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
