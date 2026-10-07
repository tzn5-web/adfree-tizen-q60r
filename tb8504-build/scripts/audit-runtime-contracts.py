#!/usr/bin/env python3
"""Static runtime-contract audit for the reconstructed TB8504 Android 16 state.

Checks active init service executables, stale process-SDK overrides, duplicate
service names, VINTF XML syntax and HAL declarations known to have had their
implementation removed. The check is conservative: unknown /system/bin
executables are reported as platform/external rather than failed.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

COPY_LINE_RE = re.compile(r"^\s*([^#\s][^:]*):([^\s\\]+)")
LOCAL_MODULE_RE = re.compile(r"^\s*LOCAL_MODULE\s*:?=\s*([^\s\\#]+)", re.M)
BP_NAME_RE = re.compile(r'\bname\s*:\s*"([^"]+)"')
PRODUCT_PACKAGE_START = re.compile(r"^\s*PRODUCT_PACKAGES\s*\+=")
SERVICE_RE = re.compile(r"^\s*service\s+(\S+)\s+(.+?)\s*$")
HAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

DEST_VARS = {
    "$(TARGET_COPY_OUT_VENDOR)": "/vendor",
    "$(TARGET_COPY_OUT_SYSTEM)": "/system",
    "$(TARGET_COPY_OUT_PRODUCT)": "/product",
    "$(TARGET_COPY_OUT_SYSTEM_EXT)": "/system_ext",
    "$(TARGET_COPY_OUT_ODM)": "/odm",
}

REMOVED_EXECUTABLE_BASENAMES = {
    "mm-qcamera-daemon",
    "wfdservice",
    "wifidisplayhalservice",
    "qfp-daemon",
    "qrngd",
    "qrngp",
    "audiod",
    "ppd",
    "dts_configurator",
    "dtseagleservice",
    "mdtpd",
    "qcomsysd",
    "ptt_socket_app",
    "cnss-daemon",
    "ssgqmigd",
    "ssgtzd",
    "mlid",
    "drmdiag",
    "qvop-daemon",
    "ims_regmanager",
    "hvdcp",
    "qlogd",
    "qseeproxydaemon",
    "esepmdaemon",
    "seemp_healthd",
}
REMOVED_SERVICE_NAMES = {
    "qcamerasvr",
    "vendor.hvdcp_opti",
    "vendor.tlocd",
    "vendor.ssr_setup",
    "vendor.ss_ramdump",
    "vendor.qrtr-ns",
    "vendor.start_hci_filter",
    "vendor.bt-dun",
    "vendor.btsnoop",
    "vendor.bt_logger",
    "vendor.port-bridge",
    "vendor.qdmastatsd",
    "crashdata-sh",
    "vendor.vppservice",
    "vendor.move_time_data",
    "vendor.wifilearner",
    "vendor.audio-hal-2-0",
}
REMOVED_HAL_NAMES = {
    "com.qualcomm.qti.wifidisplayhal",
}


def normalize_dest(value: str) -> str:
    value = value.strip()
    for key, repl in DEST_VARS.items():
        if value.startswith(key):
            value = repl + value[len(key):]
            break
    value = value.replace("//", "/")
    return value


def aliases(path: str) -> set[str]:
    out = {path}
    if path.startswith("/system/vendor/"):
        out.add("/vendor/" + path[len("/system/vendor/"):])
    elif path.startswith("/vendor/"):
        out.add("/system/vendor/" + path[len("/vendor/"):])
    return out


def product_copy_destinations(root: Path) -> set[str]:
    result: set[str] = set()
    for mk in root.rglob("*.mk"):
        if ".pre_" in mk.name:
            continue
        active = False
        for raw in mk.read_text("utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            if "PRODUCT_COPY_FILES" in stripped and "+=" in stripped:
                active = True
                payload = stripped.split("+=", 1)[1].strip()
            elif active:
                payload = stripped
            else:
                continue

            payload = payload.rstrip("\\").strip()
            if payload and not payload.startswith("#"):
                match = COPY_LINE_RE.match(payload)
                if match:
                    dest = normalize_dest(match.group(2))
                    result.update(aliases(dest))

            if not raw.rstrip().endswith("\\"):
                active = False
    return result


def product_packages(root: Path) -> set[str]:
    result: set[str] = set()
    for mk in root.rglob("*.mk"):
        if ".pre_" in mk.name:
            continue
        active = False
        for raw in mk.read_text("utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            if PRODUCT_PACKAGE_START.match(raw):
                active = True
                payload = stripped.split("+=", 1)[1].strip()
            elif active:
                payload = stripped
            else:
                continue

            payload = payload.rstrip("\\").strip()
            if payload and not payload.startswith("#"):
                for token in payload.split():
                    if token.endswith(":32") or token.endswith(":64"):
                        token = token[:-3]
                    result.add(token)
            if not raw.rstrip().endswith("\\"):
                active = False
    return result


def source_modules(root: Path) -> set[str]:
    result: set[str] = set()
    for mk in root.rglob("Android.mk"):
        if ".pre_" in mk.name:
            continue
        text = mk.read_text("utf-8", errors="replace")
        result.update(LOCAL_MODULE_RE.findall(text))
    for bp in root.rglob("Android.bp"):
        text = bp.read_text("utf-8", errors="replace")
        result.update(BP_NAME_RE.findall(text))
    return result


def module_info_installed_paths(path: Path | None) -> set[str]:
    result: set[str] = set()
    if path is None or not path.is_file():
        return result

    data = json.loads(path.read_text("utf-8", errors="replace"))
    marker = "out/target/product/TB8504/"
    for module in data.values():
        for installed in module.get("installed", []):
            if marker not in installed:
                continue
            rel = installed.split(marker, 1)[1]
            if rel.startswith("system/vendor/"):
                target = "/vendor/" + rel[len("system/vendor/"):]
            elif rel.startswith("system/system_ext/"):
                target = "/system_ext/" + rel[len("system/system_ext/"):]
            elif rel.startswith("system/product/"):
                target = "/product/" + rel[len("system/product/"):]
            elif rel.startswith("system/"):
                target = "/system/" + rel[len("system/"):]
            elif rel.startswith("vendor/"):
                target = "/vendor/" + rel[len("vendor/"):]
            elif rel.startswith("product/"):
                target = "/product/" + rel[len("product/"):]
            else:
                continue
            result.update(aliases(target))
    return result


def parse_service_headers(device_root: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for rc in sorted(device_root.rglob("*.rc")):
        name = rc.name
        if ".pre_" in name or name.startswith("init.recovery"):
            continue
        for line_no, raw in enumerate(
            rc.read_text("utf-8", errors="replace").splitlines(), 1
        ):
            match = SERVICE_RE.match(raw)
            if not match:
                continue
            service_name, command = match.groups()
            command = command.rstrip("\\").strip()
            try:
                argv = shlex.split(command)
            except ValueError:
                argv = command.split()
            if not argv:
                continue
            result.append({
                "service": service_name,
                "exec": argv[0],
                "args": " ".join(argv[1:]),
                "file": str(rc.relative_to(device_root)),
                "line": str(line_no),
            })
    return result


def classify_exec(
    path: str,
    installed_paths: set[str],
    packages: set[str],
    modules: set[str],
) -> str:
    if path in installed_paths:
        return "installed-copy"
    alt = next((x for x in aliases(path) if x in installed_paths), None)
    if alt:
        return "installed-copy-alias"

    base = Path(path).name
    candidates = {base}
    if base.endswith(".sh"):
        candidates.add(base[:-3])
    if candidates & packages:
        return "product-package"
    if candidates & modules:
        return "source-module"

    if path.startswith("/system/bin/"):
        return "platform-or-system-source"
    if path in {"/system/bin/sh", "/system/bin/logwrapper"}:
        return "platform"
    return "missing"


def parse_sdk_overrides(device_root: Path) -> list[dict[str, str]]:
    board = device_root / "BoardConfig.mk"
    if not board.is_file():
        return []
    result: list[dict[str, str]] = []
    active = False
    for line_no, raw in enumerate(
        board.read_text("utf-8", errors="replace").splitlines(), 1
    ):
        stripped = raw.strip()
        if stripped.startswith("TARGET_PROCESS_SDK_VERSION_OVERRIDE") and ":=" in stripped:
            active = True
            payload = stripped.split(":=", 1)[1].strip()
        elif active:
            payload = stripped
        else:
            continue

        payload = payload.rstrip("\\").strip()
        if payload and not payload.startswith("#") and "=" in payload:
            proc_path, sdk = payload.split("=", 1)
            result.append({
                "path": proc_path.strip(),
                "sdk": sdk.strip(),
                "line": str(line_no),
            })
        if not raw.rstrip().endswith("\\"):
            active = False
    return result


def stale_camera_sepolicy_refs(device_root: Path) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    sepolicy = device_root / "sepolicy"
    if not sepolicy.is_dir():
        return refs

    for path in sorted(sepolicy.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in {".te", ".cil"} and "context" not in path.name:
            continue
        for line_no, raw in enumerate(
            path.read_text("utf-8", errors="replace").splitlines(), 1
        ):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "mm-qcamerad" in stripped:
                refs.append({
                    "file": str(path.relative_to(device_root)),
                    "line": str(line_no),
                    "text": stripped,
                })
    return refs


def vintf_inventory(device_root: Path) -> tuple[list[dict[str, str]], list[str]]:
    hals: list[dict[str, str]] = []
    errors: list[str] = []
    for filename in (
        "manifest.xml",
        "framework_manifest.xml",
        "compatibility_matrix.xml",
        "framework_compatibility_matrix.xml",
    ):
        path = device_root / filename
        if not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            errors.append(f"{filename}:{exc}")
            continue
        for hal in root.findall("hal"):
            name = (hal.findtext("name") or "").strip()
            if name and HAL_NAME_RE.fullmatch(name):
                hals.append({"file": filename, "name": name})
    return hals, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True, type=Path)
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--report-dir", required=True, type=Path)
    ap.add_argument("--module-info", type=Path)
    args = ap.parse_args()

    vendor_root = args.vendor.resolve()
    device_root = args.device.resolve()
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    installed_paths = product_copy_destinations(vendor_root)
    installed_paths |= product_copy_destinations(device_root)
    module_info_paths = module_info_installed_paths(args.module_info)
    installed_paths |= module_info_paths
    packages = product_packages(vendor_root) | product_packages(device_root)
    modules = source_modules(vendor_root) | source_modules(device_root)

    services = parse_service_headers(device_root)
    seen: dict[str, list[dict[str, str]]] = defaultdict(list)
    unresolved: list[dict[str, str]] = []
    removed_refs: list[dict[str, str]] = []
    service_rows: list[dict[str, str]] = []

    for service in services:
        seen[service["service"]].append(service)
        status = classify_exec(
            service["exec"], installed_paths, packages, modules
        )
        row = dict(service)
        row["status"] = status
        service_rows.append(row)

        base = Path(service["exec"]).name
        if (
            service["service"] in REMOVED_SERVICE_NAMES
            or base in REMOVED_EXECUTABLE_BASENAMES
        ):
            removed_refs.append(row)

        if status == "missing":
            unresolved.append(row)

        if service["exec"] == "/system/bin/logwrapper":
            args_text = service["args"]
            wrapped = args_text.split()[0] if args_text else ""
            if wrapped.startswith("/"):
                wrapped_status = classify_exec(
                    wrapped, installed_paths, packages, modules
                )
                if wrapped_status == "missing":
                    wrapped_row = dict(row)
                    wrapped_row["wrapped_exec"] = wrapped
                    wrapped_row["wrapped_status"] = wrapped_status
                    unresolved.append(wrapped_row)

    duplicates = {
        name: rows for name, rows in seen.items() if len(rows) > 1
    }

    overrides = parse_sdk_overrides(device_root)
    stale_overrides: list[dict[str, str]] = []
    for item in overrides:
        status = classify_exec(
            item["path"], installed_paths, packages, modules
        )
        item["status"] = status
        if status == "missing":
            stale_overrides.append(item)

    hals, vintf_errors = vintf_inventory(device_root)
    stale_hals = [
        item for item in hals if item["name"] in REMOVED_HAL_NAMES
    ]
    stale_sepolicy = stale_camera_sepolicy_refs(device_root)

    report = {
        "service_count": len(services),
        "installed_path_count": len(installed_paths),
        "module_info_installed_path_count": len(module_info_paths),
        "package_count": len(packages),
        "module_count": len(modules),
        "unresolved_services": unresolved,
        "removed_service_references": removed_refs,
        "duplicate_services": duplicates,
        "sdk_overrides": overrides,
        "stale_sdk_overrides": stale_overrides,
        "vintf_hals": hals,
        "vintf_parse_errors": vintf_errors,
        "stale_vintf_hals": stale_hals,
        "stale_camera_sepolicy_references": stale_sepolicy,
    }
    (report_dir / "runtime-contracts.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with (report_dir / "runtime-services.txt").open("w", encoding="utf-8") as out:
        for row in service_rows:
            out.write(
                f"{row['status']}\t{row['service']}\t{row['exec']}\t"
                f"{row['file']}:{row['line']}\n"
            )

    print("=== TB8504 RUNTIME CONTRACT AUDIT ===")
    print(f"INIT_SERVICE_COUNT={len(services)}")
    print(f"MODULE_INFO_INSTALLED_PATHS={len(module_info_paths)}")
    print(f"UNRESOLVED_INIT_SERVICES={len(unresolved)}")
    print(f"REMOVED_SERVICE_REFERENCES={len(removed_refs)}")
    print(f"DUPLICATE_INIT_SERVICE_NAMES={len(duplicates)}")
    print(f"SDK_OVERRIDE_COUNT={len(overrides)}")
    print(f"STALE_SDK_OVERRIDES={len(stale_overrides)}")
    print(f"VINTF_HAL_DECLARATIONS={len(hals)}")
    print(f"VINTF_PARSE_ERRORS={len(vintf_errors)}")
    print(f"STALE_VINTF_HAL_DECLARATIONS={len(stale_hals)}")
    print(f"STALE_CAMERA_SEPOLICY_REFERENCES={len(stale_sepolicy)}")

    for row in unresolved:
        print(
            f"UNRESOLVED_SERVICE={row['service']}|{row['exec']}|"
            f"{row['file']}:{row['line']}"
        )
    for row in removed_refs:
        print(
            f"REMOVED_SERVICE_REF={row['service']}|{row['exec']}|"
            f"{row['file']}:{row['line']}"
        )
    for name, rows in sorted(duplicates.items()):
        where = ",".join(f"{r['file']}:{r['line']}" for r in rows)
        print(f"DUPLICATE_SERVICE={name}|{where}")
    for row in stale_overrides:
        print(
            f"STALE_SDK_OVERRIDE={row['path']}={row['sdk']}|"
            f"BoardConfig.mk:{row['line']}"
        )
    for item in stale_hals:
        print(f"STALE_VINTF_HAL={item['name']}|{item['file']}")
    for err in vintf_errors:
        print(f"VINTF_PARSE_ERROR={err}")
    for item in stale_sepolicy:
        print(
            f"STALE_CAMERA_SEPOLICY={item['file']}:{item['line']}|"
            f"{item['text']}"
        )

    failures = (
        len(unresolved)
        + len(removed_refs)
        + len(duplicates)
        + len(stale_overrides)
        + len(stale_hals)
        + len(vintf_errors)
        + len(stale_sepolicy)
    )
    print(f"RUNTIME_CONTRACT_FAILURES={failures}")
    if failures:
        print("TB8504_RUNTIME_CONTRACT_AUDIT=NEEDS_REVIEW")
        return 1
    print("TB8504_RUNTIME_CONTRACT_AUDIT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
