#!/usr/bin/env python3
"""Fail-closed audit of the final LineageOS OTA ZIP for TB8504."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import zipfile
from pathlib import Path

DEVICE = "TB8504"
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

        identity="\n".join([
            meta.get("pre-device",""),
            meta.get("post-build",""),
        ])
        if DEVICE not in identity:
            fail(f"OTA metadata does not identify {DEVICE}")

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
            "system.img",
            "super.img",
        }
        payload_hits=sorted(n for n in names if Path(n).name in update_payloads)
        if not payload_hits:
            fail("no recognizable system/update payload in ROM ZIP")

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

        report={
            "ROM":str(rom),
            "ROM_SIZE":str(rom.stat().st_size),
            "ROM_SHA256":sha_file(rom),
            "ZIP_ENTRIES":str(len(names)),
            "OTA_TYPE":ota_type or "UNSPECIFIED_NON_AB",
            "POST_BUILD":meta.get("post-build",""),
            "POST_SDK_LEVEL":meta.get("post-sdk-level",""),
            "PAYLOAD_MEMBERS":",".join(payload_hits),
            "OTA_BOOT_SHA256":zip_boot_sha,
            "LOCAL_BOOT_SHA256":local_boot_sha,
            "FINAL_PACKAGE_AUDIT":"PASS",
        }
        text="\n".join(f"{k}={v}" for k,v in report.items())+"\n"
        (args.report_dir/"final-package.txt").write_text(text,encoding="utf-8")
        print(text,end="")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
