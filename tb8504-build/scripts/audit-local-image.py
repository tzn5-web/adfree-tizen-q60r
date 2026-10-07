#!/usr/bin/env python3
"""Fail-closed local boot/recovery image auditor for TB8504 Android 16."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import lzma
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

PARTITION_LIMIT = 67108864
EXPECTED_DTB = "tb8504-msm8917-pmi8937-qrd-sku5.dtb"
REQUIRED_KERNEL_CONFIG = (
    "CONFIG_MACH_LENOVO_TB8504=y",
    "CONFIG_ARCH_MSM8937=y",
    "CONFIG_EXT4_ENCRYPTION=y",
    "CONFIG_FS_ENCRYPTION=y",
    "CONFIG_KEYS=y",
    "CONFIG_CRYPTO_AES=y",
    "CONFIG_CRYPTO_XTS=y",
    "CONFIG_CRYPTO_CTS=y",
    "CONFIG_CRYPTO_CBC=y",
    "CONFIG_CRYPTO_SHA256=y",
    "CONFIG_MODULE_SIG=y",
    "CONFIG_MODULE_SIG_FORCE=y",
    "CONFIG_MODULE_SIG_ALL=y",
)
REQUIRED_CMDLINE_TOKENS = (
    "console=null",
    "androidboot.hardware=qcom",
    "msm_rtb.filter=0x237",
    "ehci-hcd.park=3",
    "lpm_levels.sleep_disabled=1",
    "androidboot.bootdevice=7824900.sdhci",
    "loop.max_part=7",
)
FORBIDDEN_CMDLINE_TOKENS = {
    "androidboot.selinux=permissive",
    "androidboot.selinux=disabled",
    "androidboot.selinux=0",
    "selinux=0",
    "enforcing=0",
}
EXPECTED_MODULES = {
    "ansi_cprng.ko", "backlight.ko", "br_netfilter.ko", "evbug.ko",
    "generic_bl.ko", "lcd.ko", "mmc_block_test.ko", "mmc_test.ko",
    "rdbg.ko", "test-iosched.ko", "ufs_test.ko", "wil6210.ko",
}
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
REQUIRED_IMS = {
    "vendor.imsqmidaemon", "vendor.imsdatadaemon",
    "vendor.ims_rtp_daemon", "vendor.imsrcsservice",
}
CONTROL_RE = re.compile(
    r"^\s*(?:start|stop|restart|enable|disable)\s+([^\s#;]+)"
)
CTL_RE = re.compile(
    r"^\s*setprop\s+ctl\.(?:start|stop|restart)\s+([^\s#;]+)"
)

def fail(msg: str, code: int = 2) -> None:
    print(f"IMAGE_AUDIT_FAIL={msg}")
    raise SystemExit(code)

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def align(value: int, size: int) -> int:
    return (value + size - 1) // size * size

def parse_newc(data: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    off = 0
    while off + 110 <= len(data):
        magic = data[off:off + 6]
        if magic not in {b"070701", b"070702"}:
            fail(f"ramdisk cpio has invalid newc magic at offset {off}")
        try:
            vals = [
                int(data[off + 6 + i * 8:off + 14 + i * 8], 16)
                for i in range(13)
            ]
        except ValueError as exc:
            fail(f"ramdisk cpio header parse failed: {exc}")
        filesize = vals[6]
        namesize = vals[11]
        name_start = off + 110
        name_end = name_start + namesize
        if namesize < 1 or name_end > len(data):
            fail("ramdisk cpio name bounds invalid")
        raw_name = data[name_start:name_end - 1]
        name = raw_name.decode("utf-8", "replace")
        data_start = align(name_end, 4)
        data_end = data_start + filesize
        if data_end > len(data):
            fail(f"ramdisk cpio file bounds invalid: {name}")
        if name == "TRAILER!!!":
            break
        files[name.lstrip("./")] = data[data_start:data_end]
        off = align(data_end, 4)
    if not files:
        fail("ramdisk cpio contains no files")
    return files

def decompress_ramdisk(blob: bytes, root: Path) -> tuple[bytes, str]:
    if blob.startswith(b"070701") or blob.startswith(b"070702"):
        return blob, "cpio"
    if blob.startswith(b"\x1f\x8b"):
        return gzip.decompress(blob), "gzip"
    if blob.startswith(b"\xfd7zXZ\x00"):
        return lzma.decompress(blob), "xz"
    if blob[:4] in {b"\x04\x22\x4d\x18", b"\x02\x21\x4c\x18"}:
        candidates = [
            shutil.which("lz4"),
            str(root / "prebuilts/misc/linux-x86/lz4/lz4"),
            str(root / "out/host/linux-x86/bin/lz4"),
        ]
        tool = next((x for x in candidates if x and Path(x).is_file()), None)
        if not tool:
            fail("LZ4 ramdisk detected but no lz4 tool is available")
        proc = subprocess.run(
            [tool, "-dc"], input=blob, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            fail("LZ4 ramdisk decompression failed")
        return proc.stdout, "lz4"
    fail(f"unknown ramdisk compression magic={blob[:8].hex()}")

def read_release_report(path: Path) -> None:
    if not path.is_file():
        fail(f"release report missing: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text("utf-8", errors="replace").splitlines():
        if "=" in raw:
            k, v = raw.split("=", 1)
            values[k.strip()] = v.strip()
    expected = {
        "TARGET_RELEASE": "trunk_staging",
        "TARGET_PRODUCT": "lineage_TB8504",
        "TARGET_VARIANT": "userdebug",
        "PLATFORM_SDK_VERSION": "36",
    }
    for k, wanted in expected.items():
        if values.get(k) != wanted:
            fail(f"release gate mismatch {k}: {values.get(k)!r} != {wanted!r}")
    platform = values.get("PLATFORM_VERSION", "")
    if not platform.startswith("16"):
        fail(f"PLATFORM_VERSION is not Android 16: {platform!r}")
    lineage = values.get("LINEAGE_VERSION", "")
    if not lineage.startswith("23.2-"):
        fail(f"LINEAGE_VERSION is not 23.2: {lineage!r}")
    print("ANDROID16_RELEASE_IDENTITY=PASS")
    for key in (
        "TARGET_PRODUCT","TARGET_RELEASE","TARGET_VARIANT","PLATFORM_VERSION",
        "PLATFORM_SDK_VERSION","LINEAGE_VERSION","BUILD_ID",
    ):
        print(f"{key}={values.get(key, '')}")

def audit_modules(root: Path) -> None:
    out = root / "out/target/product/TB8504"
    stage = out / "obj/PACKAGING/depmod_vendor_intermediates/lib/modules/0.0"
    if not stage.is_dir():
        fail(f"depmod staging missing: {stage}")
    kos = sorted(stage.rglob("*.ko"))
    names = {p.name for p in kos}
    print(f"DEPMOD_KO_COUNT={len(kos)}")
    print(f"DEPMOD_UNIQUE_KO_COUNT={len(names)}")
    missing = sorted(EXPECTED_MODULES - names)
    extra = sorted(names - EXPECTED_MODULES)
    if missing or extra:
        fail(f"kernel module set mismatch missing={missing} extra={extra}")

    for meta in (
        "modules.dep", "modules.alias", "modules.softdep", "modules.symbols",
        "modules.order", "modules.builtin", "modules.builtin.modinfo",
    ):
        p = stage / meta
        print(f"DEPMOD_{meta.upper().replace('.', '_')}={'YES' if p.is_file() else 'NO'}")
    if not (stage / "modules.dep").is_file():
        fail("depmod did not produce modules.dep")

    modinfo = shutil.which("modinfo")
    openssl = shutil.which("openssl")
    cert = out / "obj/KERNEL_OBJ/signing_key.x509"
    if not modinfo or not openssl or not cert.is_file():
        fail("module-signing audit requires modinfo, openssl and signing_key.x509")

    kernel_obj = out / "obj/KERNEL_OBJ"
    kernel_release = ""
    release_file = kernel_obj / "include/config/kernel.release"
    if release_file.is_file():
        kernel_release = release_file.read_text("utf-8", errors="replace").strip()
    if not kernel_release:
        uts = kernel_obj / "include/generated/utsrelease.h"
        if uts.is_file():
            m_rel = re.search(
                r'#define\\s+UTS_RELEASE\\s+"([^"]+)"',
                uts.read_text("utf-8", errors="replace"),
            )
            if m_rel:
                kernel_release = m_rel.group(1)
    if not kernel_release:
        fail("cannot determine local kernel release for module vermagic audit")

    signer_set: set[str] = set()
    key_set: set[str] = set()
    vermagic_set: set[str] = set()
    for ko in kos:
        signer = subprocess.run(
            [modinfo, "-F", "signer", str(ko)], text=True,
            capture_output=True, check=False,
        ).stdout.strip()
        sig_key = subprocess.run(
            [modinfo, "-F", "sig_key", str(ko)], text=True,
            capture_output=True, check=False,
        ).stdout.strip()
        vermagic = subprocess.run(
            [modinfo, "-F", "vermagic", str(ko)], text=True,
            capture_output=True, check=False,
        ).stdout.strip()
        if not signer or not sig_key or not vermagic:
            fail(f"unsigned/unreadable module metadata: {ko.name}")
        if vermagic.split()[0] != kernel_release:
            fail(
                f"module vermagic release mismatch {ko.name}: "
                f"{vermagic!r} != {kernel_release!r}"
            )
        signer_set.add(signer)
        key_set.add(re.sub(r"[^0-9a-fA-F]", "", sig_key).lower())
        vermagic_set.add(vermagic)

    if len(signer_set) != 1 or len(key_set) != 1:
        fail(f"module signing identity is not coherent signers={signer_set} keys={key_set}")

    cert_text = ""
    for args in (
        [openssl, "x509", "-in", str(cert), "-noout", "-text"],
        [openssl, "x509", "-inform", "DER", "-in", str(cert), "-noout", "-text"],
    ):
        proc = subprocess.run(args, text=True, capture_output=True, check=False)
        if proc.returncode == 0:
            cert_text = proc.stdout
            break
    if not cert_text:
        fail("cannot parse local kernel signing certificate")
    m = re.search(
        r"Subject Key Identifier:\s*\n\s*([0-9A-Fa-f:]+)", cert_text
    )
    if not m:
        fail("local signing certificate has no Subject Key Identifier")
    cert_ski = re.sub(r"[^0-9a-fA-F]", "", m.group(1)).lower()
    module_key = next(iter(key_set))
    if module_key != cert_ski and not module_key.endswith(cert_ski):
        fail(f"module sig_key does not match local certificate SKI {module_key} != {cert_ski}")

    print(f"KERNEL_RELEASE={kernel_release}")
    print(f"MODULE_VERMAGIC_VARIANTS={len(vermagic_set)}")
    print(f"MODULE_SIGNER={next(iter(signer_set))}")
    print(f"MODULE_SIGNING_SKI={cert_ski}")
    print("MODULE_VERMAGIC_COHERENCE=PASS")
    print("MODULE_SIGNING_COHERENCE=PASS")

def audit_ramdisk(kind: str, files: dict[str, bytes]) -> None:
    names = sorted(files)
    bases = {Path(x).name for x in names}
    print(f"RAMDISK_FILE_COUNT={len(names)}")
    if "init" not in bases:
        fail(f"{kind} ramdisk has no init")

    if kind == "boot":
        target_names = [n for n in names if Path(n).name == "init.target.rc"]
        if not target_names:
            fail("boot ramdisk has no init.target.rc")
        text = "\n".join(
            files[n].decode("utf-8", "replace") for n in target_names
        )
        for service in REQUIRED_IMS:
            if f"service {service} " not in text:
                fail(f"boot ramdisk missing restored IMS service: {service}")

        stale: list[str] = []
        for name, data in files.items():
            if not (name.endswith(".sh") or name.endswith(".rc")):
                continue
            for no, raw in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                m = CONTROL_RE.match(raw) or CTL_RE.match(raw)
                if m and m.group(1) in ABSENT_SERVICES:
                    stale.append(f"{name}:{no}:{m.group(1)}")
        if stale:
            fail(f"boot ramdisk has stale removed-service controls: {stale[:20]}")
        print("BOOT_RAMDISK_RUNTIME_CONTRACTS=PASS")
    else:
        if "init.recovery.qcom.rc" not in bases:
            fail("recovery ramdisk missing init.recovery.qcom.rc")
        fstab = [n for n in names if "fstab" in Path(n).name]
        if not fstab:
            fail("recovery ramdisk has no fstab")
        recovery_bins = [n for n in names if Path(n).name == "recovery"]
        if not recovery_bins:
            fail("recovery ramdisk has no recovery executable")
        print("RECOVERY_FSTAB_FILES=" + ",".join(fstab))
        print("RECOVERY_RAMDISK_CONTRACTS=PASS")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--kind", required=True, choices=("boot", "recovery"))
    ap.add_argument("--release-report", required=True, type=Path)
    ap.add_argument("--image", type=Path)
    args = ap.parse_args()

    root = args.root.resolve()
    out = root / "out/target/product/TB8504"
    image = args.image.resolve() if args.image else out / f"{args.kind}.img"
    if not image.is_file():
        fail(f"image missing: {image}")

    read_release_report(args.release_report)
    data = image.read_bytes()
    print(f"IMAGE={image}")
    print(f"IMAGE_SIZE={len(data)}")
    print(f"IMAGE_SHA256={sha256(data)}")
    if len(data) <= 0 or len(data) > PARTITION_LIMIT:
        fail(f"image size invalid {len(data)} > {PARTITION_LIMIT}")
    if len(data) < 64 or data[:8] != b"ANDROID!":
        fail("legacy Android boot magic missing")

    vals = struct.unpack_from("<10I", data, 8)
    (
        kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second_size,
        second_addr, tags_addr, page_size, header_version, os_version,
    ) = vals
    print(f"HEADER_VERSION={header_version}")
    print(f"PAGE_SIZE={page_size}")
    print(f"KERNEL_SIZE={kernel_size}")
    print(f"KERNEL_ADDR=0x{kernel_addr:08x}")
    print(f"RAMDISK_SIZE={ramdisk_size}")
    print(f"RAMDISK_ADDR=0x{ramdisk_addr:08x}")
    print(f"SECOND_SIZE={second_size}")
    print(f"TAGS_ADDR=0x{tags_addr:08x}")
    expected = {
        "header_version": (header_version, 0),
        "page_size": (page_size, 2048),
        "kernel_addr": (kernel_addr, 0x80008000),
        "ramdisk_addr": (ramdisk_addr, 0x81000000),
        "tags_addr": (tags_addr, 0x80000100),
        "second_size": (second_size, 0),
    }
    for label, (actual, wanted) in expected.items():
        if actual != wanted:
            fail(f"{label} mismatch actual={actual:#x} wanted={wanted:#x}")
    if kernel_size <= 0 or ramdisk_size <= 0:
        fail("kernel or ramdisk payload is empty")

    # Legacy v0 header stores cmdline at 64..575 and extra cmdline at
    # 608..1631. Validate the hardware-critical tokens rather than trusting
    # that mkbootimg inherited them correctly.
    cmdline = (
        data[64:576].split(b"\x00", 1)[0]
        + b" "
        + data[608:1632].split(b"\x00", 1)[0]
    ).decode("ascii", "replace").strip()
    print(f"BOOT_CMDLINE={cmdline}")
    cmdline_tokens = set(cmdline.split())
    missing_cmdline = [
        token for token in REQUIRED_CMDLINE_TOKENS if token not in cmdline_tokens
    ]
    if missing_cmdline:
        fail(f"required TB8504 kernel cmdline tokens missing: {missing_cmdline}")
    forbidden_cmdline=sorted(cmdline_tokens & FORBIDDEN_CMDLINE_TOKENS)
    if forbidden_cmdline:
        fail(
            "SELinux permissive/disabled kernel cmdline forbidden: "
            f"{forbidden_cmdline}"
        )
    print("BOOT_CMDLINE_SELINUX_ENFORCEMENT=PASS")
    print("BOOT_CMDLINE_CONTRACT=PASS")

    kernel_off = page_size
    ramdisk_off = page_size + align(kernel_size, page_size)
    kernel = data[kernel_off:kernel_off + kernel_size]
    ramdisk = data[ramdisk_off:ramdisk_off + ramdisk_size]
    if len(kernel) != kernel_size or len(ramdisk) != ramdisk_size:
        fail("image payload bounds are truncated")
    kernel_sha = sha256(kernel)
    print(f"IMAGE_KERNEL_SHA256={kernel_sha}")

    candidates = [
        out / "kernel",
        out / "obj/KERNEL_OBJ/arch/arm64/boot/Image.gz-dtb",
    ]
    candidate_rows = []
    match = False
    for p in candidates:
        if p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            candidate_rows.append((p, h))
            print(f"KERNEL_CANDIDATE={p}")
            print(f"KERNEL_CANDIDATE_SHA256={h}")
            if h == kernel_sha:
                match = True
    if not match:
        fail(f"image kernel does not match current local kernel candidates: {candidate_rows}")
    print("IMAGE_KERNEL_MATCH=PASS")

    kernel_obj = out / "obj/KERNEL_OBJ"
    config = kernel_obj / ".config"
    if not config.is_file():
        fail(f"local kernel config missing: {config}")
    config_lines = set(config.read_text("utf-8", errors="replace").splitlines())
    missing_config = [
        line for line in REQUIRED_KERNEL_CONFIG if line not in config_lines
    ]
    if missing_config:
        fail(f"required local kernel config missing: {missing_config}")
    print("LOCAL_KERNEL_CONFIG_AUDIT=PASS")

    dtb_root = kernel_obj / "arch/arm64/boot/dts"
    dtbs = sorted(dtb_root.rglob(EXPECTED_DTB)) if dtb_root.is_dir() else []
    if len(dtbs) != 1 or not dtbs[0].is_file() or dtbs[0].stat().st_size <= 0:
        fail(f"expected TB8504 DTB not uniquely present: {dtbs}")
    dtb_data = dtbs[0].read_bytes()
    print(f"LOCAL_DTB={dtbs[0]}")
    print(f"LOCAL_DTB_SIZE={len(dtb_data)}")
    print(f"LOCAL_DTB_SHA256={sha256(dtb_data)}")
    if kernel.find(dtb_data) < 0:
        fail("expected TB8504 DTB is not embedded in boot/recovery kernel payload")
    print("LOCAL_DTB_EMBEDDED=PASS")

    cpio, compression = decompress_ramdisk(ramdisk, root)
    print(f"RAMDISK_COMPRESSION={compression}")
    print(f"RAMDISK_UNCOMPRESSED_SIZE={len(cpio)}")
    files = parse_newc(cpio)
    audit_ramdisk(args.kind, files)
    audit_modules(root)

    print(f"{args.kind.upper()}_IMAGE_AUDIT=PASS")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
