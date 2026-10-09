#!/usr/bin/env python3
"""Regression checks for a shared kernel across boot/recovery outputs."""
from __future__ import annotations

import runpy
import struct
import tempfile
from pathlib import Path


def main() -> int:
    mod = runpy.run_path(str(Path(__file__).with_name("tb8504-autopilot.py")), run_name="pair_selftest")
    Auto = mod["Autopilot"]
    Result = mod["CommandResult"]

    def image(payload):
        data = bytearray(2048)
        data[:8] = b"ANDROID!"
        struct.pack_into("<I", data, 8, len(payload))
        struct.pack_into("<I", data, 36, 2048)
        return bytes(data) + payload

    class Fake(Auto):
        def say(self, message=""):
            pass
        def build_binding_current(self, kind):
            return False
        def android_shell(self, command, log_name):
            self.commands.append(command)
            for kind in ("boot", "recovery"):
                (self.product_out / f"{kind}.img").write_bytes(image(b"shared kernel"))
            return Result(0, "", command)
        def audit_image(self, kind, fresh=False):
            self.audits.append((kind, fresh))
            return fresh or self.reuse.get(kind, False)

    with tempfile.TemporaryDirectory() as tmp:
        auto = object.__new__(Fake)
        auto.product_out = Path(tmp)
        auto.max_attempts = 1
        auto.commands = []
        auto.audits = []
        auto.reuse = {"boot": False, "recovery": True}
        for kind in ("boot", "recovery"):
            (auto.product_out / f"{kind}.img").write_bytes(image(kind.encode()))
        auto.ensure_current_boot_recovery()
        assert auto.commands == ["mka 'bootimage' 'recoveryimage'"]
        assert auto.audits == [("boot", False), ("recovery", False), ("boot", True), ("recovery", True)]
        auto.commands.clear()
        auto.reuse = {"boot": True, "recovery": True}
        auto.ensure_current_boot_recovery()
        assert not auto.commands
        (auto.product_out / "recovery.img").write_bytes(image(b"different kernel"))
        try:
            auto.require_boot_recovery_kernel_coherence()
        except mod["StopAutopilot"]:
            pass
        else:
            raise AssertionError("different boot/recovery kernels accepted")
    print("TB8504_SHARED_KERNEL_PAIR_SELFTEST=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
