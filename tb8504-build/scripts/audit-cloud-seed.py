#!/usr/bin/env python3
"""Audit a one-time TB8504 cloud seed before any GitHub ingestion."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import hashlib
import io
import re
import struct
import subprocess
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
    "meta/INSTALLED_MODULES.txt",
    "meta/MODULE_SIGNING.txt",
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
MODULE_SIG_MAGIC = b"~Module signature appended~\n"
MODULE_SIG_INFO_SIZE = 12


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



def parse_module_signature(data: bytes) -> dict[str, object]:
    if not data.endswith(MODULE_SIG_MAGIC):
        raise ValueError("module signature marker missing")

    info_end = len(data) - len(MODULE_SIG_MAGIC)
    info_start = info_end - MODULE_SIG_INFO_SIZE
    if info_start < 0:
        raise ValueError("module signature footer truncated")

    try:
        algo, digest, ident, signer_len, keyid_len, sig_len = struct.unpack(
            ">BBBBB3xI", data[info_start:info_end]
        )
    except struct.error as exc:
        raise ValueError(f"bad module signature footer: {exc}") from exc

    unsigned_end = info_start - signer_len - keyid_len - sig_len
    if unsigned_end <= 0:
        raise ValueError("invalid signed-module tail lengths")

    signer_start = unsigned_end
    keyid_start = signer_start + signer_len
    sig_start = keyid_start + keyid_len

    signature_blob = data[sig_start:info_start]
    if len(signature_blob) != sig_len or sig_len < 2:
        raise ValueError("invalid module signature blob length")

    declared_rsa_len = struct.unpack(">H", signature_blob[:2])[0]
    if declared_rsa_len != sig_len - 2:
        raise ValueError(
            f"signature length mismatch footer={sig_len} rsa={declared_rsa_len}"
        )

    return {
        "keyid": data[keyid_start:sig_start],
        "signer": data[signer_start:keyid_start],
        "digest_id": digest,
        "algo_id": algo,
        "ident_id": ident,
    }


def cert_subject_key_id_bytes(cert: bytes) -> str:
    try:
        proc = subprocess.run(
            [
                "openssl",
                "x509",
                "-inform",
                "DER",
                "-noout",
                "-ext",
                "subjectKeyIdentifier",
            ],
            input=cert,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot read local signing certificate SKI: {exc}") from exc

    output = proc.stdout.decode("utf-8", "replace")
    hex_pairs = re.findall(r"(?i)\b[0-9a-f]{2}\b", output)
    if not hex_pairs:
        raise ValueError("subjectKeyIdentifier not found in local signing certificate")

    return "".join(x.lower() for x in hex_pairs)


def parse_module_manifest(text: str) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}

    for raw in text.splitlines():
        if not raw.strip():
            continue

        parts = raw.split("\t")
        if len(parts) != 3:
            fail(f"malformed installed-module manifest line: {raw!r}")

        rel, size_s, digest = parts
        if not safe_name(rel):
            fail(f"unsafe installed-module path: {rel}")
        if not size_s.isdigit():
            fail(f"bad installed-module size: {rel}")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            fail(f"bad installed-module hash: {rel}")

        result[rel] = (int(size_s), digest.lower())

    return result

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

        module_manifest = parse_module_manifest(
            z.read("meta/INSTALLED_MODULES.txt").decode("utf-8", "replace")
        )
        module_members = sorted(
            n for n in names if n.startswith("installed-modules/") and n.endswith(".ko")
        )
        manifest_members = {
            "installed-modules/" + rel for rel in module_manifest
        }

        if set(module_members) != manifest_members:
            missing_mods = sorted(manifest_members - set(module_members))
            extra_mods = sorted(set(module_members) - manifest_members)
            if missing_mods:
                print("INSTALLED_MODULE_FILES_MISSING=" + ",".join(missing_mods))
            if extra_mods:
                print("INSTALLED_MODULE_FILES_EXTRA=" + ",".join(extra_mods))
            fail("installed-module manifest coverage mismatch")

        module_meta = z.read("meta/MODULE_SIGNING.txt").decode("utf-8", "replace")
        module_count_line = next(
            (x for x in module_meta.splitlines() if x.startswith("INSTALLED_MODULE_COUNT=")),
            "",
        )
        declared_count = (
            int(module_count_line.split("=", 1)[1])
            if module_count_line.split("=", 1)[-1].isdigit()
            else -1
        )

        if declared_count != len(module_manifest):
            fail(
                f"installed-module count mismatch declared={declared_count} "
                f"manifest={len(module_manifest)}"
            )

        print(f"INSTALLED_MODULE_COUNT={len(module_manifest)}")

        local_cert_name = "meta/local_module_signing.x509"
        local_cert_sha = "NONE"
        local_cert_ski = "NONE"
        signature_mismatches = 0

        if module_manifest:
            if local_cert_name not in names:
                print("LOCAL_MODULE_SIGNING_CERT=MISSING")
                signature_mismatches += len(module_manifest)
            else:
                cert = z.read(local_cert_name)
                local_cert_sha = sha256_bytes(cert)
                try:
                    local_cert_ski = cert_subject_key_id_bytes(cert)
                except ValueError as exc:
                    print(f"LOCAL_MODULE_SIGNING_CERT_ERROR={exc}")
                    signature_mismatches += len(module_manifest)
                    local_cert_ski = "INVALID"

                print(f"LOCAL_MODULE_SIGNING_CERT_SHA256={local_cert_sha}")
                print(f"LOCAL_MODULE_SIGNING_CERT_SKI={local_cert_ski}")

            for rel, (wanted_size, wanted_sha) in sorted(module_manifest.items()):
                name = "installed-modules/" + rel
                data = z.read(name)
                actual_sha = sha256_bytes(data)

                if len(data) != wanted_size or actual_sha != wanted_sha:
                    fail(f"installed-module content mismatch: {rel}")

                try:
                    sig = parse_module_signature(data)
                    keyid = bytes(sig["keyid"]).hex()
                    signer = bytes(sig["signer"]).decode("utf-8", "replace")
                    print(f"INSTALLED_MODULE_SIGNER={rel}:{signer}")
                    print(f"INSTALLED_MODULE_KEYID={rel}:{keyid}")

                    if local_cert_ski not in {"NONE", "INVALID"} and (
                        keyid.lower() != local_cert_ski.lower()
                    ):
                        signature_mismatches += 1
                        print(
                            f"INSTALLED_MODULE_KEY_MISMATCH={rel}:"
                            f"module={keyid}:cert={local_cert_ski}"
                        )
                except ValueError as exc:
                    signature_mismatches += 1
                    print(f"INSTALLED_MODULE_SIGNATURE_ERROR={rel}:{exc}")
        else:
            print("LOCAL_MODULE_SIGNING_CERT_SHA256=NONE")
            print("LOCAL_MODULE_SIGNING_CERT_SKI=NONE")

        print(f"INSTALLED_MODULE_SIGNATURE_MISMATCHES={signature_mismatches}")
        if not module_manifest:
            print("INSTALLED_MODULE_SIGNING_COHERENCE=NO_INSTALLED_MODULES")
        elif signature_mismatches == 0:
            print("INSTALLED_MODULE_SIGNING_COHERENCE=PASS")
        else:
            print("INSTALLED_MODULE_SIGNING_COHERENCE=FAIL")

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
