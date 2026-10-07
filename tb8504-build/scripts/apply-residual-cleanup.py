#!/usr/bin/env python3
"""Repair residual TB8504 Android 16 bring-up inconsistencies after STAGE8L/8N.

This transform is intentionally narrow and fail-closed. It:
  * restores the four IMS services whose proprietary executables are still
    installed and whose runtime contract remains present;
  * removes direct start/stop references to services intentionally removed
    from the device init files;
  * removes the now-backendless Qualcomm Wi-Fi Display app/jar selection and
    associated stale device configuration;
  * removes the fstman config copy after fstman itself was removed.

It is idempotent: a fully repaired tree reports ALREADY_APPLIED instead of
rewriting files.
"""
from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

IMS_SERVICES = {
    "vendor.imsqmidaemon",
    "vendor.imsdatadaemon",
    "vendor.ims_rtp_daemon",
    "vendor.imsrcsservice",
}
IMS_EXECUTABLES = {
    "vendor.imsqmidaemon": "imsqmidaemon",
    "vendor.imsdatadaemon": "imsdatadaemon",
    "vendor.ims_rtp_daemon": "ims_rtp_daemon",
    "vendor.imsrcsservice": "imsrcsd",
}

# Exact service names removed by the old pre_stage8l12 cleanup, excluding the
# four IMS services above which are restored by this transform.
ABSENT_SERVICES = {
    "audiod","chre","cnss-daemon","crashdata-sh","diag_mdlog_start",
    "diag_mdlog_stop","drmdiag","dts_configurator","dtseagleservice",
    "esepmdaemon","fstman","fstman_wlan0","gamed","hbtp","hvdcp",
    "ims_regmanager","iop","mdtpd","mlid","perfd","poweroffhandler","ppd",
    "ptt_ffbm","ptt_socket_app","qcamerasvr","qcomsysd","qfp-daemon",
    "qlogd","qrngd","qrngp","qseeproxydaemon","qvop-daemon",
    "seemp_healthd","ssgqmigd","ssgtzd","vendor.LKCore-dbg",
    "vendor.LKCore-rel","vendor.audio-hal-2-0","vendor.bt-dun",
    "vendor.bt_logger","vendor.btsnoop","vendor.dataadpl","vendor.hbtp",
    "vendor.hvdcp_opti","vendor.ipacm-diag","vendor.move_time_data",
    "vendor.port-bridge","vendor.qdmastatsd","vendor.qmuxd","vendor.qrtr-ns",
    "vendor.ril-daemon2","vendor.ril-daemon3","vendor.sensors",
    "vendor.ss_ramdump","vendor.ssr_diag","vendor.ssr_setup",
    "vendor.start_hci_filter","vendor.tlocd","vendor.vppservice",
    "vendor.wifilearner","vm_bms","wifi-crda","wifi-sdio-on","wifi_ftmd",
    "wigighalsvc","wigignpt",
}

SERVICE_RE = re.compile(r"^service\s+(\S+)\s+")
CONTROL_RE = re.compile(
    r"^(?P<indent>\s*)(?P<verb>start|stop|restart|enable|disable)"
    r"\s+(?P<service>[^\s#;]+)(?P<tail>.*)$"
)
CTL_PROP_RE = re.compile(
    r"^(?P<indent>\s*)setprop\s+ctl\.(?:start|stop|restart)"
    r"\s+(?P<service>[^\s#;]+)(?P<tail>.*)$"
)

def fail(msg: str) -> None:
    print(f"RESIDUAL_CLEANUP_FAIL={msg}")
    raise SystemExit(2)

