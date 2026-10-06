#!/usr/bin/env python3
"""Audit a one-time TB8504 cloud seed before any GitHub ingestion."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import hashlib
import io
import sys
import tarfile
import zipfile

BOOT_LIMIT = 67_108_864
RECOVERY_LIMIT = 67_108_864
MAX_MEMBER = 128 * 1024 * 1024

REQUIRED = {
    "meta/EXPORT.txt",
    "meta/REPOS.txt",
    "SHA256SUMS",
    "device_lenovo_TB8504.tar.gz",
}


def fail(msg: str) -> None:
    print(f"SEED_AUDIT_FAIL={msg}")
    raise SystemExit(1)


def safe_name(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            digest, path = raw.split(None, 1)
        except ValueError:
            fail(f"malformed SHA256SUMS line: {raw!r}")
        path = path.lstrip("*")
        if path.startswith("./"):
            path = path[2:]
        if len(digest) != 64:
            fail(f"invalid SHA256 digest for {path}")
        result[path] = digest.lower()
    return result


def audit_boot_blob(label: str, data: bytes, limit: int) -> None:
    print(f"{label}_SIZE={len(data)}")
    print(f"{label}_SHA256={sha256_bytes(data)}")
    if len(data) <= 4096:
        fail(f"{label} implausibly small")
    if len(data) > limit:
        fail(f"{label} exceeds partition limit")
    if data[:8] != b"ANDROID!":
        fail(f"{label} bad Android boot magic")
    print(f"{label}_MAGIC=ANDROID!")


def audit_device_tar(data: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        if not members:
            fail("device tree tar is empty")

        for member in members:
            name = member.name
            if not safe_name(name):
                fail(f"unsafe tar path: {name}")
            if not (name == "TB8504" or name.startswith("TB8504/")):
                fail(f"unexpected device tar root: {name}")
            if member.isdev():
                fail(f"device node forbidden in tar: {name}")
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    fail(f"unsafe link in tar: {name} -> {member.linkname}")

        print(f"DEVICE_TREE_TAR_MEMBERS={len(members)}")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} TB8504_CLOUD_SEED_*.zip")
        return 2

    path = Path(sys.argv[1])
    if not path.is_file():
        fail(f"seed zip missing: {path}")

    print(f"SEED_ZIP={path}")
    print(f"SEED_ZIP_SIZE={path.stat().st_size}")
    print(f"SEED_ZIP_SHA256={hashlib.sha256(path.read_bytes()).hexdigest()}")

    with zipfile.ZipFile(path, "r") as z:
        bad = z.testzip()
        if bad:
            fail(f"corrupt zip member: {bad}")

        infos = z.infolist()
        names = {i.filename for i in infos if not i.is_dir()}

        for info in infos:
            if not safe_name(info.filename):
                fail(f"unsafe zip path: {info.filename}")
            if info.file_size > MAX_MEMBER:
                fail(f"member too large: {info.filename} ({info.file_size})")

            # Reject Unix device nodes. Symlinks are not expected from the
            # exporter and are intentionally rejected at ZIP level.
            mode = (info.external_attr >> 16) & 0xFFFF
            ftype = mode & 0o170000
            if ftype in {0o020000, 0o060000}:
                fail(f"device node forbidden in zip: {info.filename}")
            if ftype == 0o120000:
                fail(f"zip symlink forbidden: {info.filename}")

        missing = sorted(REQUIRED - names)
        if missing:
            fail("required members missing: " + ",".join(missing))

        export = z.read("meta/EXPORT.txt").decode("utf-8", "replace")
        required_meta = {
            "NO_BUILD=YES",
            "NO_FLASH=YES",
            "EXPECTED_BOOT_SIZE=67108864",
            "EXPECTED_RECOVERY_SIZE=67108864",
            "EXPECTED_SYSTEM_SIZE=4080218112",
        }
        export_lines = set(export.splitlines())
        absent = sorted(required_meta - export_lines)
        if absent:
            fail("EXPORT metadata mismatch: " + ",".join(absent))

        sums = parse_sums(z.read("SHA256SUMS").decode("utf-8", "replace"))
        checked = 0

        for name, expected in sums.items():
            if name == "SHA256SUMS":
                continue
            if name not in names:
                fail(f"SHA256SUMS references missing member: {name}")
            actual = sha256_bytes(z.read(name))
            if actual != expected:
                fail(f"hash mismatch: {name}")
            checked += 1

        print(f"SEED_HASHES_VERIFIED={checked}")

        audit_device_tar(z.read("device_lenovo_TB8504.tar.gz"))

        if "seeds/boot_seed.img" in names:
            audit_boot_blob(
                "BOOT_SEED",
                z.read("seeds/boot_seed.img"),
                BOOT_LIMIT,
            )
        else:
            print("BOOT_SEED=MISSING")

        if "seeds/recovery_seed.img" in names:
            audit_boot_blob(
                "RECOVERY_SEED",
                z.read("seeds/recovery_seed.img"),
                RECOVERY_LIMIT,
            )
        else:
            print("RECOVERY_SEED=MISSING")

        patches = sorted(n for n in names if n.startswith("patches/") and n.endswith(".patch"))
        statuses = sorted(n for n in names if n.startswith("meta/") and n.endswith(".status.txt"))
        print(f"PATCH_FILES={len(patches)}")
        print(f"STATUS_FILES={len(statuses)}")
        print(f"ZIP_FILES={len(names)}")

    print("TB8504_CLOUD_SEED_AUDIT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
