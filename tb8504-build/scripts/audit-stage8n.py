#!/usr/bin/env python3
"""TB8504 post-cleanup ELF dependency audit for the exported Android 16 state.

This GitHub lane is intentionally independent of a full Android build. It
reconstructs the installed proprietary set from the patched vendor makefile,
inspects every installed ELF, and classifies DT_NEEDED edges using:
  * installed proprietary providers, with ELF-class matching;
  * local device-tree modules, with Android.mk multilib awareness;
  * PRODUCT_PACKAGES selected by the exported device tree;
  * pinned external source modules proven at the exported source SHA;
  * conservative Android platform/VNDK providers.

A final product-output audit on the integrated build remains authoritative.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

COPY_RE = re.compile(r"^\s*(vendor/lenovo/TB8504/proprietary/[^:]+):([^\s\\]+)")
LOCAL_MODULE_RE = re.compile(r"^\s*LOCAL_MODULE\s*:?=\s*([^\s\\#]+)", re.M)
LOCAL_MULTILIB_RE = re.compile(r"^\s*LOCAL_MULTILIB\s*:?=\s*([^\s\\#]+)", re.M)
LOCAL_32_VALUE_RE = re.compile(r"^\s*LOCAL_32_BIT_ONLY\s*:?=\s*([^\s\\#]+)", re.M)
LOCAL_64_VALUE_RE = re.compile(r"^\s*LOCAL_64_BIT_ONLY\s*:?=\s*([^\s\\#]+)", re.M)
LOCAL_ARCH_RE = re.compile(r"^\s*LOCAL_MODULE_TARGET_ARCH\s*:?=\s*([^\n#]+)", re.M)
MAKE_VAR_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:?=\s*([^#\\]+?)\s*$", re.M)
MAKE_REF_RE = re.compile(r"^\$\(([A-Za-z0-9_]+)\)$")
BP_NAME_RE = re.compile(r'\bname\s*:\s*"([^"]+)"')
BP_MULTILIB_RE = re.compile(r'\bcompile_multilib\s*:\s*"([^"]+)"')
NEEDED_RE = re.compile(r"\(NEEDED\).*\[([^\]]+)\]")
SONAME_RE = re.compile(r"\(SONAME\).*\[([^\]]+)\]")

PLATFORM_EXACT = {
    "ld-android.so", "libEGL.so", "libGLESv1_CM.so", "libGLESv2.so",
    "libOpenCL.so", "libRS.so", "libRSCpuRef.so", "libRS_internal.so",
    "libandroid.so", "libandroid_runtime.so", "libaudioroute.so",
    "libaudioclient.so", "libaudiofoundation.so", "libaudioutils.so",
    "libbase.so", "libbcinfo.so", "libbinder.so", "libbinder_ndk.so",
    "libc.so", "libc++.so", "libcamera_client.so", "libcamera_metadata.so",
    "libcrypto.so", "libcutils.so", "libdl.so", "libexpat.so", "libfmq.so",
    "libgui.so", "libhardware.so", "libhardware_legacy.so", "libhidlbase.so",
    "libhidltransport.so", "libhwbinder.so", "libion.so", "libjpeg.so",
    "liblog.so", "liblz4.so", "liblzma.so", "libm.so", "libmedia.so",
    "libmedia_omx.so", "libmediandk.so", "libmediautils.so",
    "libnativehelper.so", "libnativewindow.so", "libnetd_client.so",
    "libnetutils.so", "libnl.so", "libpng.so", "libpower.so",
    "libprocessgroup.so", "libprotobuf-cpp-full.so", "libprotobuf-cpp-lite.so",
    "libril.so", "librilutils.so", "libselinux.so", "libsqlite.so",
    "libssl.so", "libstagefright.so", "libstagefright_foundation.so",
    "libstdc++.so", "libsync.so", "libtinyalsa.so", "libtinyxml2.so",
    "libui.so", "libunwind.so", "libutils.so", "libvndksupport.so",
    "libxml2.so", "libz.so",
}
PLATFORM_PREFIXES = (
    "android.frameworks.", "android.hardware.", "android.hidl.",
    "android.system.", "libandroid_", "libclang_rt.", "libhidl",
    "libprotobuf", "libvndk", "vendor.qti.hardware.",
)

# Modules whose existence was verified in the exact exported display source SHA
# 92dc6b713dc4b4d37959b6af1078b98dd73ba740. cc_library_shared modules build
# both target ABIs unless narrowed by compile_multilib; none of these are.
PINNED_EXTERNAL_SOURCE_BITS = {
    "libqdMetaData.so": {32, 64},
    "libqdMetaData.system.so": {32, 64},
    "libqdutils.so": {32, 64},
    "libqservice.so": {32, 64},
    "libsdmutils.so": {32, 64},
}

FORBIDDEN_BASENAMES = {
    "wfdservice", "wifidisplayhalservice", "mm-qcamera-daemon",
    "libOmxVideoDSMode.so", "libmmcamera_llvd.so", "libmmcamera_quadracfa.so",
    "libmmcamera_trueportrait_lib.so",
}
FORBIDDEN_PREFIXES = ("libwfd", "com.qualcomm.qti.wifidisplayhal@")

GNSS_HINTS = (
    "gnss", "gps", "izat", "lbs", "loc_launcher", "xtra-daemon",
    "lowi", "location",
)


def fail(message: str, code: int = 2) -> None:
    print(f"STAGE8N_SOURCE_AUDIT_FAIL={message}")
    raise SystemExit(code)


def elf_class(path: Path) -> int | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(5)
    except OSError:
        return None
    if len(head) < 5 or head[:4] != b"\x7fELF":
        return None
    return {1: 32, 2: 64}.get(head[4])


def dynamic_info(path: Path) -> tuple[list[str], str | None]:
    proc = subprocess.run(
        ["readelf", "-dW", str(path)], text=True, capture_output=True, check=False
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
        src_repo, dest = m.groups()
        rel = src_repo.removeprefix("vendor/lenovo/TB8504/")
        key = (rel, dest)
        if key in seen:
            continue
        seen.add(key)
        result.append({"source_rel": rel, "dest": dest})
    if not result:
        fail("no proprietary PRODUCT_COPY_FILES entries parsed")
    return result


def make_variables(device_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(device_root.rglob("*.mk")):
        text = path.read_text("utf-8", errors="replace")
        for match in MAKE_VAR_RE.finditer(text):
            values[match.group(1)] = match.group(2).strip()
    return values


def resolve_make_value(value: str, variables: dict[str, str]) -> str:
    seen: set[str] = set()
    current = value.strip()
    while True:
        ref = MAKE_REF_RE.fullmatch(current)
        if not ref:
            return current
        key = ref.group(1)
        if key in seen or key not in variables:
            return current
        seen.add(key)
        current = variables[key].strip()


def mk_module_bits(block: str, variables: dict[str, str]) -> set[int]:
    bit32 = LOCAL_32_VALUE_RE.search(block)
    if bit32 and resolve_make_value(bit32.group(1), variables).lower() == "true":
        return {32}

    bit64 = LOCAL_64_VALUE_RE.search(block)
    if bit64 and resolve_make_value(bit64.group(1), variables).lower() == "true":
        return {64}

    arch = LOCAL_ARCH_RE.search(block)
    if arch:
        words = resolve_make_value(arch.group(1).strip(), variables).split()
        bits: set[int] = set()
        if "arm" in words:
            bits.add(32)
        if "arm64" in words:
            bits.add(64)
        if bits:
            return bits

    multi = LOCAL_MULTILIB_RE.search(block)
    if multi:
        value = resolve_make_value(multi.group(1), variables)
        return {
            "both": {32, 64}, "32": {32}, "64": {64}, "first": {64},
        }.get(value, {64})

    return {64}


def add_module_variant(out: dict[str, set[int]], name: str, bits: set[int]) -> None:
    out.setdefault(name, set()).update(bits)
    if name.startswith("lib") and not name.endswith(".so"):
        out.setdefault(name + ".so", set()).update(bits)


def local_source_modules(
    device_root: Path, variables: dict[str, str]
) -> dict[str, set[int]]:
    modules: dict[str, set[int]] = {}
    clear_vars = "include $(CLEAR_VARS)"
    for path in device_root.rglob("Android.mk"):
        text = path.read_text("utf-8", errors="replace")
        for block in text.split(clear_vars)[1:]:
            m = LOCAL_MODULE_RE.search(block)
            if m:
                add_module_variant(
                    modules, m.group(1), mk_module_bits(block, variables)
                )

    for path in device_root.rglob("Android.bp"):
        text = path.read_text("utf-8", errors="replace")
        for m in re.finditer(r"cc_(?:library|library_shared)\s*\{(.*?)\n\}", text, re.S):
            body = m.group(1)
            nm = BP_NAME_RE.search(body)
            if not nm:
                continue
            mm = BP_MULTILIB_RE.search(body)
            value = mm.group(1) if mm else "both"
            bits = {
                "both": {32, 64}, "32": {32}, "64": {64}, "first": {64},
            }.get(value, {32, 64})
            add_module_variant(modules, nm.group(1), bits)
    return modules


def product_packages(device_root: Path) -> dict[str, set[int]]:
    packages: dict[str, set[int]] = {}
    for path in device_root.rglob("*.mk"):
        lines = path.read_text("utf-8", errors="replace").splitlines()
        active = False
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("PRODUCT_PACKAGES +="):
                active = True
                payload = stripped.split("+=", 1)[1].strip().rstrip("\\").strip()
            elif active:
                payload = stripped.rstrip("\\").strip()
            else:
                continue

            if payload and not payload.startswith("#"):
                for token in payload.split():
                    bits = {32, 64}
                    name = token
                    if token.endswith(":32"):
                        name, bits = token[:-3], {32}
                    elif token.endswith(":64"):
                        name, bits = token[:-3], {64}
                    add_module_variant(packages, name, bits)

            if not raw.rstrip().endswith("\\"):
                active = False
    return packages


def vendor_prebuilt_modules(
    vendor_root: Path, selected_packages: dict[str, set[int]]
) -> dict[str, set[int]]:
    modules: dict[str, set[int]] = {}
    bp = vendor_root / "Android.bp"
    if not bp.is_file():
        return modules

    text = bp.read_text("utf-8", errors="replace")
    for match in re.finditer(
        r"cc_prebuilt_library_shared\s*\{(.*?)\n\}", text, re.S
    ):
        body = match.group(1)
        name_match = BP_NAME_RE.search(body)
        if not name_match:
            continue
        name = name_match.group(1)
        if name not in selected_packages and f"{name}.so" not in selected_packages:
            continue

        mm = BP_MULTILIB_RE.search(body)
        value = mm.group(1) if mm else "both"
        bits = {
            "both": {32, 64}, "32": {32}, "64": {64}, "first": {64},
        }.get(value, {32, 64})
        add_module_variant(modules, name, bits)

    return modules


def external_provider_bits(
    name: str,
    local_modules: dict[str, set[int]],
    packages: dict[str, set[int]],
    vendor_prebuilts: dict[str, set[int]],
) -> tuple[set[int], str | None]:
    if name in local_modules:
        return local_modules[name], "device-source"
    if name in vendor_prebuilts:
        return vendor_prebuilts[name], "vendor-prebuilt"
    if name in PINNED_EXTERNAL_SOURCE_BITS:
        return PINNED_EXTERNAL_SOURCE_BITS[name], "pinned-source"
    if name in packages:
        return packages[name], "product-package"
    if name in PLATFORM_EXACT or name.startswith(PLATFORM_PREFIXES):
        return {32, 64}, "platform"
    return set(), None


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
    variables = make_variables(device_root)
    local_modules = local_source_modules(device_root, variables)
    packages = product_packages(device_root)
    vendor_packages = product_packages(vendor_root)
    vendor_prebuilts = vendor_prebuilt_modules(vendor_root, vendor_packages)

    missing_sources: list[str] = []
    elfs: list[dict[str, object]] = []
    providers: dict[int, dict[str, list[str]]] = {
        32: defaultdict(list), 64: defaultdict(list)
    }
    forbidden: list[str] = []

    for item in entries:
        rel, dest = str(item["source_rel"]), str(item["dest"])
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
            "source_rel": rel, "dest": dest, "basename": base, "bits": bits,
            "needed": needed, "soname": soname,
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
        fail("legacy WFD/camera payloads reappeared in install set")

    total_edges = resolved_private = resolved_external = 0
    wrong_edges: list[dict[str, object]] = []
    missing_edges: list[dict[str, object]] = []
    gnss_edges: list[dict[str, object]] = []

    for rec in elfs:
        bits = int(rec["bits"])
        consumer = str(rec["dest"])
        is_gnss = any(h in consumer.lower() for h in GNSS_HINTS)
        for need in rec["needed"]:
            total_edges += 1
            same = providers[bits].get(need, [])
            if same:
                resolved_private += 1
                status, detail = "private", ",".join(same)
            else:
                other = providers[64 if bits == 32 else 32].get(need, [])
                if other:
                    edge = {
                        "consumer": consumer, "bits": bits, "needed": need,
                        "opposite_providers": other, "provider_kind": "proprietary",
                    }
                    wrong_edges.append(edge)
                    status, detail = "wrong-bitness", ",".join(other)
                else:
                    ext_bits, kind = external_provider_bits(
                        need, local_modules, packages, vendor_prebuilts
                    )
                    if bits in ext_bits:
                        resolved_external += 1
                        status = kind or "external"
                        detail = ",".join(map(str, sorted(ext_bits)))
                    elif ext_bits:
                        edge = {
                            "consumer": consumer, "bits": bits, "needed": need,
                            "opposite_providers": [
                                f"{kind}:{','.join(map(str, sorted(ext_bits)))}"
                            ],
                            "provider_kind": kind,
                        }
                        wrong_edges.append(edge)
                        status, detail = "wrong-bitness", edge["opposite_providers"][0]
                    else:
                        missing_edges.append(
                            {"consumer": consumer, "bits": bits, "needed": need}
                        )
                        status, detail = "unresolved", ""
            if is_gnss:
                gnss_edges.append({
                    "consumer": consumer, "bits": bits, "needed": need,
                    "status": status, "detail": detail,
                })

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
        "local_source_module_names": len(local_modules),
        "product_package_names": len(packages),
        "vendor_package_names": len(vendor_packages),
        "vendor_prebuilt_module_names": len(vendor_prebuilts),
        "make_variable_names": len(variables),
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

    serial_local = {k: sorted(v) for k, v in sorted(local_modules.items())}
    serial_packages = {k: sorted(v) for k, v in sorted(packages.items())}
    serial_vendor_prebuilts = {
        k: sorted(v) for k, v in sorted(vendor_prebuilts.items())
    }
    (report_dir / "stage8n.json").write_text(
        json.dumps({
            "summary": summary,
            "unresolved": missing_by_lib,
            "wrong_bitness": wrong_by_lib,
            "local_source_modules": serial_local,
            "product_packages": serial_packages,
            "vendor_prebuilts": serial_vendor_prebuilts,
            "gnss_edges": gnss_edges,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with (report_dir / "unresolved.txt").open("w", encoding="utf-8") as out:
        for lib in sorted(missing_by_lib):
            edges = missing_by_lib[lib]
            out.write(f"{lib}\tedges={len(edges)}\n")
            for edge in edges:
                out.write(f"  {edge['bits']}\t{edge['consumer']}\n")
    with (report_dir / "wrong-bitness.txt").open("w", encoding="utf-8") as out:
        for lib in sorted(wrong_by_lib):
            edges = wrong_by_lib[lib]
            out.write(f"{lib}\tedges={len(edges)}\n")
            for edge in edges:
                out.write(
                    f"  consumer={edge['bits']}:{edge['consumer']} "
                    f"providers={','.join(edge['opposite_providers'])}\n"
                )
    with (report_dir / "gnss-deps.txt").open("w", encoding="utf-8") as out:
        for edge in gnss_edges:
            out.write(
                f"{edge['bits']}\t{edge['consumer']}\t{edge['needed']}\t"
                f"{edge['status']}\t{edge['detail']}\n"
            )

    print("=== TB8504 STAGE8N GITHUB SOURCE AUDIT ===")
    for key, value in summary.items():
        print(f"{key.upper()}={value}")
    for lib in sorted(missing_by_lib):
        consumers = ",".join(
            f"{edge['bits']}:{edge['consumer']}" for edge in missing_by_lib[lib]
        )
        print(f"UNRESOLVED_LIB={lib}|{consumers}")
    for lib in sorted(wrong_by_lib):
        consumers = ",".join(
            f"{edge['bits']}:{edge['consumer']}" for edge in wrong_by_lib[lib]
        )
        print(f"WRONG_BITNESS_LIB={lib}|{consumers}")

    gnss_bad = [
        edge for edge in gnss_edges
        if edge["status"] in {"unresolved", "wrong-bitness"}
    ]
    print(f"GNSS_DEP_EDGES={len(gnss_edges)}")
    print(f"GNSS_BAD_EDGES={len(gnss_bad)}")
    for edge in gnss_bad:
        print(
            f"GNSS_BAD={edge['bits']}:{edge['consumer']}->{edge['needed']}:"
            f"{edge['status']}:{edge['detail']}"
        )

    if missing_edges or wrong_edges:
        print("STAGE8N_GITHUB_SOURCE_AUDIT=NEEDS_REVIEW")
        return 1
    print("STAGE8N_GITHUB_SOURCE_AUDIT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
