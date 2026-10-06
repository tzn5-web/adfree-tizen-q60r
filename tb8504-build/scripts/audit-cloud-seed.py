#!/usr/bin/env python3
"""Audit a one-time TB8504 cloud seed before any GitHub ingestion."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import hashlib
import io
import re
import struct
import sys
import tarfile
import zipfile

BOOT_LIMIT = 67_108_864
RECOVERY_LIMIT = 67_108_864
MAX_MEMBER = 128 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
MAX_DEVICE_TREE_UNCOMPRESSED = 128 * 1024 * 1024

REQUIRED = {
    "meta/EXPORT.txt",
    "meta/REPOS.txt",
    "meta/SEEDS.txt",
    "meta/EXPORTED_REPOS.txt",
    "SHA256SUMS",
    "device_lenovo_TB8504.tar.gz",
    "seeds/boot_seed.img",
}

EXPECTED_EXPORT = {
    "NO_BUILD=YES",
    "NO_FLASH=YES",
    "EXPECTED_BOOT_SIZE=67108864",
    "EXPECTED_RECOVERY_SIZE=67108864",
    "EXPECTED_SYSTEM_SIZE=4080218112",
    "EXPECTED_BOOT_HEADER_VERSION=0",
    "EXPECTED_BOOT_PAGESIZE=2048",
    "EXPECTED_KERNEL_ADDR=0x80008000",
    "EXPECTED_RAMDISK_ADDR=0x81000000",
    "EXPECTED_TAGS_ADDR=0x80000100",
}

FORBIDDEN_NAME_PATTERNS = (
    re.compile(r"(^|/)(id_rsa|id_ed25519)$", re.I),
    re.compile(r"\.(pem|key|p12|pfx|jks|keystore)$", re.I),
    re.compile(r"(^|/)\.env$", re.I),
    re.compile(r"(credential|secret|token)", re.I),
)

SECRET_TEXT_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"://[^/\s:@]+:[^/\s@]+@"),
)

KERNEL_KEY = "kernel__lenovo__msm8917"


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

        if len(digest) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            fail(f"invalid SHA256 digest for {path}")

        if path in result:
            fail(f"duplicate SHA256SUMS entry: {path}")

        result[path] = digest.lower()

    return result


def audit_android_boot_blob(label: str, data: bytes, limit: int) -> None:
    print(f"{label}_SIZE={len(data)}")
    print(f"{label}_SHA256={sha256_bytes(data)}")

    if len(data) <= 4096:
        fail(f"{label} implausibly small")
    if len(data) > limit:
        fail(f"{label} exceeds partition limit")
    if data[:8] != b"ANDROID!":
        fail(f"{label} bad Android boot magic")
    if len(data) < 8 + 9 * 4 + 4:
        fail(f"{label} truncated boot header")

    fields = struct.unpack_from("<9I", data, 8)
    (
        kernel_size,
        kernel_addr,
        ramdisk_size,
        ramdisk_addr,
        second_size,
        second_addr,
        tags_addr,
        page_size,
        header_version,
    ) = fields

    print(f"{label}_HEADER_VERSION={header_version}")
    print(f"{label}_PAGE_SIZE={page_size}")
    print(f"{label}_KERNEL_SIZE={kernel_size}")
    print(f"{label}_KERNEL_ADDR=0x{kernel_addr:08x}")
    print(f"{label}_RAMDISK_SIZE={ramdisk_size}")
    print(f"{label}_RAMDISK_ADDR=0x{ramdisk_addr:08x}")
    print(f"{label}_SECOND_SIZE={second_size}")
    print(f"{label}_SECOND_ADDR=0x{second_addr:08x}")
    print(f"{label}_TAGS_ADDR=0x{tags_addr:08x}")

    expected = {
        "header_version": (header_version, 0),
        "page_size": (page_size, 2048),
        "kernel_addr": (kernel_addr, 0x80008000),
        "ramdisk_addr": (ramdisk_addr, 0x81000000),
        "tags_addr": (tags_addr, 0x80000100),
    }

    for key, (actual, wanted) in expected.items():
        if actual != wanted:
            fail(f"{label} {key} expected={wanted:#x} actual={actual:#x}")

    if kernel_size == 0 or ramdisk_size == 0:
        fail(f"{label} kernel or ramdisk is empty")

    if second_size != 0:
        fail(f"{label} unexpected second-stage payload size={second_size}")

    print(f"{label}_STRUCTURE=PASS")


def audit_device_tar(data: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        if not members:
            fail("device tree tar is empty")

        total = 0
        for member in members:
            name = member.name
            if not safe_name(name):
                fail(f"unsafe tar path: {name}")
            if not (name == "TB8504" or name.startswith("TB8504/")):
                fail(f"unexpected device tar root: {name}")
            if member.isdev():
                fail(f"device node forbidden in tar: {name}")

            total += max(0, member.size)
            if total > MAX_DEVICE_TREE_UNCOMPRESSED:
                fail("device tree tar expands beyond safety limit")

            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    fail(f"unsafe link in tar: {name} -> {member.linkname}")

            for rx in FORBIDDEN_NAME_PATTERNS:
                if rx.search(name):
                    fail(f"sensitive filename in device tree tar: {name}")

        print(f"DEVICE_TREE_TAR_MEMBERS={len(members)}")
        print(f"DEVICE_TREE_TAR_UNCOMPRESSED={total}")


def parse_repo_heads(text: str) -> dict[str, str]:
    heads: dict[str, str] = {}
    current = None

    for line in text.splitlines():
        if line.startswith("REPO="):
            current = line.split("=", 1)[1].strip()
        elif current and line.startswith("HEAD="):
            heads[current] = line.split("=", 1)[1].strip()

    return heads


def audit_small_text_for_secrets(name: str, data: bytes) -> None:
    if len(data) > 2 * 1024 * 1024:
        return

    for rx in SECRET_TEXT_PATTERNS:
        if rx.search(data):
            fail(f"possible credential leaked in text member: {name}")


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
        file_infos = [i for i in infos if not i.is_dir()]
        file_names = [i.filename for i in file_infos]

        if len(file_names) != len(set(file_names)):
            fail("duplicate file names in ZIP")

        names = set(file_names)
        total_uncompressed = 0

        for info in infos:
            if not safe_name(info.filename):
                fail(f"unsafe zip path: {info.filename}")

            if info.file_size > MAX_MEMBER:
                fail(f"member too large: {info.filename} ({info.file_size})")

            total_uncompressed += info.file_size
            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
                fail("ZIP expands beyond total safety limit")

            mode = (info.external_attr >> 16) & 0xFFFF
            ftype = mode & 0o170000
            if ftype in {0o020000, 0o060000}:
                fail(f"device node forbidden in zip: {info.filename}")
            if ftype == 0o120000:
                fail(f"zip symlink forbidden: {info.filename}")

            for rx in FORBIDDEN_NAME_PATTERNS:
                if rx.search(info.filename):
                    fail(f"sensitive filename in seed ZIP: {info.filename}")

        print(f"SEED_UNCOMPRESSED_TOTAL={total_uncompressed}")

        missing = sorted(REQUIRED - names)
        if missing:
            fail("required members missing: " + ",".join(missing))

        export = z.read("meta/EXPORT.txt").decode("utf-8", "replace")
        export_lines = set(export.splitlines())
        absent = sorted(EXPECTED_EXPORT - export_lines)
        if absent:
            fail("EXPORT metadata mismatch: " + ",".join(absent))

        seeds_meta = z.read("meta/SEEDS.txt").decode("utf-8", "replace")
        if "BOOT_SEED=present" not in seeds_meta.splitlines():
            fail("SEEDS metadata does not mark boot seed present")

        sums = parse_sums(z.read("SHA256SUMS").decode("utf-8", "replace"))
        expected_hashed_names = names - {"SHA256SUMS"}

        if set(sums) != expected_hashed_names:
            missing_hashes = sorted(expected_hashed_names - set(sums))
            extra_hashes = sorted(set(sums) - expected_hashed_names)
            if missing_hashes:
                print("SEED_HASH_MISSING=" + ",".join(missing_hashes))
            if extra_hashes:
                print("SEED_HASH_EXTRA=" + ",".join(extra_hashes))
            fail("SHA256SUMS coverage mismatch")

        checked = 0
        for name, expected in sums.items():
            data = z.read(name)
            actual = sha256_bytes(data)
            if actual != expected:
                fail(f"hash mismatch: {name}")
            checked += 1

            if (
                name.startswith("meta/")
                or name.startswith("patches/")
                or name.startswith("commits/")
            ):
                audit_small_text_for_secrets(name, data)

        print(f"SEED_HASHES_VERIFIED={checked}")

        skipped_sensitive = z.read(
            "meta/SKIPPED_SENSITIVE_FILES.txt"
        ).decode("utf-8", "replace") if "meta/SKIPPED_SENSITIVE_FILES.txt" in names else ""

        if skipped_sensitive.strip():
            print("SEED_SENSITIVE_FILES_SKIPPED=YES")
            for line in skipped_sensitive.splitlines():
                print(f"SKIPPED_SENSITIVE_FILE={line}")
        else:
            print("SEED_SENSITIVE_FILES_SKIPPED=NO")

        audit_device_tar(z.read("device_lenovo_TB8504.tar.gz"))
        audit_android_boot_blob(
            "BOOT_SEED", z.read("seeds/boot_seed.img"), BOOT_LIMIT
        )

        if "seeds/recovery_seed.img" in names:
            audit_android_boot_blob(
                "RECOVERY_SEED",
                z.read("seeds/recovery_seed.img"),
                RECOVERY_LIMIT,
            )
        else:
            print("RECOVERY_SEED=MISSING")

        repo_text = z.read("meta/REPOS.txt").decode("utf-8", "replace")
        heads = parse_repo_heads(repo_text)
        kernel_head = heads.get("kernel/lenovo/msm8917", "")
        if not re.fullmatch(r"[0-9a-f]{40}", kernel_head):
            fail("kernel/lenovo/msm8917 HEAD missing or invalid in REPOS metadata")

        print(f"LOCAL_KERNEL_HEAD={kernel_head}")

        kernel_patch = f"patches/{KERNEL_KEY}.patch"
        kernel_mbox = f"commits/{KERNEL_KEY}.mbox"
        kernel_status = f"meta/{KERNEL_KEY}.status.txt"
        kernel_untracked_prefix = "untracked/kernel/lenovo/msm8917/"

        patch_bytes = len(z.read(kernel_patch)) if kernel_patch in names else -1
        mbox_bytes = len(z.read(kernel_mbox)) if kernel_mbox in names else -1
        status_text = (
            z.read(kernel_status).decode("utf-8", "replace")
            if kernel_status in names
            else ""
        )
        untracked_kernel = sorted(
            n for n in names if n.startswith(kernel_untracked_prefix)
        )

        print(f"LOCAL_KERNEL_PATCH_BYTES={patch_bytes}")
        print(f"LOCAL_KERNEL_MBOX_BYTES={mbox_bytes}")
        print(f"LOCAL_KERNEL_STATUS_NONEMPTY={1 if status_text.strip() else 0}")
        print(f"LOCAL_KERNEL_UNTRACKED_COUNT={len(untracked_kernel)}")

        if patch_bytes < 0 or mbox_bytes < 0:
            fail("kernel patch/commit state was not exported")

        exported_repos = z.read(
            "meta/EXPORTED_REPOS.txt"
        ).decode("utf-8", "replace").splitlines()

        if "hardware/qcom-caf/msm8996/gps" not in exported_repos:
            fail("GPS/LOC repository missing from exported repo set")

        print(f"EXPORTED_REPO_COUNT={len([x for x in exported_repos if x.strip()])}")

        patches = sorted(
            n for n in names if n.startswith("patches/") and n.endswith(".patch")
        )
        statuses = sorted(
            n for n in names if n.startswith("meta/") and n.endswith(".status.txt")
        )
        print(f"PATCH_FILES={len(patches)}")
        print(f"STATUS_FILES={len(statuses)}")
        print(f"ZIP_FILES={len(names)}")

    print("TB8504_CLOUD_SEED_AUDIT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