def snapshot(paths: list[Path], roots: list[tuple[str, Path]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in paths:
        for prefix, root in roots:
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            out[f"{prefix}/{rel}"] = p.read_text("utf-8", errors="replace").splitlines(keepends=True)
            break
    return out

def write_patch(before: dict[str,list[str]], after: dict[str,list[str]], out: Path) -> None:
    chunks: list[str] = []
    for rel in sorted(before):
        if before[rel] == after[rel]:
            continue
        chunks.extend(difflib.unified_diff(
            before[rel], after[rel],
            fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="\n",
        ))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(chunks), encoding="utf-8")

def service_blocks(path: Path) -> dict[str,str]:
    lines = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
    out: dict[str,str] = {}
    i = 0
    while i < len(lines):
        m = SERVICE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        block = [lines[i]]
        i += 1
        while i < len(lines) and (lines[i][:1].isspace() or not lines[i].strip()):
            block.append(lines[i])
            i += 1
        out[name] = "".join(block).rstrip() + "\n"
    return out

def current_services(device: Path) -> set[str]:
    names: set[str] = set()
    for p in device.rglob("*.rc"):
        if ".pre_" in p.name:
            continue
        for line in p.read_text("utf-8", errors="replace").splitlines():
            m = SERVICE_RE.match(line)
            if m:
                names.add(m.group(1))
    return names

def restore_ims(device: Path, vendor: Path) -> int:
    target = device / "rootdir/init.target.rc"
    backup = device / "rootdir/init.target.rc.pre_stage8l12"
    vmk = vendor / "TB8504-vendor.mk"
    if not target.is_file() or not backup.is_file() or not vmk.is_file():
        fail("IMS restore inputs missing")

    vendor_text = vmk.read_text("utf-8", errors="replace")
    for service, exe in IMS_EXECUTABLES.items():
        needle = f"/{exe}:$(TARGET_COPY_OUT_VENDOR)/bin/{exe}"
        if needle not in vendor_text:
            fail(f"IMS executable is not installed by vendor makefile: {service} -> {exe}")

    present = current_services(device)
    missing = sorted(IMS_SERVICES - present)
    if not missing:
        return 0

    blocks = service_blocks(backup)
    absent_blocks = [x for x in missing if x not in blocks]
    if absent_blocks:
        fail(f"missing original IMS service blocks: {absent_blocks}")

    text = target.read_text("utf-8", errors="replace")
    marker = "# ANDROID16_BRINGUP: restored installed IMS daemon services\n"
    if marker not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + marker
    for name in missing:
        text += blocks[name] + "\n"
    target.write_text(text, encoding="utf-8")

    now = current_services(device)
    if not IMS_SERVICES <= now:
        fail("IMS services were not restored completely")
    return len(missing)

def remove_dead_controls(device: Path) -> int:
    changed = 0
    refs = 0
    for p in sorted(device.rglob("*")):
        if not p.is_file() or ".pre_" in p.name or p.suffix not in {".rc", ".sh"}:
            continue
        lines = p.read_text("utf-8", errors="replace").splitlines(keepends=True)
        out: list[str] = []
        local = 0
        for raw in lines:
            plain = raw.rstrip("\n")
            m = CONTROL_RE.match(plain) or CTL_PROP_RE.match(plain)
            if m and m.group("service") in ABSENT_SERVICES:
                refs += 1
                local += 1
                if p.suffix != ".sh":
                    fail(
                        "stale removed-service control found outside shell script: "
                        f"{p.relative_to(device)}:{m.group('service')}"
                    )
                indent = raw[: len(raw) - len(raw.lstrip())]
                out.append(
                    f"{indent}: # ANDROID16_BRINGUP: removed stale control "
                    f"for {m.group('service')}\n"
                )
                continue
            out.append(raw)
        if local:
            p.write_text("".join(out), encoding="utf-8")
            changed += 1

    # Expected exported+first-cleanup state has exactly 15 references. Zero is
    # the only valid idempotent state.
    if refs not in {0, 15}:
        fail(f"unexpected removed-service control reference count: {refs}")
    return refs

def replace_exact(path: Path, old: str, new: str, expected: int = 1) -> int:
    text = path.read_text("utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        if new == "" or (new and new in text):
            return 0
        fail(f"{path.name}: missing both original and repaired form for {old!r}")
    if count != expected:
        fail(f"{path.name}: expected {expected} occurrences of {old!r}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    return count

def remove_bp_module(path: Path, module_name: str) -> int:
    text = path.read_text("utf-8", errors="replace")
    needle = f'name: "{module_name}"'
    pos = text.find(needle)
    if pos < 0:
        return 0

    start = text.rfind("\n", 0, pos)
    while start > 0:
        prev = text.rfind("\n", 0, start)
        candidate = text[prev + 1:start].strip()
        if candidate.endswith("{") and re.match(r"^[A-Za-z0-9_]+\s*\{$", candidate):
            start = prev + 1
            break
        start = prev
    else:
        fail(f"cannot locate Android.bp block start for {module_name}")

    brace = 0
    end = None
    for i in range(start, len(text)):
        if text[i] == "{":
            brace += 1
        elif text[i] == "}":
            brace -= 1
            if brace == 0:
                end = i + 1
                while end < len(text) and text[end] in "\r\n":
                    end += 1
                break
    if end is None:
        fail(f"cannot locate Android.bp block end for {module_name}")

    block = text[start:end]
    if block.count(needle) != 1:
        fail(f"ambiguous Android.bp block for {module_name}")
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    return 1

def wfd_cleanup(device: Path, vendor: Path) -> dict[str,int]:
    counts: dict[str,int] = {}
    vmk = vendor / "TB8504-vendor.mk"
    vbp = vendor / "Android.bp"
    dmk = device / "device.mk"
    prop = device / "vendor_prop.mk"
    overlay = device / "overlay/frameworks/base/core/res/res/values/config.xml"
    whitelist = device / "configs/qti_whitelist.xml"
    privapp = device / "configs/privapp-permissions-qti.xml"

    counts["vendor_pkg_wfdservice"] = replace_exact(
        vmk, "    WfdService \\\n", "", 1
    )
    # WfdCommon is final item in the generated list and has no continuation.
    counts["vendor_pkg_wfdcommon"] = replace_exact(
        vmk, "    WfdCommon\n", "", 1
    )
    counts["vendor_bp_wfdservice"] = remove_bp_module(vbp, "WfdService")
    counts["vendor_bp_wfdcommon"] = remove_bp_module(vbp, "WfdCommon")

    counts["fstman_config"] = replace_exact(
        dmk,
        "    $(LOCAL_PATH)/wifi/fstman.ini:system/etc/wifi/fstman.ini \\\n",
        "",
        1,
    )
    counts["wfd_prop"] = replace_exact(
        prop, "    persist.sys.wfd.virtual=0 \\\n", "", 1
    )

    for name in ("config_wifiDisplaySupportsProtectedBuffers", "config_enableWifiDisplay"):
        old = f'<bool name="{name}">true</bool>'
        new = f'<bool name="{name}">false</bool>'
        counts[name] = replace_exact(overlay, old, new, 1)

    counts["whitelist_client"] = replace_exact(
        whitelist,
        '    <hidden-api-whitelisted-app package="com.qualcomm.wfd.client" />\n',
        "",
        1,
    )
    counts["whitelist_service"] = replace_exact(
        whitelist,
        '    <hidden-api-whitelisted-app package="com.qualcomm.wfd.service" />\n',
        "",
        1,
    )

    text = privapp.read_text("utf-8", errors="replace")
    rx = re.compile(
        r'\n\s*<privapp-permissions package="com\.qualcomm\.wfd\.service">.*?'
        r'</privapp-permissions>\s*\n',
        re.S,
    )
    new_text, n = rx.subn("\n", text)
    if n == 0 and "com.qualcomm.wfd.service" not in text:
        n = 0
    elif n != 1:
        fail(f"unexpected WFD privapp block count: {n}")
    if n:
        privapp.write_text(new_text, encoding="utf-8")
    counts["privapp_wfd"] = n
    return counts

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--vendor", required=True, type=Path)
    ap.add_argument("--patch-out", required=True, type=Path)
    ap.add_argument("--report-out", required=True, type=Path)
    args = ap.parse_args()
    device, vendor = args.device.resolve(), args.vendor.resolve()
    if not device.is_dir() or not vendor.is_dir():
        fail("device/vendor root missing")

    touched = [
        device / "rootdir/init.target.rc",
        device / "rootdir/etc/init.qcom.post_boot.sh",
        device / "rootdir/etc/init.qcom.sh",
        device / "rootdir/etc/init.qcom.sensors.sh",
        device / "rootdir/etc/init.class_main.sh",
        device / "device.mk",
        device / "vendor_prop.mk",
        device / "overlay/frameworks/base/core/res/res/values/config.xml",
        device / "configs/qti_whitelist.xml",
        device / "configs/privapp-permissions-qti.xml",
        vendor / "TB8504-vendor.mk",
        vendor / "Android.bp",
    ]
    missing = [str(p) for p in touched if not p.is_file()]
    if missing:
        fail(f"required residual-cleanup files missing: {missing}")

    roots = [("device/lenovo/TB8504", device), ("vendor/lenovo/TB8504", vendor)]
    before = snapshot(touched, roots)

    ims_restored = restore_ims(device, vendor)
    dead_refs_removed = remove_dead_controls(device)
    wfd = wfd_cleanup(device, vendor)

    # Final hard checks.
    services = current_services(device)
    if not IMS_SERVICES <= services:
        fail("required IMS services missing after repair")

    residual_refs: list[str] = []
    for p in device.rglob("*"):
        if not p.is_file() or ".pre_" in p.name or p.suffix not in {".rc", ".sh"}:
            continue
        for no, raw in enumerate(p.read_text("utf-8", errors="replace").splitlines(), 1):
            m = CONTROL_RE.match(raw) or CTL_PROP_RE.match(raw)
            if m and m.group("service") in ABSENT_SERVICES:
                residual_refs.append(f"{p.relative_to(device)}:{no}:{m.group('service')}")
    if residual_refs:
        fail(f"removed-service control references remain: {residual_refs[:20]}")

    combined = "\n".join(
        p.read_text("utf-8", errors="replace") for p in [
            vendor / "TB8504-vendor.mk", vendor / "Android.bp"
        ]
    )
    if "WfdService" in combined or "WfdCommon" in combined:
        fail("WFD app/jar remains selected or defined in vendor generated files")

    if "fstman.ini" in (device / "device.mk").read_text("utf-8", errors="replace"):
        fail("fstman config is still copied after fstman removal")

    after = snapshot(touched, roots)
    write_patch(before, after, args.patch_out)
    changed = sorted(k for k in before if before[k] != after[k])

    state = "ALREADY_APPLIED" if not changed else "APPLIED"
    lines = [
        f"RESIDUAL_CLEANUP_STATE={state}",
        f"IMS_SERVICES_RESTORED={ims_restored}",
        f"DEAD_CONTROL_REFS_REMOVED={dead_refs_removed}",
        f"CHANGED_FILES={len(changed)}",
        *[f"CHANGED_FILE={x}" for x in changed],
        *[f"WFD_{k.upper()}={v}" for k,v in sorted(wfd.items())],
        "NO_FLASH=YES",
        "RESIDUAL_CLEANUP=PASS",
    ]
    args.report_out.parent.mkdir(parents=True, exist_ok=True)
    args.report_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
