import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from device_control import azure_store, core, jobs

class FakeAzure:
    def __init__(self):
        self.items = {}
    def upload_blob(self, path, data, overwrite=False):
        if not overwrite and path in self.items:
            raise FileExistsError(path)
        self.items[path] = data
    def list_blobs(self, name_starts_with=""):
        return [SimpleNamespace(name=k, size=len(v)) for k,v in self.items.items()
                if k.startswith(name_starts_with)]
    def download_blob(self, path):
        data = self.items[path]
        return SimpleNamespace(properties=SimpleNamespace(size=len(data)),
                               readall=lambda: data)

class CloudTests(unittest.TestCase):
    def test_path_traversal_rejected(self):
        for item in ("../secret", "/abs", "a/../../b", "a//b", "a\\..\\b", "a/" + "x"*100):
            with self.subTest(item=item), self.assertRaises(ValueError):
                azure_store.artifact_path(item)

    def test_bad_project_id_rejected(self):
        for p in ("", "../x", "X", "a", "space here", ".hidden"):
            with self.subTest(p=p), self.assertRaises(ValueError):
                azure_store.project_name(p)

    def test_create_list_write_and_read(self):
        cloud = FakeAzure()
        with patch.object(azure_store, "client", return_value=cloud):
            created = azure_store.create_project("windows-audit", "device tests")
            self.assertEqual(created["project_id"], "windows-audit")
            self.assertEqual(azure_store.list_projects(), ["windows-audit"])
            value = azure_store.save_text("windows-audit", "reports/status.txt", "ready")
            self.assertEqual(value["bytes"], 5)
            self.assertEqual(azure_store.read_text("windows-audit", "reports/status.txt"), "ready")
            self.assertEqual(azure_store.list_artifacts("windows-audit")[0]["name"],
                             "reports/status.txt")
            with self.assertRaises(FileExistsError):
                azure_store.save_text("windows-audit", "reports/status.txt", "again")

    def test_capability_denied_when_disabled(self):
        policy = core.Policy({"schema":1, "enabled":True, "roots":["Desktop"],
                              "capabilities":{"azure_read": False, "azure_write":False}})
        with self.assertRaises(core.AccessDenied):
            policy.require("azure_write")
        with self.assertRaises(core.AccessDenied):
            policy.require("azure_read")

    def test_job_disallowed_on_linux(self):
        policy = core.Policy({"schema":1, "enabled":True, "roots":["Desktop"],
                              "capabilities":{"shell":True}})
        with patch.object(jobs, "Policy", return_value=policy), \
             patch.object(jobs.platform, "system", return_value="Linux"):
            with self.assertRaises(OSError):
                jobs.start_job("echo hello")

    def test_invalid_job_command(self):
        with self.assertRaises(ValueError):
            jobs.command_args("echo", "bash")
        with self.assertRaises(ValueError):
            jobs.command_args("", "cmd")

if __name__ == "__main__":
    unittest.main()
