#!/usr/bin/env python3
"""TB8504 post-cleanup ELF dependency audit for the exported Android 16 state.

This is intentionally independent of a full Android build. It reconstructs the
installed proprietary set from the patched vendor makefile, inspects every ELF
in that set, and classifies DT_NEEDED edges as:
  * resolved by another installed proprietary ELF of the same bitness;
  * provided by a source-built/platform module;
  * wrong-bitness (provider exists, but only for the opposite ELF class);
  * unresolved proprietary candidate.

The full product-output audit remains the final authority, but this lane catches
blob-set/linker defects in GitHub before the next local integrated build.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

COPY_RE = re.compile(
    r"^\s*(vendor/lenovo/TB8504/proprietary/[^:]+):([^\s\\]+)"
)
LOCAL_MODULE_RE = re.compile(r"^\s*LOCAL_MODULE\s*:?=\s*([^\s\\#]+)", re.M)
BP_NAME_RE = re.compile(r'\bname\s*:\s*"([^"]+)"')
NEEDED_RE = re.compile(r"\(NEEDED\).*\[([^\]]+)\]")
SONAME_RE = re.compile(r"\(SONAME\).*\[([^\]]+)\]")

PLATFORM_EXACT = {
    "ld-android.so", "libEGL.so", "libGLESv1_CM.so", "libGLESv2.so",
    "libOpenCL.so", "libRS.so", "libandroid.so", "libandroid_runtime.so",
    "libaudioclient.so", "libaudiofoundation.so", "libaudioutils.so",
    "libbase.so", "libbinder.so", "libbinder_ndk.so", "libc.so", "libc++.so",
    "libcamera_client.so", "libcamera_metadata.so", "libcrypto.so",
    "libcutils.so", "libdl.so", "libexpat.so", "libfmq.so", "libgui.so",
    "libhardware.so", "libhardware_legacy.so", "libhidlbase.so",
    "libhidltransport.so", "libion.so", "libjpeg.so", "liblog.so",
    "liblz4.so", "liblzma.so", "libm.so", "libmedia.so", "libmediandk.so",
    "libmediautils.so", "libnativewindow.so", "libnetd_client.so",
    "libnetutils.so", "libnl.so", "libpng.so", "libprocessgroup.so",
    "libprotobuf-cpp-full.so", "libprotobuf-cpp-lite.so", "libselinux.so",
    "libstagefright.so", "libstagefright_foundation.so", "libstdc++.so",
    "libsync.so", "libtinyalsa.so", "libtinyxml2.so", "libui.so",
    "libunwind.so", "libutils.so", "libvndksupport.so", "libz.so",
}
PLATFORM_PREFIXES = (
    "android.frameworks.", "android.hardware.", "android.hidl.",
    "android.system.", "libandroid_", "libclang_rt.", "libhidl",
    "libprotobuf", "libvndk", "vendor.qti.hardware.",
)

FORBIDDEN_BASENAMES = {
    "wfdservice", "wifidisplayhalservice", "mm-qcamera-daemon",
    "libOmxVideoDSMode.so",
    "libmmcamera_llvd.so", "libmmcamera_quadracfa.so",
    "libmmcamera_trueportrait_lib.so",
}
FORBIDDEN_PREFIXES = ("libwfd", "com.qualcomm.qti.wifidisplayhal@")


def fail(message: str, code: int = 2) -> None:
    print(f"STAGE8N_SOURCE_AUDIT_FAIL={message}")
    raise SystemExit(code)


def elf_class(path: Path) -> int | None:
    try:
        head = path.read_bytes()[:5]
    except OSError:
        return None
    if len(head) < 5 or head[:4] != b"\x7fELF":
        return None
    if head[4] == 1:
        return 32
    if head[4] == 2:
        return 64
    return None


def dynamic_info(path: Path) -> tuple[list[str], str | None]:
    proc = subprocess.run(
        ["readelf", "-dW", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return [], None
    needed = NEEDED_RE.findall(proc.stdout)
    sonames = SONAME_RE.findall(proc.stdout)
    return needed, sonames[-1] if sonames else None


def installed_entries(vendor_root: Path) -> list[dict[str, str]]:
    mk = vendor_root / "TB8504-vendor.mk"
    if not mk.is_file():
        fail(f"missing vendor makefile: {mk}")

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in mk.read_text("utf-8", errors="replace").splitlines():
        m = COPY_RE.match(raw)
        if not m:
            continue
        src_repo = m.group(1)
        dest = m.group(2)
        rel = src_repo.removeprefix("vendor/lenovo/TB8504/")
        key = (rel, dest)
        if key in seen:
            continue
        seen.add(key)
        result.append({"source_rel": rel, "dest": dest})
    if not result:
        fail("no proprietary PRODUCT_COPY_FILES entries parsed")
    return result


def source_modules(device_root: Path) -> set[str]:
    modules: set[str] = set()
    for path in device_root.rglob("Android.mk"):
        text = path.read_text("utf-8", errors="replace")
        modules.update(LOCAL_MODULE_RE.findall(text))
    for path in device_root.rglob("Android.bp"):
        text = path.read_text("utf-8", errors="replace")
        modules.update(BP_NAME_RE.findall(text))

    normalized = set(modules)
    for name in list(modules):
        if name.startswith("lib") and not name.endswith(".so"):
            normalized.add(name + ".so")
    return normalized


def is_platform_or_source(name: str, modules: set[str]) -> bool:
    if name in PLATFORM_EXACT or name in modules:
        return True
    return name.startswith(PLATFORM_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", required=True, type=Path)
    ap.add_argument("--device", required=True, type=Path)
    ap.add_argument("--report-dir", required=True, type=Path)
    args = ap.parse_args()

    vendor_root = args.vendor.resolve()
    device_root = args.device.resolve()
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    if not vendor_root.is_dir() or not device_root.is_dir():
        fail("vendor/device root missing")

    entries = installed_entries(vendor_root)
    modules = source_modules(device_root)

    missing_sources: list[str] = []
    elfs: list[dict[str, object]] = []
    providers: dict[int, dict[str, list[str]]] = {
        32: defaultdict(list), 64: defaultdict(list)
    }
    forbidden: list[str] = []

    for item in entries:
        rel = str(item["source_rel"])
        dest = str(item["dest"])
        path = vendor_root / rel
        if not path.is_file():
            missing_sources.append(rel)
            continue

        base = Path(dest).name
        if base in FORBIDDEN_BASENAMES or base.startswith(FORBIDDEN_PREFIXES):
            forbidden.append(dest)

        bits = elf_class(path)
        if bits is None:
            continue
        needed, soname = dynamic_info(path)
        rec = {
            "source_rel": rel,
            "dest": dest,
            "basename": base,
            "bits": bits,
            "needed": needed,
            "soname": soname,
        }
        elfs.append(rec)
        for key in {base, path.name, soname} - {None}:
            providers[bits][str(key)].append(dest)

    if missing_sources:
        print(f"MISSING_INSTALL_SOURCES={len(missing_sources)}")
        for rel in missing_sources[:100]:
            print(f"MISSING_INSTALL_SOURCE={rel}")
        fail("patched vendor makefile references missing source files")

    if forbidden:
        print(f"FORBIDDEN_LEGACY_INSTALLS={len(forbidden)}")
        for dest in forbidden:
            print(f"FORBIDDEN_LEGACY_INSTALL={dest}")
        fail("legacy WFD/camera/DSI payloads reappeared in install set")

    total_edges = 0
    resolved_private = 0
    resolved_external = 0
    wrong_edges: list[dict[str, object]] = []
    missing_edges: list[dict[str, object]] = []

    for rec in elfs:
        bits = int(rec["bits"])
        for need in rec["needed"]:
            total_edges += 1
            same = providers[bits].get(need, [])
            if same:
                resolved_private += 1
                continue
            other = providers[64 if bits == 32 else 32].get(need, [])
            if other:
                wrong_edges.append(
                    {
                        "consumer": rec["dest"], "bits": bits,
                        "needed": need, "opposite_providers": other,
                    }
                )
                continue
            if is_platform_or_source(need, modules):
                resolved_external += 1
                continue
            missing_edges.append(
                {"consumer": rec["dest"], "bits": bits, "needed": need}
            )

    missing_by_lib: dict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in missing_edges:
        missing_by_lib[str(edge["needed"])].append(edge)
    wrong_by_lib: dict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in wrong_edges:
        wrong_by_lib[str(edge["needed"])].append(edge)

    summary = {
        "copy_entries": len(entries),
        "elf_files": len(elfs),
        "elf32_files": sum(1 for x in elfs if x["bits"] == 32),
        "elf64_files": sum(1 for x in elfs if x["bits"] == 64),
        "source_module_names": len(modules),
        "dt_needed_edges": total_edges,
        "resolved_private_edges": resolved_private,
        "resolved_external_edges": resolved_external,
        "unresolved_edges": len(missing_edges),
        "unresolved_unique_libs": len(missing_by_lib),
        "wrong_bitness_edges": len(wrong_edges),
        "wrong_bitness_unique_libs": len(wrong_by_lib),
        "historical_stage8n2_unresolved_edges": 44,
        "historical_stage8n2_unresolved_unique_libs": 11,
        "historical_stage8n2_wrong_bitness_edges": 2,
        "historical_stage8n2_wrong_bitness_unique_libs": 2,
    }

    (report_dir / "stage8n.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "unresolved": missing_by_lib,
                "wrong_bitness": wrong_by_lib,
                "source_modules": sorted(modules),
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )

    with (report_dir / "unresolved.txt").open("w", encoding="utf-8") as f:
        for lib in sorted(missing_by_lib):
            edges = missing_by_lib[lib]
            f.write(f"{lib}\tedges={len(edges)}\n")
            for e in edges:
                f.write(f"  {e['bits']}\t{e['consumer']}\n")

    with (report_dir / "wrong-bitness.txt").open("w", encoding="utf-8") as f:
        for lib in sorted(wrong_by_lib):
            edges = wrong_by_lib[lib]
            f.write(f"{lib}\tedges={len(edges)}\n")
            for e in edges:
                f.write(
                    f"  consumer={e['bits']}:{e['consumer']} "
                    f"providers={','.join(e['opposite_providers'])}\n"
                )

    print("=== TB8504 STAGE8N GITHUB SOURCE AUDIT ===")
    for key, value in summary.items():
        print(f"{key.upper()}={value}")

    for lib in sorted(missing_by_lib):
        consumers = ",".join(
            f"{e['bits']}:{e['consumer']}" for e in missing_by_lib[lib]
        )
        print(f"UNRESOLVED_LIB={lib}|{consumers}")
    for lib in sorted(wrong_by_lib):
        consumers = ",".join(
            f"{e['bits']}:{e['consumer']}" for e in wrong_by_lib[lib]
        )
        print(f"WRONG_BITNESS_LIB={lib}|{consumers}")

    if missing_edges or wrong_edges:
        print("STAGE8N_GITHUB_SOURCE_AUDIT=NEEDS_REVIEW")
        return 1

    print("STAGE8N_GITHUB_SOURCE_AUDIT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
