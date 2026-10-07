#!/usr/bin/env python3
"""Audit actual TB8504 built output after the final Android 16 build.

This complements source-side STAGE8N. It inspects what was actually emitted
under out/target/product/TB8504 and fails closed on the runtime contracts that
were historically problematic during the TB8504 bring-up.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import tempfile
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

EXPECTED_IMS = {
    "vendor.imsqmidaemon": "imsqmidaemon",
    "vendor.imsdatadaemon": "imsdatadaemon",
    "vendor.ims_rtp_daemon": "ims_rtp_daemon",
    "vendor.imsrcsservice": "imsrcsd",
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
EXPECTED_MODULES = {
    "ansi_cprng.ko","backlight.ko","br_netfilter.ko","evbug.ko",
    "generic_bl.ko","lcd.ko","mmc_block_test.ko","mmc_test.ko",
    "rdbg.ko","test-iosched.ko","ufs_test.ko","wil6210.ko",
}
PROHIBITED_OUTPUT_BASENAMES = {
    "WfdService.apk",
    "WfdCommon.jar",
    "wfdservice",
    "wifidisplayhalservice",
    "fstman",
}
CONTROL_RE = re.compile(r"^\s*(?:start|stop|restart|enable|disable)\s+([^\s#;]+)")
CTL_RE = re.compile(r"^\s*setprop\s+ctl\.(?:start|stop|restart)\s+([^\s#;]+)")
SERVICE_RE = re.compile(r"^service\s+(\S+)\s+([^\s\\]+)")
NEEDED_RE = re.compile(r"\(NEEDED\).*\[([^\]]+)\]")
SONAME_RE = re.compile(r"\(SONAME\).*\[([^\]]+)\]")

def fail(msg: str) -> None:
    print(f"BUILT_OUTPUT_AUDIT_FAIL={msg}")
    raise SystemExit(2)

def sha(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False

def elf_class(path: Path) -> int:
    with path.open("rb") as f:
        hdr=f.read(5)
    if len(hdr) < 5 or hdr[:4] != b"\x7fELF":
        return 0
    return hdr[4]

def installed_roots(out: Path) -> list[Path]:
    roots=[]
    for rel in ("root","system","vendor","product","system_ext","odm"):
        p=out/rel
        if p.is_dir():
            roots.append(p)
    # Legacy integrated vendor.
    if (out/"system/vendor").is_dir():
        roots.append(out/"system/vendor")
    return list(dict.fromkeys(roots))

def find_installed(out: Path, basename: str) -> list[Path]:
    hits=[]
    for root in installed_roots(out):
        for p in root.rglob(basename):
            if p.is_file() or p.is_symlink():
                hits.append(p)
    return list(dict.fromkeys(hits))

def parse_init(out: Path) -> tuple[dict[str, tuple[str, Path, int]], list[str]]:
    services={}
    stale=[]
    for root in installed_roots(out):
        for p in root.rglob("*.rc"):
            try:
                lines=p.read_text("utf-8",errors="replace").splitlines()
            except OSError:
                continue
            for no,line in enumerate(lines,1):
                m=SERVICE_RE.match(line)
                if m:
                    services.setdefault(m.group(1),(m.group(2),p,no))
                c=CONTROL_RE.match(line) or CTL_RE.match(line)
                if c and c.group(1) in ABSENT_SERVICES:
                    stale.append(f"{p}:{no}:{c.group(1)}")
    return services,stale

def parse_vendor_copy_entries(vmk: Path) -> list[tuple[str,str]]:
    entries=[]
    for raw in vmk.read_text("utf-8",errors="replace").splitlines():
        s=raw.strip().rstrip("\\").strip()
        if ":" not in s:
            continue
        src,dst=s.split(":",1)
        if not src.startswith("vendor/lenovo/TB8504/proprietary/"):
            continue
        if "$(TARGET_COPY_OUT_VENDOR)/" not in dst:
            continue
        entries.append((src,dst.split("$(TARGET_COPY_OUT_VENDOR)/",1)[1]))
    return entries

def choose_vendor_root(out: Path, entries: list[tuple[str,str]]) -> Path:
    candidates=[
        p for p in (out/"vendor", out/"system/vendor") if p.is_dir()
    ]
    if not candidates:
        fail("no installed vendor root found in built output")
    scored=[]
    for candidate in candidates:
        score=sum(1 for _,dst_rel in entries if (candidate/dst_rel).exists())
        scored.append((score,candidate))
    scored.sort(key=lambda x:x[0], reverse=True)
    print("VENDOR_ROOT_CANDIDATES=" + ",".join(
        f"{p}:{score}" for score,p in scored
    ))
    if scored[0][0] == 0:
        fail("no vendor root candidate contains generated copy destinations")
    return scored[0][1]


def dynamic_info(path: Path) -> tuple[list[str], str | None]:
    proc=subprocess.run(
        ["readelf","-dW",str(path)],
        text=True,capture_output=True,check=False,
    )
    if proc.returncode!=0:
        fail(f"readelf dynamic audit failed: {path}: {proc.stderr.strip()}")
    needed=NEEDED_RE.findall(proc.stdout)
    sonames=SONAME_RE.findall(proc.stdout)
    return needed,(sonames[-1] if sonames else None)

def audit_actual_vendor_elf_dependencies(root: Path, out: Path) -> None:
    """Re-audit DT_NEEDED on the proprietary ELF files actually installed.

    Providers are discovered from all staged installed roots so this validates
    the final product topology rather than trusting the source-side graph.
    Resolution is bitness-sensitive and fail-closed.
    """
    vmk=root/"vendor/lenovo/TB8504/TB8504-vendor.mk"
    entries=parse_vendor_copy_entries(vmk)
    vendor_root=choose_vendor_root(out,entries)

    providers={1:{},2:{}}
    scanned=set()
    for install_root in installed_roots(out):
        for p in install_root.rglob("*"):
            if not p.is_file() or p in scanned or not is_elf(p):
                continue
            scanned.add(p)
            cls=elf_class(p)
            if cls not in (1,2):
                continue
            needed,soname=dynamic_info(p)
            names={p.name}
            if soname:
                names.add(soname)
            for name in names:
                providers[cls].setdefault(name,[]).append(str(p))

    consumers=[]
    for _src_rel,dst_rel in entries:
        p=vendor_root/dst_rel
        if not p.is_file() or not is_elf(p):
            continue
        cls=elf_class(p)
        if cls not in (1,2):
            fail(f"invalid installed ELF class: {p}")
        needed,soname=dynamic_info(p)
        consumers.append((p,cls,needed,soname))

    total_edges=0
    unresolved=[]
    wrong=[]
    for p,cls,needed,_soname in consumers:
        other=2 if cls==1 else 1
        for lib in needed:
            total_edges+=1
            if providers[cls].get(lib):
                continue
            if providers[other].get(lib):
                wrong.append({
                    "consumer":str(p),
                    "class":32 if cls==1 else 64,
                    "needed":lib,
                    "opposite_providers":providers[other][lib][:8],
                })
            else:
                unresolved.append({
                    "consumer":str(p),
                    "class":32 if cls==1 else 64,
                    "needed":lib,
                })

    report={
        "consumer_elfs":len(consumers),
        "provider_elfs":len(scanned),
        "dt_needed_edges":total_edges,
        "unresolved_edges":len(unresolved),
        "wrong_bitness_edges":len(wrong),
        "unresolved":unresolved,
        "wrong_bitness":wrong,
    }
    print(f"ACTUAL_VENDOR_ELF_CONSUMERS={report['consumer_elfs']}")
    print(f"ACTUAL_OUTPUT_ELF_PROVIDERS={report['provider_elfs']}")
    print(f"ACTUAL_OUTPUT_DT_NEEDED_EDGES={report['dt_needed_edges']}")
    print(f"ACTUAL_OUTPUT_UNRESOLVED_EDGES={report['unresolved_edges']}")
    print(f"ACTUAL_OUTPUT_WRONG_BITNESS_EDGES={report['wrong_bitness_edges']}")
    if unresolved:
        fail(f"actual output unresolved DT_NEEDED edges: {unresolved[:30]}")
    if wrong:
        fail(f"actual output wrong-bitness DT_NEEDED edges: {wrong[:30]}")
    print("ACTUAL_OUTPUT_ELF_DEPENDENCY_AUDIT=PASS")

def audit_vendor_copy(root: Path, out: Path) -> None:
    vmk=root/"vendor/lenovo/TB8504/TB8504-vendor.mk"
    if not vmk.is_file():
        fail("TB8504-vendor.mk missing")
    entries=parse_vendor_copy_entries(vmk)
    vendor_root=choose_vendor_root(out,entries)
    if len(entries) < 100:
        fail(f"unexpectedly few vendor copy entries: {len(entries)}")
    missing=[]
    elf_checked=0
    elf_mismatch=[]
    for src_rel,dst_rel in entries:
        src=root/src_rel
        dst=vendor_root/dst_rel
        if not src.is_file():
            fail(f"vendor source copy input missing: {src_rel}")
        if not dst.exists():
            missing.append(dst_rel)
            continue
        if is_elf(src):
            elf_checked += 1
            if not dst.is_file() or not is_elf(dst):
                elf_mismatch.append(f"{dst_rel}:not-ELF")
            elif elf_class(src) != elf_class(dst):
                elf_mismatch.append(
                    f"{dst_rel}:class{elf_class(src)}->{elf_class(dst)}"
                )
    print(f"VENDOR_COPY_ENTRIES={len(entries)}")
    print(f"VENDOR_COPY_MISSING={len(missing)}")
    print(f"VENDOR_COPY_ELF_CHECKED={elf_checked}")
    print(f"VENDOR_COPY_ELF_CLASS_MISMATCH={len(elf_mismatch)}")
    if missing:
        fail(f"installed vendor copy destinations missing: {missing[:30]}")
    if elf_mismatch:
        fail(f"installed vendor ELF class mismatch: {elf_mismatch[:30]}")
    print("VENDOR_COPY_OUTPUT_AUDIT=PASS")

def audit_build_props(out: Path) -> None:
    props=[]
    for root in installed_roots(out):
        for p in root.rglob("build.prop"):
            try:
                props.append((p,p.read_text("utf-8",errors="replace")))
            except OSError:
                pass
    if not props:
        fail("no built build.prop files found")
    combined="\n".join(t for _,t in props)
    if "ro.build.version.sdk=36" not in combined:
        fail("built properties do not prove SDK 36")
    if (
        "ro.build.version.release_or_codename=16" not in combined
        and "ro.build.version.release=16" not in combined
    ):
        fail("built properties do not prove Android 16")

    def values(key: str) -> set[str]:
        result=set()
        prefix=key+"="
        for _path,text in props:
            for raw in text.splitlines():
                s=raw.strip()
                if s.startswith(prefix):
                    result.add(s[len(prefix):].strip())
        return result

    performance_props={
        "ro.surface_flinger.supports_background_blur":{"0"},
        "ro.config.avoid_gfx_accel":{"true"},
    }
    for key,wanted in performance_props.items():
        actual=values(key)
        print(f"BUILT_PERFORMANCE_PROP={key}={','.join(sorted(actual))}")
        if actual!=wanted:
            fail(f"built performance property mismatch {key}: {actual} != {wanted}")

    stale=values("debug.sf.disable_backpressure")
    print(
        "BUILT_STALE_DISABLE_BACKPRESSURE="
        + ("ABSENT" if not stale else ",".join(sorted(stale)))
    )
    if stale:
        fail(f"stale debug.sf.disable_backpressure built property remains: {stale}")

    print(f"BUILD_PROP_FILES={len(props)}")
    print("ANDROID16_BUILT_PROPERTIES=PASS")
    print("BUILT_LOW_END_GRAPHICS_CONTRACT=PASS")

def audit_vintf(out: Path) -> None:
    xmls=[]
    stale=[]
    ims=False
    parse_errors=[]
    for root in installed_roots(out):
        for p in root.rglob("*.xml"):
            if "vintf" not in str(p).lower():
                continue
            try:
                text=p.read_text("utf-8",errors="replace")
            except OSError:
                continue
            xmls.append(p)
            if "com.qualcomm.qti.wifidisplayhal" in text:
                stale.append(str(p))
            if "vendor.qti.imsrtpservice" in text:
                ims=True
            try:
                ET.fromstring(text)
            except ET.ParseError as exc:
                parse_errors.append(f"{p}:{exc}")
    print(f"BUILT_VINTF_XML_FILES={len(xmls)}")
    print(f"BUILT_VINTF_PARSE_ERRORS={len(parse_errors)}")
    print(f"BUILT_STALE_WFD_VINTF={len(stale)}")
    if parse_errors:
        fail(f"built VINTF XML parse errors: {parse_errors[:20]}")
    if stale:
        fail(f"stale WFD HAL present in built VINTF: {stale[:20]}")
    if not ims:
        fail("built VINTF does not contain required vendor.qti.imsrtpservice")
    print("BUILT_VINTF_CONTRACTS=PASS")

def audit_runtime(out: Path) -> None:
    services,stale=parse_init(out)
    missing=[]
    wrong=[]
    for name,exe in EXPECTED_IMS.items():
        row=services.get(name)
        if not row:
            missing.append(name)
            continue
        if Path(row[0]).name != exe:
            wrong.append(f"{name}:{row[0]} != {exe}")
        if not find_installed(out,exe):
            missing.append(f"{name}:executable:{exe}")
    print(f"BUILT_INIT_SERVICES={len(services)}")
    print(f"BUILT_STALE_CONTROL_REFS={len(stale)}")
    print(f"BUILT_IMS_MISSING={len(missing)}")
    print(f"BUILT_IMS_WRONG_EXEC={len(wrong)}")
    if stale:
        fail(f"stale removed-service control refs in built output: {stale[:30]}")
    if missing or wrong:
        fail(f"IMS built runtime contract failed missing={missing} wrong={wrong}")
    print("BUILT_RUNTIME_CONTRACTS=PASS")

def audit_removed_outputs(out: Path) -> None:
    hits=[]
    for name in PROHIBITED_OUTPUT_BASENAMES:
        for p in find_installed(out,name):
            hits.append(str(p))
    print(f"PROHIBITED_REMOVED_OUTPUTS={len(hits)}")
    if hits:
        fail(f"removed WFD/fstman output still installed: {hits[:30]}")
    print("REMOVED_OUTPUT_CONTRACTS=PASS")

def audit_modules(root: Path, out: Path) -> None:
    module_roots=[]
    for p in (
        out/"vendor/lib/modules",
        out/"system/vendor/lib/modules",
        out/"system/lib/modules",
    ):
        if p.is_dir():
            module_roots.append(p)
    if not module_roots:
        fail("no installed kernel module directory found")
    kos=[]
    for mr in module_roots:
        kos.extend(p for p in mr.rglob("*.ko") if p.is_file())
    names={p.name for p in kos}
    missing=sorted(EXPECTED_MODULES-names)
    extra=sorted(names-EXPECTED_MODULES)
    print(f"INSTALLED_MODULE_DIRS={len(module_roots)}")
    print(f"INSTALLED_MODULE_FILES={len(kos)}")
    print(f"INSTALLED_MODULE_UNIQUE={len(names)}")
    if missing or extra:
        fail(f"installed module set mismatch missing={missing} extra={extra}")
    if not any((mr/"modules.dep").is_file() for mr in module_roots):
        fail("installed modules.dep missing")

    modinfo=shutil.which("modinfo")
    openssl=shutil.which("openssl")
    cert=out/"obj/KERNEL_OBJ/signing_key.x509"
    if not modinfo or not openssl or not cert.is_file():
        fail("installed module signing audit prerequisites missing")
    kernel_obj=out/"obj/KERNEL_OBJ"
    kernel_release=""
    release_file=kernel_obj/"include/config/kernel.release"
    if release_file.is_file():
        kernel_release=release_file.read_text("utf-8",errors="replace").strip()
    if not kernel_release:
        uts=kernel_obj/"include/generated/utsrelease.h"
        if uts.is_file():
            m_rel=re.search(
                r'#define\\s+UTS_RELEASE\\s+"([^"]+)"',
                uts.read_text("utf-8",errors="replace"),
            )
            if m_rel:
                kernel_release=m_rel.group(1)
    if not kernel_release:
        fail("cannot determine built kernel release for module vermagic audit")

    signers=set()
    sig_keys=set()
    vermagics=set()
    for ko in kos:
        signer=subprocess.run(
            [modinfo,"-F","signer",str(ko)],text=True,capture_output=True
        ).stdout.strip()
        key=subprocess.run(
            [modinfo,"-F","sig_key",str(ko)],text=True,capture_output=True
        ).stdout.strip()
        vermagic=subprocess.run(
            [modinfo,"-F","vermagic",str(ko)],text=True,capture_output=True
        ).stdout.strip()
        if not signer or not key or not vermagic:
            fail(f"installed unsigned/unreadable module metadata: {ko}")
        if vermagic.split()[0] != kernel_release:
            fail(
                f"installed module vermagic mismatch {ko.name}: "
                f"{vermagic!r} != {kernel_release!r}"
            )
        signers.add(signer)
        sig_keys.add(re.sub(r"[^0-9a-fA-F]","",key).lower())
        vermagics.add(vermagic)
    if len(signers)!=1 or len(sig_keys)!=1:
        fail(f"installed module signing identities diverge: {signers} {sig_keys}")

    cert_text=""
    for cmd in (
        [openssl,"x509","-in",str(cert),"-noout","-text"],
        [openssl,"x509","-inform","DER","-in",str(cert),"-noout","-text"],
    ):
        p=subprocess.run(cmd,text=True,capture_output=True)
        if p.returncode==0:
            cert_text=p.stdout
            break
    m=re.search(r"Subject Key Identifier:\s*\n\s*([0-9A-Fa-f:]+)",cert_text)
    if not m:
        fail("kernel signing cert SKI missing")
    ski=re.sub(r"[^0-9a-fA-F]","",m.group(1)).lower()
    key=next(iter(sig_keys))
    if key!=ski and not key.endswith(ski):
        fail(f"installed module sig_key != kernel cert SKI: {key} != {ski}")
    print(f"BUILT_KERNEL_RELEASE={kernel_release}")
    print(f"INSTALLED_MODULE_VERMAGIC_VARIANTS={len(vermagics)}")
    print(f"INSTALLED_MODULE_SIGNER={next(iter(signers))}")
    print(f"INSTALLED_MODULE_SIGNING_SKI={ski}")
    print("INSTALLED_MODULE_VERMAGIC_COHERENCE=PASS")
    print("INSTALLED_MODULE_SIGNING_COHERENCE=PASS")

def audit_system_image(root: Path, out: Path) -> None:
    p=out/"system.img"
    if not p.is_file() or p.stat().st_size<=0:
        fail("system.img missing/empty")
    stored=p.stat().st_size
    with p.open("rb") as f:
        header=f.read(28)
    expanded=stored
    sparse=False
    if len(header)>=28 and struct.unpack_from("<I",header,0)[0]==0xED26FF3A:
        sparse=True
        vals=struct.unpack_from("<I4H4I",header,0)
        blk_sz=vals[5]; total_blks=vals[6]
        if vals[1]!=1 or blk_sz<=0:
            fail("invalid sparse system.img header")
        expanded=blk_sz*total_blks
    limit=4080218112
    if expanded>limit:
        fail(f"system.img expanded size exceeds partition: {expanded}>{limit}")
    print(f"SYSTEM_IMAGE_SPARSE={'YES' if sparse else 'NO'}")
    print(f"SYSTEM_IMAGE_STORED_SIZE={stored}")
    print(f"SYSTEM_IMAGE_EXPANDED_SIZE={expanded}")
    print(f"SYSTEM_IMAGE_SHA256={sha(p)}")
    print("SYSTEM_IMAGE_SIZE_CONTRACT=PASS")

    host=root/"out/host/linux-x86/bin"
    simg2img=host/"simg2img"
    e2fsck=host/"e2fsck"
    if not e2fsck.is_file():
        fail(f"host e2fsck missing after Android build: {e2fsck}")
    with tempfile.TemporaryDirectory(prefix="tb8504-system-fs-") as td:
        raw=Path(td)/"system.raw.img"
        if sparse:
            if not simg2img.is_file():
                fail(f"host simg2img missing for sparse system.img: {simg2img}")
            proc=subprocess.run(
                [str(simg2img),str(p),str(raw)],
                text=True,capture_output=True,check=False,
            )
            if proc.returncode!=0 or not raw.is_file():
                fail(
                    "simg2img failed for system.img: "
                    + (proc.stderr or proc.stdout).strip()
                )
        else:
            shutil.copyfile(p,raw)

        with raw.open("rb") as fh:
            fh.seek(1024+56)
            ext_magic=fh.read(2)
        if ext_magic!=b"\x53\xef":
            fail(
                "system.img is not the expected ext filesystem "
                f"(superblock magic={ext_magic.hex()})"
            )

        proc=subprocess.run(
            [str(e2fsck),"-f","-n",str(raw)],
            text=True,capture_output=True,check=False,
        )
        print(f"SYSTEM_E2FSCK_RC={proc.returncode}")
        if proc.returncode!=0:
            tail="\n".join(
                ((proc.stdout or "")+"\n"+(proc.stderr or "")).splitlines()[-80:]
            )
            fail("system.img e2fsck failed: "+tail)
    print("SYSTEM_IMAGE_FILESYSTEM_AUDIT=PASS")


def read_first_api_level(out: Path) -> str:
    for root in installed_roots(out):
        for p in root.rglob("build.prop"):
            try:
                for raw in p.read_text("utf-8",errors="replace").splitlines():
                    if raw.startswith("ro.product.first_api_level="):
                        value=raw.split("=",1)[1].strip()
                        if value.isdigit():
                            return value
            except OSError:
                pass
    return ""

def audit_checkvintf(root: Path, out: Path) -> None:
    tool=root/"out/host/linux-x86/bin/checkvintf"
    if not tool.is_file():
        fail(f"checkvintf host tool missing after build: {tool}")

    vendor_root=out/"vendor"
    if not vendor_root.is_dir() and (out/"system/vendor").is_dir():
        vendor_root=out/"system/vendor"

    mappings=[
        ("/system",out/"system"),
        ("/vendor",vendor_root),
        ("/odm",out/"odm"),
        ("/product",out/"product"),
        ("/system_ext",out/"system_ext"),
        ("/apex",out/"apex"),
    ]
    args=[str(tool),"--check-compat"]
    mapped=[]
    for logical,physical in mappings:
        if physical.is_dir():
            args += ["--dirmap",f"{logical}:{physical}"]
            mapped.append(f"{logical}:{physical}")

    if not any(x.startswith("/system:") for x in mapped):
        fail("checkvintf cannot run: staged /system directory missing")
    if not any(x.startswith("/vendor:") for x in mapped):
        fail("checkvintf cannot run: staged /vendor directory missing")

    first_api=read_first_api_level(out)
    if first_api:
        args += ["--property",f"ro.product.first_api_level={first_api}"]

    proc=subprocess.run(args,text=True,capture_output=True,check=False)
    combined=(proc.stdout or "")+"\n"+(proc.stderr or "")
    print("CHECKVINTF_DIRMAPS="+",".join(mapped))
    print(f"CHECKVINTF_FIRST_API_LEVEL={first_api}")
    print(f"CHECKVINTF_RC={proc.returncode}")
    if proc.returncode!=0 or "COMPATIBLE" not in combined:
        fail(
            "checkvintf compatibility failed: "
            + "\n".join(combined.splitlines()[-80:])
        )
    print("CHECKVINTF_COMPATIBILITY=PASS")

def audit_rom_zip(out: Path, rom: Path) -> None:
    if not rom.is_file() or rom.stat().st_size < 1024*1024:
        fail(f"final ROM ZIP missing/implausibly small: {rom}")
    try:
        with zipfile.ZipFile(rom,"r") as z:
            bad=z.testzip()
            if bad:
                fail(f"final ROM ZIP CRC failure: {bad}")
            infos=z.infolist()
            names=[i.filename for i in infos]
            if not names:
                fail("final ROM ZIP is empty")
            unsafe=[
                n for n in names
                if n.startswith("/") or ".." in Path(n).parts
            ]
            if unsafe:
                fail(f"unsafe paths in final ROM ZIP: {unsafe[:20]}")

            metadata=""
            if "META-INF/com/android/metadata" in names:
                metadata=z.read("META-INF/com/android/metadata").decode(
                    "utf-8","replace"
                )
                pre_device=""
                for raw in metadata.splitlines():
                    if raw.startswith("pre-device="):
                        pre_device=raw.split("=",1)[1].strip()
                        break
                if pre_device and "TB8504" not in {
                    x.strip() for x in pre_device.split("|")
                }:
                    fail(f"ROM metadata pre-device does not include TB8504: {pre_device}")

            has_payload="payload.bin" in names
            has_block_system=(
                any(
                    n in names for n in (
                        "system.new.dat","system.new.dat.br","system.new.dat.xz"
                    )
                )
                and "system.transfer.list" in names
            )
            has_system_img=any(n in names for n in ("system.img","IMAGES/system.img"))
            has_updater=(
                "META-INF/com/google/android/update-binary" in names
                or "META-INF/com/google/android/updater-script" in names
            )
            if not (has_payload or has_block_system or has_system_img or has_updater):
                fail("ROM ZIP contains no recognized Android OTA/install payload")

            boot_names=[n for n in ("boot.img","IMAGES/boot.img") if n in names]
            if boot_names:
                built_boot=out/"boot.img"
                if not built_boot.is_file():
                    fail("ROM contains boot.img but staged boot.img is missing")
                built_sha=sha(built_boot)
                for name in boot_names:
                    zipped_sha=hashlib.sha256(z.read(name)).hexdigest()
                    if zipped_sha!=built_sha:
                        fail(
                            f"ROM {name} does not match staged boot.img: "
                            f"{zipped_sha} != {built_sha}"
                        )

            print(f"FINAL_ROM_ZIP_ENTRIES={len(infos)}")
            print(f"FINAL_ROM_HAS_METADATA={'YES' if metadata else 'NO'}")
            print(f"FINAL_ROM_HAS_PAYLOAD={'YES' if has_payload else 'NO'}")
            print(f"FINAL_ROM_HAS_BLOCK_SYSTEM={'YES' if has_block_system else 'NO'}")
            print(f"FINAL_ROM_HAS_SYSTEM_IMG={'YES' if has_system_img else 'NO'}")
            print(f"FINAL_ROM_HAS_UPDATER={'YES' if has_updater else 'NO'}")
            print(f"FINAL_ROM_BOOT_ENTRIES={len(boot_names)}")
    except zipfile.BadZipFile as exc:
        fail(f"final ROM ZIP parse failure: {exc}")
    print(f"FINAL_ROM_ZIP_SHA256={sha(rom)}")
    print("FINAL_ROM_ZIP_AUDIT=PASS")

def audit_policy(out: Path) -> None:
    candidates=[]
    names={
        "sepolicy","precompiled_sepolicy","plat_sepolicy.cil",
        "vendor_sepolicy.cil","mapping",
    }
    for root in installed_roots(out):
        for p in root.rglob("*"):
            if p.is_file() and (p.name in names or "sepolicy" in p.name):
                if p.stat().st_size>0:
                    candidates.append(p)
    print(f"BUILT_SEPOLICY_ARTIFACTS={len(candidates)}")
    if not candidates:
        fail("no built SELinux policy artifacts found")
    print("BUILT_SEPOLICY_ARTIFACTS=PASS")

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",required=True,type=Path)
    ap.add_argument("--report-dir",required=True,type=Path)
    ap.add_argument("--rom",type=Path)
    args=ap.parse_args()
    root=args.root.resolve()
    out=root/"out/target/product/TB8504"
    args.report_dir.mkdir(parents=True,exist_ok=True)
    if not out.is_dir():
        fail(f"product output missing: {out}")

    print("=== TB8504 ACTUAL BUILT OUTPUT AUDIT ===")
    audit_build_props(out)
    audit_vendor_copy(root,out)
    audit_actual_vendor_elf_dependencies(root,out)
    audit_runtime(out)
    audit_removed_outputs(out)
    audit_vintf(out)
    audit_checkvintf(root,out)
    audit_modules(root,out)
    audit_system_image(root,out)
    audit_policy(out)
    if args.rom is not None:
        audit_rom_zip(out,args.rom.resolve())

    print("ACTUAL_BUILT_OUTPUT_AUDIT=PASS")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
