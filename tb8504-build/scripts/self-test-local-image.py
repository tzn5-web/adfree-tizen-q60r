#!/usr/bin/env python3
"""Regression checks for first-stage TB8504 images and legacy RSA modules."""
from __future__ import annotations

import contextlib
import io
import runpy
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    audit = runpy.run_path(str(Path(__file__).with_name("audit-local-image.py")), run_name="image_selftest")

    def rejected(fn, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                fn(*args)
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError(f"invalid evidence accepted by {fn.__name__}")

    def fdt(properties):
        strings = bytearray()
        offsets = {}
        body = bytearray()
        def word(value):
            body.extend(struct.pack(">I", value))
        def pad():
            body.extend(b"\0" * (-len(body) % 4))
        def node(name, props, children):
            word(1)
            body.extend(name.encode() + b"\0")
            pad()
            for name, value in props.items():
                if name not in offsets:
                    offsets[name] = len(strings)
                    strings.extend(name.encode() + b"\0")
                value = value.encode() + b"\0"
                word(3)
                word(len(value))
                word(offsets[name])
                body.extend(value)
                pad()
            for child in children:
                node(*child)
            word(2)
        node("", {}, [("firmware", {}, [("android", {"compatible": "android,firmware"}, [
            ("fstab", {"compatible": "android,fstab"}, [
                ("system", properties, []), ("vendor", {"status": "disabled"}, []),
            ]),
        ])])])
        word(9)
        struct_off = 56
        string_off = struct_off + len(body)
        total = string_off + len(strings)
        return struct.pack(">10I", 0xD00DFEED, total, struct_off, string_off, 40, 17, 16, 0, len(strings), len(body)) + b"\0" * 16 + body + strings

    properties = {
        "compatible": "android,system",
        "dev": "/dev/block/platform/soc/7824900.sdhci/by-name/system",
        "type": "ext4", "mnt_flags": "ro,barrier=1,discard",
        "fsmgr_flags": "wait", "status": "ok",
    }
    dtb = fdt(properties)
    assert audit["parse_fdt_properties"](dtb)["/firmware/android/fstab/system"]["type"] == b"ext4\0"
    for bad in (dtb[:20], dtb[:-1], b"BAD!" + dtb[4:]):
        rejected(audit["parse_fdt_properties"], bad)
    init = bytearray(64)
    init[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", init, 18, 183)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        built = root / "out/target/product/TB8504/ramdisk/init"
        built.parent.mkdir(parents=True)
        built.write_bytes(init)
        files = {"init": bytes(init)}
        with contextlib.redirect_stdout(io.StringIO()):
            audit["audit_ramdisk"]("boot", files, root, dtb)
        rejected(audit["audit_ramdisk"], "boot", files)
        rejected(audit["audit_ramdisk"], "boot", {"init": b"wrong"}, root, dtb)
        rejected(audit["audit_ramdisk"], "boot", {}, root, dtb)
        rejected(audit["audit_ramdisk"], "boot", {**files, "bad.rc": b"start audiod\n"}, root, dtb)
        for override in ({"status": "disabled"}, {"dev": "wrong"}, {"type": "f2fs"}, {"fsmgr_flags": "wait,recoveryonly"}):
            rejected(audit["audit_ramdisk"], "boot", files, root, fdt({**properties, **override}))
        rejected(audit["audit_ramdisk"], "boot", {**files, "init.target.rc": b""}, root, dtb)
        legacy_rc = "".join(f"service {name} /vendor/bin/example\n" for name in audit["REQUIRED_IMS"])
        with contextlib.redirect_stdout(io.StringIO()):
            audit["audit_ramdisk"]("boot", {**files, "init.target.rc": legacy_rc.encode()})
        rejected(audit["audit_ramdisk"], "recovery", files)
        with contextlib.redirect_stdout(io.StringIO()):
            audit["audit_ramdisk"]("recovery", {**files, "init.recovery.qcom.rc": b"", "etc/recovery.fstab": b"", "sbin/recovery": b"ELF"})

        openssl = shutil.which("openssl")
        assert openssl, "openssl required for module signature self-test"
        key = root / "key.pem"
        public = root / "public.pem"
        subprocess.run([openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)], check=True, capture_output=True)
        subprocess.run([openssl, "pkey", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)
        unsigned = bytes(init) + b"\0vermagic=3.18.140-test SMP aarch64\0"
        sig = subprocess.run([openssl, "dgst", "-sha512", "-sign", str(key)], input=unsigned, check=True, capture_output=True).stdout
        signer = b"Test signer"
        keyid = b"\xaa" * 20
        signed = unsigned + signer + keyid + struct.pack(">H", len(sig)) + sig + struct.pack(">BBBBB3xI", 1, 6, 1, len(signer), len(keyid), len(sig) + 2) + b"~Module signature appended~\n"
        row = audit["verify_legacy_module"](signed, public.read_bytes(), openssl)
        assert row["vermagic"].startswith("3.18.140-test ")
        rejected(audit["parse_legacy_module"], unsigned)
        rejected(audit["parse_legacy_module"], signed[:-1])
        corrupt = bytearray(signed)
        corrupt[40] ^= 1
        rejected(audit["verify_legacy_module"], bytes(corrupt), public.read_bytes(), openssl)
        corrupt = bytearray(signed)
        corrupt[-len(b"~Module signature appended~\n") - 11] = 4
        rejected(audit["parse_legacy_module"], bytes(corrupt))
    print("TB8504_LOCAL_IMAGE_REGRESSION_SELFTEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
