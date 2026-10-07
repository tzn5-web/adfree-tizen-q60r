#!/usr/bin/env python3
"""Fail-closed audit of the final LineageOS OTA ZIP for TB8504."""
from __future__ import annotations

import argparse
import hashlib
import lzma
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path

DEVICE = "TB8504"
OTA_DEVICE_ALIASES = {
    "TB-8504X", "TB-8504F", "tb-8504x", "tb-8504f", "tb_8504",
}
SDK = "36"

def fail(msg: str) -> None:
    print(f"FINAL_PACKAGE_AUDIT_FAIL={msg}")
    raise SystemExit(2)

def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def props_from_output(out: Path) -> dict[str,str]:
    vals={}
    for p in out.rglob("build.prop"):
        try:
            text=p.read_text("utf-8",errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            if "=" not in raw or raw.lstrip().startswith("#"):
                continue
            k,v=raw.split("=",1)
            vals.setdefault(k.strip(),v.strip())
    return vals

def parse_metadata(text: str) -> dict[str,str]:
    out={}
    for raw in text.splitlines():
        if "=" in raw:
            k,v=raw.split("=",1)
            out[k.strip()]=v.strip()
    return out


def parse_ranges(raw: str) -> list[tuple[int,int]]:
    try:
        nums=[int(x) for x in raw.strip().split(",") if x!=""]
    except ValueError as exc:
        fail(f"invalid transfer range set {raw!r}: {exc}")
    if not nums or nums[0] != len(nums)-1 or nums[0] % 2:
        fail(f"malformed transfer range set: {raw!r}")
    ranges=[]
    for start,end in zip(nums[1::2],nums[2::2]):
        if start < 0 or end <= start:
            fail(f"invalid transfer range pair: {start},{end}")
        ranges.append((start,end))
    return ranges


def extract_new_dat(
    z: zipfile.ZipFile,
    member: str,
    root: Path,
    work: Path,
) -> Path:
    compressed=work/Path(member).name
    with z.open(member,"r") as src, compressed.open("wb") as dst:
        shutil.copyfileobj(src,dst,1024*1024)

    if member.endswith(".br"):
        brotli_candidates=[
            root/"out/host/linux-x86/bin/brotli",
            Path(shutil.which("brotli") or ""),
        ]
        brotli=next(
            (p for p in brotli_candidates if str(p) and p.is_file()),None
        )
        if brotli is None:
            fail("brotli decoder missing for system.new.dat.br audit")
        raw=work/"system.new.dat"
        with raw.open("wb") as dst:
            proc=subprocess.run(
                [str(brotli),"-d","-c",str(compressed)],
                stdout=dst,stderr=subprocess.PIPE,check=False,
            )
        if proc.returncode!=0:
            fail(
                "brotli decode failed: "
                + proc.stderr.decode("utf-8","replace")[-2000:]
            )
        return raw

    if member.endswith(".xz"):
        raw=work/"system.new.dat"
        try:
            with lzma.open(compressed,"rb") as src, raw.open("wb") as dst:
                shutil.copyfileobj(src,dst,1024*1024)
        except lzma.LZMAError as exc:
            fail(f"xz decode failed: {exc}")
        return raw

    return compressed


def materialize_local_system(root: Path, out: Path, work: Path) -> Path:
    image=out/"system.img"
    if not image.is_file():
        fail("local system.img missing for block OTA audit")
    with image.open("rb") as fh:
        header=fh.read(4)
    if len(header)==4 and struct.unpack("<I",header)[0]==0xED26FF3A:
        simg2img=root/"out/host/linux-x86/bin/simg2img"
        if not simg2img.is_file():
            fail(f"simg2img missing for block OTA audit: {simg2img}")
        raw=work/"local-system.raw.img"
        proc=subprocess.run(
            [str(simg2img),str(image),str(raw)],
            text=True,capture_output=True,check=False,
        )
        if proc.returncode!=0 or not raw.is_file():
            fail(
                "simg2img failed during final package audit: "
                + (proc.stderr or proc.stdout)[-2000:]
            )
        return raw
    return image


def ext_block_size(raw_image: Path) -> int:
    with raw_image.open("rb") as fh:
        fh.seek(1024+24)
        data=fh.read(4)
        fh.seek(1024+56)
        magic=fh.read(2)
    if len(data)!=4 or magic!=b"\x53\xef":
        fail("local system image is not a valid ext filesystem")
    log=struct.unpack("<I",data)[0]
    if log>6:
        fail(f"implausible ext block-size exponent: {log}")
    return 1024 << log


def compare_stream_to_ranges(
    new_data: Path,
    local_raw: Path,
    commands: list[tuple[str,list[tuple[int,int]]]],
    block_size: int,
) -> tuple[int,int]:
    compared_blocks=0
    zero_blocks=0
    with new_data.open("rb") as newf, local_raw.open("rb") as local:
        for op,ranges in commands:
            if op=="new":
                for start,end in ranges:
                    remaining=(end-start)*block_size
                    local.seek(start*block_size)
                    while remaining:
                        n=min(1024*1024,remaining)
                        actual=newf.read(n)
                        expected=local.read(n)
                        if len(actual)!=n:
                            fail("system.new.dat truncated while consuming new ranges")
                        if actual!=expected:
                            fail(
                                "block OTA system payload differs from staged "
                                f"system.img at blocks {start}-{end}"
                            )
                        remaining-=n
                    compared_blocks += end-start
            elif op=="zero":
                for start,end in ranges:
                    remaining=(end-start)*block_size
                    local.seek(start*block_size)
                    while remaining:
                        n=min(1024*1024,remaining)
                        expected=local.read(n)
                        if len(expected)!=n or expected!=b"\x00"*n:
                            fail(
                                "block OTA zero range does not match staged "
                                f"system.img at blocks {start}-{end}"
                            )
                        remaining-=n
                    zero_blocks += end-start
            elif op=="erase":
                # Erase ranges are don't-care blocks and cannot be compared
                # byte-for-byte after a filesystem image is materialized.
                continue
            else:
                fail(f"unexpected source-dependent transfer command: {op}")

        if newf.read(1):
            fail("system.new.dat has unconsumed trailing data")
    return compared_blocks,zero_blocks


def audit_block_system_payload(
    z: zipfile.ZipFile,
    names: list[str],
    block_payloads: list[str],
    root: Path,
    out: Path,
) -> dict[str,str]:
    if len(block_payloads)!=1:
        fail(
            "expected exactly one block system payload, found "
            f"{len(block_payloads)}"
        )
    transfer=[n for n in names if Path(n).name=="system.transfer.list"]
    if len(transfer)!=1:
        fail(
            "block OTA system payload requires exactly one "
            f"system.transfer.list, found {len(transfer)}"
        )

    text=z.read(transfer[0]).decode("utf-8","strict")
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    if len(lines)<3:
        fail("system.transfer.list is truncated")
    try:
        version=int(lines[0])
        declared_blocks=int(lines[1])
    except ValueError as exc:
        fail(f"invalid transfer-list header: {exc}")
    if version<1 or version>4:
        fail(f"unsupported transfer-list version: {version}")
    command_start=4 if version>=2 else 2
    if len(lines)<=command_start:
        fail("system.transfer.list has no commands")

    commands=[]
    target_blocks=0
    source_dependent=[]
    for raw in lines[command_start:]:
        parts=raw.split()
        if not parts:
            continue
        op=parts[0]
        if op in {"new","zero","erase"}:
            if len(parts)!=2:
                fail(f"malformed {op} transfer command: {raw}")
            ranges=parse_ranges(parts[1])
            commands.append((op,ranges))
            if op in {"new","zero"}:
                target_blocks += sum(end-start for start,end in ranges)
        elif op in {"move","bsdiff","imgdiff","stash","free"}:
            source_dependent.append(raw)
        else:
            fail(f"unknown transfer command: {raw}")

    if source_dependent:
        fail(
            "final full ROM unexpectedly contains source-dependent block "
            f"transfers: {source_dependent[:20]}"
        )
    if target_blocks!=declared_blocks:
        fail(
            f"transfer-list block count mismatch: commands={target_blocks} "
            f"declared={declared_blocks}"
        )

    with tempfile.TemporaryDirectory(prefix="tb8504-ota-system-") as td:
        work=Path(td)
        new_data=extract_new_dat(z,block_payloads[0],root,work)
        local_raw=materialize_local_system(root,out,work)
        block_size=ext_block_size(local_raw)
        if block_size!=4096:
            fail(f"unexpected system ext block size for block OTA: {block_size}")
        compared,zeroed=compare_stream_to_ranges(
            new_data,local_raw,commands,block_size
        )

    if compared<=0:
        fail("block OTA audit compared zero new-data blocks")
    return {
        "TRANSFER_LIST_VERSION":str(version),
        "TRANSFER_DECLARED_BLOCKS":str(declared_blocks),
        "TRANSFER_NEW_BLOCKS_COMPARED":str(compared),
        "TRANSFER_ZERO_BLOCKS_COMPARED":str(zeroed),
        "BLOCK_OTA_SYSTEM_BINDING":"PASS",
    }


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",required=True,type=Path)
    ap.add_argument("--rom",required=True,type=Path)
    ap.add_argument("--min-mtime",required=True,type=float)
    ap.add_argument("--report-dir",required=True,type=Path)
    args=ap.parse_args()

    root=args.root.resolve()
    out=root/"out/target/product/TB8504"
    rom=args.rom.resolve()
    args.report_dir.mkdir(parents=True,exist_ok=True)

    if not rom.is_file() or rom.stat().st_size <= 0:
        fail(f"ROM ZIP missing/empty: {rom}")
    if rom.stat().st_mtime + 1 < args.min_mtime:
        fail(
            f"ROM ZIP predates current bacon stage: "
            f"mtime={rom.stat().st_mtime} min={args.min_mtime}"
        )

    try:
        z=zipfile.ZipFile(rom,"r")
    except Exception as exc:
        fail(f"cannot open ROM ZIP: {exc}")

    with z:
        bad=z.testzip()
        if bad:
            fail(f"ZIP CRC failure: {bad}")
        infos=z.infolist()
        names=[x.filename for x in infos]
        if len(names) != len(set(names)):
            fail("duplicate ZIP member names")
        for name in names:
            p=Path(name)
            if name.startswith("/") or ".." in p.parts:
                fail(f"unsafe ZIP member path: {name}")

        metadata_name="META-INF/com/android/metadata"
        if metadata_name not in names:
            fail("OTA metadata missing")
        meta=parse_metadata(z.read(metadata_name).decode("utf-8","replace"))

        ota_type=meta.get("ota-type","").upper()
        if ota_type == "AB":
            fail("unexpected A/B OTA for non-A/B TB8504")

        pre_devices={
            x.strip()
            for x in re.split(r"[|,]",meta.get("pre-device",""))
            if x.strip()
        }
        if not (pre_devices & OTA_DEVICE_ALIASES):
            fail(
                "OTA pre-device does not contain a supported TB8504 alias: "
                f"{sorted(pre_devices)}"
            )
        if DEVICE not in meta.get("post-build",""):
            fail(
                f"OTA post-build fingerprint does not identify {DEVICE}: "
                f"{meta.get('post-build','')}"
            )

        if meta.get("post-sdk-level") and meta["post-sdk-level"] != SDK:
            fail(f"OTA metadata SDK mismatch: {meta.get('post-sdk-level')}")

        props=props_from_output(out)
        fingerprint=props.get("ro.build.fingerprint","")
        incremental=props.get("ro.build.version.incremental","")
        if not fingerprint:
            fail("built output fingerprint missing")
        if meta.get("post-build") and meta["post-build"] != fingerprint:
            fail(
                f"OTA post-build fingerprint mismatch: "
                f"{meta['post-build']} != {fingerprint}"
            )
        if (
            incremental
            and meta.get("post-build-incremental")
            and meta["post-build-incremental"] != incremental
        ):
            fail(
                "OTA incremental mismatch: "
                f"{meta['post-build-incremental']} != {incremental}"
            )

        update_payloads={
            "payload.bin",
            "system.new.dat.br",
            "system.new.dat",
            "system.new.dat.xz",
            "system.img",
            "super.img",
        }
        payload_hits=sorted(n for n in names if Path(n).name in update_payloads)
        if not payload_hits:
            fail("no recognizable system/update payload in ROM ZIP")

        block_payloads=[
            n for n in payload_hits
            if Path(n).name in {
                "system.new.dat.br","system.new.dat","system.new.dat.xz"
            }
        ]
        if len(block_payloads)>1:
            fail(f"multiple block system payloads in OTA ZIP: {block_payloads}")

        boot_entries=[n for n in names if Path(n).name=="boot.img"]
        if len(boot_entries) != 1:
            fail(f"expected exactly one boot.img in OTA ZIP, found {len(boot_entries)}")
        local_boot=out/"boot.img"
        if not local_boot.is_file():
            fail("local boot.img missing while auditing OTA")
        zip_boot=z.read(boot_entries[0])
        local_boot_sha=sha_file(local_boot)
        zip_boot_sha=sha_bytes(zip_boot)
        if zip_boot_sha != local_boot_sha:
            fail(f"OTA boot.img mismatch {zip_boot_sha} != {local_boot_sha}")

        recovery_entries=[n for n in names if Path(n).name=="recovery.img"]
        local_recovery=out/"recovery.img"
        if recovery_entries and local_recovery.is_file():
            if len(recovery_entries) != 1:
                fail("multiple recovery.img entries in OTA ZIP")
            zip_recovery_sha=sha_bytes(z.read(recovery_entries[0]))
            local_recovery_sha=sha_file(local_recovery)
            if zip_recovery_sha != local_recovery_sha:
                fail(
                    f"OTA recovery.img mismatch "
                    f"{zip_recovery_sha} != {local_recovery_sha}"
                )

        raw_system=[n for n in names if Path(n).name=="system.img"]
        local_system=out/"system.img"
        if raw_system and local_system.is_file():
            if len(raw_system) != 1:
                fail("multiple system.img entries in OTA ZIP")
            if sha_bytes(z.read(raw_system[0])) != sha_file(local_system):
                fail("OTA raw system.img does not match local system.img")

        block_audit={}
        if block_payloads:
            block_audit=audit_block_system_payload(
                z,names,block_payloads,root,out
            )

        report={
            "ROM":str(rom),
            "ROM_SIZE":str(rom.stat().st_size),
            "ROM_SHA256":sha_file(rom),
            "ZIP_ENTRIES":str(len(names)),
            "OTA_TYPE":ota_type or "UNSPECIFIED_NON_AB",
            "POST_BUILD":meta.get("post-build",""),
            "POST_SDK_LEVEL":meta.get("post-sdk-level",""),
            "PRE_DEVICE_ALIASES":",".join(sorted(pre_devices)),
            "PAYLOAD_MEMBERS":",".join(payload_hits),
            "BLOCK_PAYLOAD_MEMBERS":",".join(block_payloads),
            "OTA_BOOT_SHA256":zip_boot_sha,
            "LOCAL_BOOT_SHA256":local_boot_sha,
            "FINAL_PACKAGE_AUDIT":"PASS",
        }
        report.update(block_audit)
        text="\n".join(f"{k}={v}" for k,v in report.items())+"\n"
        (args.report_dir/"final-package.txt").write_text(text,encoding="utf-8")
        print(text,end="")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
