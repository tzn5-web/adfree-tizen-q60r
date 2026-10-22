"""No network calls or system mutations: offline security and regression tests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from device_control import core

def policy(roots, **caps):
    return core.Policy({"schema":1,"enabled":True,"roots":roots,
                        "capabilities":caps, "android_serial":"emulator-5554",
                        "adb_path":"adb"})

class DeviceControlTests(unittest.TestCase):
    def test_disabled_when_no_policy(self):
        with patch.object(core, "policy_path", return_value=Path("/definitely-absent-policy.json")):
            with self.assertRaises(core.AccessDenied):
                core.Policy()

    def test_unknown_capability_denied(self):
        with self.assertRaises(core.AccessDenied):
            policy(["Desktop"], shell=True).require("all_access")

    def test_disabled_capability_denied(self):
        with self.assertRaises(core.AccessDenied):
            policy(["Desktop"], files_read=False).require("files_read")

    def test_alias_and_escape(self):
        with tempfile.TemporaryDirectory() as root:
            desktop = Path(root, "Desktop")
            desktop.mkdir()
            with patch.object(core, "known_folder", return_value=desktop):
                self.assertEqual(core.resolve_path("Desktop/a.txt"), desktop/"a.txt")
                self.assertFalse(core.within(core.resolve_path("Desktop/../other.txt"), [desktop]))

    def test_write_read_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            p = policy([root], files_read=True, files_write=True)
            path = Path(root, "note.txt")
            with patch.object(core, "Policy", return_value=p):
                result = core.write_text(str(path), "hello")
                self.assertEqual(result["bytes"], 5)
                self.assertEqual(core.read_text(str(path)), "hello")
                with self.assertRaises(FileExistsError):
                    core.write_text(str(path), "changed")
                core.write_text(str(path), "changed", overwrite=True)
                self.assertEqual(core.read_text(str(path)), "changed")

    def test_write_outside_roots_denied(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            with patch.object(core, "Policy", return_value=policy([root], files_write=True)):
                with self.assertRaises(core.AccessDenied):
                    core.write_text(str(Path(outside, "x.txt")), "data")

    def test_write_limits(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(core, "Policy", return_value=policy([root], files_write=True)):
                with self.assertRaises(ValueError):
                    core.write_text(str(Path(root, "huge.txt")), "x" * (core.MAX_WRITE + 1))

    def test_shell_refuses_nonwindows(self):
        with patch.object(core, "Policy", return_value=policy(["Desktop"], shell=True)), \
             patch.object(core.platform, "system", return_value="Linux"):
            with self.assertRaises(OSError):
                core.shell_command("echo test")

    def test_shell_timeout_validation(self):
        with self.assertRaises(ValueError):
            core.run_process(["echo", "test"], timeout=121)

    def test_no_shell_without_grant(self):
        with patch.object(core, "Policy", return_value=policy(["Desktop"], shell=False)):
            with self.assertRaises(core.AccessDenied):
                core.shell_command("echo test")

    def test_android_not_paired(self):
        p = policy(["Desktop"], android_write=True)
        p.data["android_serial"] = ""
        with patch.object(core, "Policy", return_value=p):
            with self.assertRaises(core.AccessDenied):
                core.adb_prefix()

    def test_android_blocks_bad_input(self):
        with patch.object(core, "Policy", return_value=policy(["Desktop"], android_write=True)):
            with self.assertRaises(ValueError):
                core.android_action("text", "a; rm -rf /")

    def test_android_png_signature(self):
        with patch.object(core, "Policy", return_value=policy(["Desktop"], android_read=True)), \
             patch.object(core, "run_process", return_value=b"not png"):
            with self.assertRaises(RuntimeError):
                core.android_screenshot_png()

    def test_status_no_policy(self):
        with patch.object(core, "policy_path", return_value=Path("/definitely-absent-policy.json")):
            self.assertFalse(core.status()["policy_enabled"])

    def test_bad_policy_schema(self):
        with self.assertRaises(core.AccessDenied):
            core.Policy({"enabled": True})

    def test_screenshot_denied_without_permission(self):
        with patch.object(core, "Policy", return_value=policy(["Desktop"], screen=False)):
            with self.assertRaises(core.AccessDenied):
                core.screenshot_png()

if __name__ == "__main__":
    unittest.main()
