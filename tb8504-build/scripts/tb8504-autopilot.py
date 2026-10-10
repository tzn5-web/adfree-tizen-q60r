#!/usr/bin/env python3
"""TB8504 Android 16 staged self-healing autopilot.

This is intentionally a deterministic, fail-closed autopilot rather than an
unbounded self-modifying script. It can automatically repair only problems
whose fixes have already been audited and encoded in the repository. Unknown
errors produce a complete diagnostic bundle and stop before any device access.

Safety invariants:
  * no adb, no fastboot, no flash, no /dev/block writes;
  * no clean/installclean/clobber;
  * no weakening of kernel/module signing checks;
  * no export of private signing keys;
  * source mutation only through prevalidated idempotent handlers;
  * every repair is followed by the complete relevant audit set.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable

REPO = "tzn5-web/adfree-tizen-q60r"
BRANCH = "tb8504-android16-build"
KNOWLEDGE_PATH = "tb8504-build/config/autopilot-knowledge.json"
HELPERS = (
    "apply-audio-header-compat.py",
    "apply-camera-compat.py",
    "apply-runtime-cleanup.py",
    "apply-residual-cleanup.py",
    "apply-product-compat.py",
    "apply-performance-compat.py",
    "apply-golden-profile.py",
    "audit-product-compat.py",
    "audit-performance-compat.py",
    "audit-residual-contracts.py",
    "audit-stage8n.py",
    "audit-runtime-contracts.py",
    "audit-local-image.py",
    "audit-built-output.py",
    "audit-final-package.py",
)
AUXILIARY_TOOLS = (
    "export-cloud-seed.sh",
)
FORBIDDEN_COMMAND_PATTERNS = (
    r"(^|\s)adb(\s|$)",
    r"(^|\s)fastboot(\s|$)",
    r"(^|\s)(clean|installclean|clobber)(\s|$)",
    r"/dev/block",
    r"\bdd\s+.*\bof=/dev/",
)
EXPECTED_HEADS = {
    "device/lenovo/TB8504": "245eadab3c1e1732da1e4c1c99b2cc2fed74b91b",
    "vendor/lenovo/TB8504": "34690afc7eff4fc5223f4f2882c2054fcddcf986",
    "kernel/lenovo/msm8917": "d242d540d9f5328919e189235e74e433418f6d81",
}
ALLOWED_KERNEL_DIRTY = {
    "scripts/sign-file",
    "include/sound/Kbuild",
}
RELEASE = {
    "product": "lineage_TB8504",
    "release": "trunk_staging",
    "variant": "userdebug",
}
PARTITION_LIMITS = {
    "boot": 67108864,
    "recovery": 67108864,
    "system": 4080218112,
}
KNOWN_RECOVERY_SHA256 = "da0d80b0fc4c094ea529ff3daf50ef22570b3ede3d62ed6a7c725ddd91cb8e61"
KNOWN_RECOVERY_SIZE = 42618880
KNOWN_RECOVERY_KERNEL_SHA256 = "6dcb32cde2d172b7e4467cc9015ccb6c292668f2155f1749ac2e186d0c4577fe"

def is_android16_platform_version(platform_version: str, sdk_version: str) -> bool:
    """Accept AOSP development-codename or finalized numeric Android 16.

    AOSP trunk_staging can expose PLATFORM_VERSION=Baklava even with SDK 36.
    Require the exact SDK level and exact recognized version, never a prefix.
    """
    return sdk_version == "36" and platform_version in {"16", "Baklava"}


class StopAutopilot(RuntimeError):
    pass

class CommandResult:
    def __init__(self, rc: int, text: str, command: str):
        self.rc = rc
        self.text = text
        self.command = command

class Autopilot:
    def __init__(
        self,
        root: Path,
        goal: str,
        max_attempts: int,
        upload_seed: bool = False,
    ):
        self.root = root.resolve()
        self.goal = goal
        self.max_attempts = max_attempts
        self.upload_seed = upload_seed
        self.device = self.root / "device/lenovo/TB8504"
        self.vendor = self.root / "vendor/lenovo/TB8504"
        self.kernel = self.root / "kernel/lenovo/msm8917"
        self.product_out = self.root / "out/target/product/TB8504"
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.report = Path.home() / f"TB8504_AUTOPILOT_{stamp}"
        self.tools = self.report / "tools"
        self.logs = self.report / "logs"
        self.snapshots = self.report / "snapshots"
        for p in (self.report, self.tools, self.logs, self.snapshots):
            p.mkdir(parents=True, exist_ok=True)
        self.full_log = (self.report / "FULL.log").open("a", encoding="utf-8")
        self.tooling_ref = ""
        self.knowledge: dict = {}
        self.release_info: dict[str, str] = {}
        self.source_changed = False
        self.current_source_fingerprint = ""
        self.workspace_revision_fingerprint = ""
        self.unproven_sources: list[str] = []
        self.handler_uses: dict[str, int] = {}
        self.state_path = self.product_out / ".tb8504-autopilot-state.json"
        self.state = self._load_state()
        self.status: dict[str, object] = {
            "goal": goal,
            "started": dt.datetime.now().isoformat(),
            "root": str(self.root),
            "no_adb": True,
            "no_fastboot": True,
            "no_flash": True,
            "no_clean": True,
        }

    def say(self, msg: str = "") -> None:
        print(msg, flush=True)
        self.full_log.write(msg + "\n")
        self.full_log.flush()

    def _load_state(self) -> dict:
        if self.state_path.is_file():
            try:
                return json.loads(self.state_path.read_text("utf-8"))
            except Exception:
                return {}
        return {}

    def save_state(self) -> None:
        self.product_out.mkdir(parents=True, exist_ok=True)
        self.state["updated"] = dt.datetime.now().isoformat()
        self.state["tooling_ref"] = self.tooling_ref
        self.state_path.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def safety_check_command(self, command: str) -> None:
        for pat in FORBIDDEN_COMMAND_PATTERNS:
            if re.search(pat, command, re.I):
                raise StopAutopilot(f"SAFETY_BLOCKED_COMMAND pattern={pat!r} command={command!r}")

    def run(
        self,
        args: list[str] | str,
        *,
        cwd: Path | None = None,
        log_name: str | None = None,
        check: bool = False,
        env: dict[str, str] | None = None,
        shell: bool = False,
    ) -> CommandResult:
        printable = args if isinstance(args, str) else " ".join(args)
        self.safety_check_command(printable)
        self.say(f"$ {printable}")
        proc = subprocess.Popen(
            args,
            cwd=str(cwd or self.root),
            env=env,
            shell=shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        collected: list[str] = []
        lf = (self.logs / log_name).open("w", encoding="utf-8") if log_name else None
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                self.full_log.write(line)
                self.full_log.flush()
                collected.append(line)
                if lf:
                    lf.write(line)
                    lf.flush()
        finally:
            if lf:
                lf.close()
        rc = proc.wait()
        result = CommandResult(rc, "".join(collected), printable)
        if check and rc != 0:
            raise StopAutopilot(f"COMMAND_FAILED rc={rc}: {printable}")
        return result

    def capture(self, args: list[str], cwd: Path | None = None, check: bool = True) -> str:
        printable = " ".join(args)
        self.safety_check_command(printable)
        p = subprocess.run(
            args,
            cwd=str(cwd or self.root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and p.returncode != 0:
            raise StopAutopilot(
                f"COMMAND_FAILED rc={p.returncode}: {printable}\n{p.stdout}\n{p.stderr}"
            )
        return p.stdout.strip()

    def resolve_tooling_ref(self) -> str:
        pinned = os.environ.get("TB8504_TOOLING_REF", "").strip()
        if pinned:
            if not re.fullmatch(r"[0-9a-f]{40}", pinned):
                raise StopAutopilot(
                    f"invalid TB8504_TOOLING_REF override: {pinned!r}"
                )
            # Prove the pinned commit exists in the intended repository before
            # using it for any helper download.
            resolved = self.capture([
                "gh", "api", "--method", "GET",
                f"repos/{REPO}/commits/{pinned}",
                "--jq", ".sha",
            ])
            if resolved != pinned:
                raise StopAutopilot(
                    f"tooling pin mismatch: requested={pinned} resolved={resolved}"
                )
            out = pinned
            self.say("TOOLING_REF_MODE=PINNED")
        else:
            out = self.capture([
                "gh", "api", "--method", "GET",
                f"repos/{REPO}/branches/{BRANCH}",
                "--jq", ".commit.sha",
            ])
            self.say("TOOLING_REF_MODE=BRANCH_HEAD")

        if not re.fullmatch(r"[0-9a-f]{40}", out):
            raise StopAutopilot(f"invalid GitHub tooling ref: {out!r}")
        self.tooling_ref = out
        self.status["tooling_ref"] = out
        self.say(f"TOOLING_REF={out}")
        return out

    def verify_tooling_ci(self) -> None:
        raw = self.capture([
            "gh", "api", "--method", "GET",
            f"repos/{REPO}/actions/runs",
            "-f", f"head_sha={self.tooling_ref}",
            "-f", "event=push",
            "-f", "per_page=100",
        ])
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise StopAutopilot(
                f"cannot parse GitHub Actions state for tooling ref: {exc}"
            )
        runs = payload.get("workflow_runs", [])
        if not isinstance(runs, list):
            raise StopAutopilot("GitHub Actions response has no workflow_runs list")
        lint_runs = [
            x for x in runs
            if isinstance(x, dict)
            and x.get("name") == "TB8504 cloud lane lint"
            and x.get("head_sha") == self.tooling_ref
        ]
        if not lint_runs:
            raise StopAutopilot(
                f"no TB8504 cloud lane lint run found for tooling ref "
                f"{self.tooling_ref}"
            )
        latest = max(
            lint_runs,
            key=lambda x: int(x.get("run_number", 0) or 0),
        )
        status = str(latest.get("status", ""))
        conclusion = str(latest.get("conclusion", ""))
        run_id = latest.get("id")
        self.say(f"TOOLING_CI_RUN_ID={run_id}")
        self.say(f"TOOLING_CI_STATUS={status}")
        self.say(f"TOOLING_CI_CONCLUSION={conclusion}")
        if status != "completed" or conclusion != "success":
            raise StopAutopilot(
                f"tooling ref is not CI-approved: "
                f"run={run_id} status={status} conclusion={conclusion}"
            )
        self.say("TOOLING_CI_GATE=PASS")

    def fetch_repo_file(self, path: str, destination: Path) -> None:
        raw = self.capture([
            "gh", "api", "--method", "GET",
            f"repos/{REPO}/contents/{path}",
            "-f", f"ref={self.tooling_ref}",
            "--jq", ".content",
        ])
        try:
            data = base64.b64decode("".join(raw.splitlines()), validate=False)
        except Exception as exc:
            raise StopAutopilot(f"cannot decode {path}: {exc}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if not data:
            raise StopAutopilot(f"downloaded empty repository file: {path}")

    def refresh_tooling(self) -> None:
        self.resolve_tooling_ref()
        self.verify_tooling_ci()

        canonical_self = self.report / "canonical-tb8504-autopilot.py"
        self.fetch_repo_file(
            "tb8504-build/scripts/tb8504-autopilot.py", canonical_self
        )
        running_self = Path(__file__).resolve()
        running_sha = self.sha_file(running_self)
        canonical_sha = self.sha_file(canonical_self)
        self.say(f"RUNNING_AUTOPILOT_SHA256={running_sha}")
        self.say(f"PINNED_AUTOPILOT_SHA256={canonical_sha}")
        if running_sha != canonical_sha:
            raise StopAutopilot(
                "running autopilot differs from the pinned GitHub commit"
            )
        self.say("AUTOPILOT_SELF_INTEGRITY=PASS")

        for name in HELPERS:
            self.fetch_repo_file(f"tb8504-build/scripts/{name}", self.tools / name)
        for name in AUXILIARY_TOOLS:
            self.fetch_repo_file(f"tb8504-build/scripts/{name}", self.tools / name)
        self.fetch_repo_file(KNOWLEDGE_PATH, self.report / "autopilot-knowledge.json")
        try:
            self.knowledge = json.loads((self.report / "autopilot-knowledge.json").read_text("utf-8"))
        except Exception as exc:
            raise StopAutopilot(f"knowledge base parse failed: {exc}")
        p = self.run(
            ["python3", "-m", "py_compile", *[str(self.tools / x) for x in HELPERS]],
            log_name="helper-pycompile.log",
        )
        if p.rc != 0:
            raise StopAutopilot("helper Python syntax audit failed")
        self.say("TOOLSET_AUDIT=PASS")

    def preflight(self) -> None:
        self.say("============================================================")
        self.say("TB8504 ANDROID 16 SELF-HEALING STAGED AUTOPILOT")
        self.say("NO ADB / NO FASTBOOT / NO FLASH / NO CLEAN")
        self.say("============================================================")
        self.say(f"ROOT={self.root}")
        self.say(f"GOAL={self.goal}")
        required_cmds = (
            "git", "gh", "python3", "bash", "curl", "readelf", "sha256sum",
            "modinfo", "openssl",
        )
        for cmd in required_cmds:
            if not shutil.which(cmd):
                raise StopAutopilot(f"missing required command: {cmd}")
        for p in (
            self.root / "build/envsetup.sh", self.device, self.vendor, self.kernel,
        ):
            if not p.exists():
                raise StopAutopilot(f"missing required path: {p}")
        free = shutil.disk_usage(self.root).free
        self.say(f"FREE_BYTES={free}")
        if free < 15 * 1024**3:
            raise StopAutopilot(f"insufficient free space: {free} bytes")
        self.refresh_tooling()
        if self.upload_seed:
            auth = self.run(
                ["gh", "auth", "status"],
                log_name="gh-auth-status.log",
            )
            if auth.rc != 0:
                raise StopAutopilot(
                    "GitHub authentication required for --upload-seed"
                )
            remote_head = self.capture([
                "gh", "api", "--method", "GET",
                f"repos/{REPO}/branches/{BRANCH}",
                "--jq", ".commit.sha",
            ])
            if remote_head != self.tooling_ref:
                raise StopAutopilot(
                    f"GitHub branch moved before converge: "
                    f"{remote_head} != {self.tooling_ref}"
                )
            self.say("SEED_HANDOFF_PREFLIGHT=PASS")
        self.snapshot_sources("before")
        self.verify_source_heads()
        self.say("PREFLIGHT=PASS")

    def git_status(self, repo: Path) -> str:
        raw = self.git_raw(
            repo, ["status", "--short", "--untracked-files=all"]
        )
        return raw.decode("utf-8", "surrogateescape").rstrip("\n")

    def git_raw(self, repo: Path, args: list[str]) -> bytes:
        printable = "git " + " ".join(args)
        self.safety_check_command(printable)
        p = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if p.returncode != 0:
            raise StopAutopilot(
                f"COMMAND_FAILED rc={p.returncode}: {printable}\n"
                + p.stderr.decode("utf-8", "replace")
            )
        return p.stdout

    def git_diff_sha256(self, repo: Path) -> str:
        return hashlib.sha256(self.git_raw(repo, ["diff", "--binary", "HEAD"])).hexdigest()

    def git_untracked(self, repo: Path) -> list[str]:
        raw = self.git_raw(
            repo, ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        return sorted(
            x for x in raw.decode("utf-8", "surrogateescape").split("\0") if x
        )

    def hash_untracked(self, repo: Path, h: "hashlib._Hash") -> None:
        for rel in self.git_untracked(repo):
            p = repo / rel
            h.update(b"U\0" + rel.encode("utf-8", "surrogateescape") + b"\0")
            if p.is_symlink():
                h.update(b"L\0" + os.readlink(p).encode("utf-8", "surrogateescape"))
            elif p.is_file():
                mode = p.stat().st_mode & 0o7777
                h.update(b"M\0" + str(mode).encode("ascii") + b"\0")
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
            else:
                h.update(b"O")

    def untracked_manifest(self, repo: Path) -> tuple[str, list[dict[str, object]]]:
        rows: list[dict[str, object]] = []
        h = hashlib.sha256()
        for rel in self.git_untracked(repo):
            p = repo / rel
            row: dict[str, object] = {"path": rel}
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            if p.is_symlink():
                target = os.readlink(p)
                row.update({"type": "symlink", "target": target})
                h.update(b"L\0" + target.encode("utf-8", "surrogateescape"))
            elif p.is_file():
                st = p.stat()
                digest = self.sha_file(p)
                size = st.st_size
                mode = st.st_mode & 0o7777
                row.update({
                    "type": "file",
                    "mode": mode,
                    "size": size,
                    "sha256": digest,
                })
                h.update(
                    b"F\0"
                    + str(mode).encode("ascii")
                    + b"\0"
                    + str(size).encode("ascii")
                    + b"\0"
                    + digest.encode("ascii")
                )
            else:
                row.update({"type": "other"})
                h.update(b"O")
            rows.append(row)
        return h.hexdigest(), rows

    def hash_plain_tree(self, root: Path) -> str:
        h = hashlib.sha256()
        if not root.is_dir():
            raise StopAutopilot(f"source directory missing: {root}")
        for p in sorted(root.rglob("*"), key=lambda x: x.as_posix()):
            rel = p.relative_to(root).as_posix()
            if "/.git/" in f"/{rel}/" or rel == ".git":
                continue
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            if p.is_symlink():
                h.update(b"L\0" + os.readlink(p).encode("utf-8", "surrogateescape"))
            elif p.is_file():
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
        return h.hexdigest()

    def audit_workspace_repo_state(self) -> None:
        repo_bin = self.root / ".repo/repo/repo"
        if not repo_bin.is_file():
            raise StopAutopilot(f"repo launcher missing: {repo_bin}")

        listing = self.capture([str(repo_bin), "list", "-p"], cwd=self.root)
        projects = sorted(
            {line.strip() for line in listing.splitlines() if line.strip()}
        )
        if not projects:
            raise StopAutopilot("repo project list is empty")

        contracts = self.knowledge.get("source_integrity_contracts", {})
        allowed_dirty = set(EXPECTED_HEADS)
        allowed_dirty.update(contracts.get("static_target_repos", {}).keys())
        dynamic = contracts.get("dynamic_primary_repos", {})
        if isinstance(dynamic, dict):
            allowed_dirty.update(dynamic.keys())
        host = contracts.get("host_only_repo", {})
        if isinstance(host, dict) and host.get("path"):
            allowed_dirty.add(str(host["path"]))
        gps_rel = str(
            contracts.get(
                "required_unproven_repo", "hardware/qcom-caf/msm8996/gps"
            )
        )
        allowed_dirty.add(gps_rel)

        h = hashlib.sha256()
        rows: list[dict[str, object]] = []
        unexpected_dirty: list[dict[str, object]] = []
        for rel in projects:
            project = self.root / rel
            inside = self.capture(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=project,
                check=False,
            )
            if inside != "true":
                raise StopAutopilot(f"repo project is not a git worktree: {rel}")
            head = self.capture(["git", "rev-parse", "HEAD"], cwd=project)
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                raise StopAutopilot(f"invalid project HEAD {rel}: {head!r}")
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            h.update(head.encode("ascii") + b"\0")
            status = self.git_status(project)
            dirty = bool(status)
            rows.append({"path": rel, "head": head, "dirty": dirty})
            if dirty and rel not in allowed_dirty:
                unexpected_dirty.append(
                    {
                        "path": rel,
                        "status": status.splitlines()[:100],
                    }
                )

        local_manifests = self.root / ".repo/local_manifests"
        local_manifests_sha = (
            self.hash_plain_tree(local_manifests)
            if local_manifests.is_dir()
            else hashlib.sha256(b"").hexdigest()
        )
        h.update(b"LOCAL_MANIFESTS\0" + local_manifests_sha.encode("ascii") + b"\0")

        digest = h.hexdigest()
        self.workspace_revision_fingerprint = digest
        report = {
            "project_count": len(projects),
            "revision_fingerprint": digest,
            "local_manifests_sha256": local_manifests_sha,
            "allowed_dirty_repos": sorted(allowed_dirty),
            "unexpected_dirty_repos": unexpected_dirty,
            "projects": rows,
        }
        (self.report / "workspace-source-state.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.say(f"WORKSPACE_PROJECT_COUNT={len(projects)}")
        self.say(f"WORKSPACE_REVISION_FINGERPRINT={digest}")
        self.say(f"LOCAL_MANIFESTS_SHA256={local_manifests_sha}")
        self.say(f"WORKSPACE_UNEXPECTED_DIRTY={len(unexpected_dirty)}")
        if unexpected_dirty:
            raise StopAutopilot(
                "unexpected dirty repos outside source model: "
                + ",".join(str(x["path"]) for x in unexpected_dirty[:30])
            )

        workspace_contract = contracts.get("workspace_repo_contract", {})
        wanted = (
            workspace_contract.get("revision_fingerprint")
            if isinstance(workspace_contract, dict)
            else None
        )
        wanted_count = (
            workspace_contract.get("expected_project_count")
            if isinstance(workspace_contract, dict)
            else None
        )
        if wanted and digest != wanted:
            raise StopAutopilot(
                f"workspace revision fingerprint mismatch: {digest} != {wanted}"
            )
        if wanted_count is not None and int(wanted_count) != len(projects):
            raise StopAutopilot(
                f"workspace project count mismatch: {len(projects)} != "
                f"{wanted_count}"
            )
        if not wanted or wanted_count is None:
            if self.goal != "converge":
                raise StopAutopilot(
                    "build goal refused until full workspace revision baseline "
                    "is explicitly approved"
                )
            self.say("WORKSPACE_PROVENANCE=PARTIAL_CONVERGE_ONLY")
        else:
            self.say("WORKSPACE_PROVENANCE=PASS")

    def verify_extended_source_contracts(self) -> None:
        contracts = self.knowledge.get("source_integrity_contracts", {})
        static = contracts.get("static_target_repos", {})
        if not isinstance(static, dict) or not static:
            raise StopAutopilot("source integrity contract set missing/empty")

        static_report: dict[str, object] = {}
        for rel, spec in sorted(static.items()):
            repo = self.root / rel
            if not repo.is_dir():
                raise StopAutopilot(f"required source repo missing: {rel}")
            inside = self.capture(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=repo,
                check=False,
            )
            if inside != "true":
                raise StopAutopilot(f"required source path is not a git worktree: {rel}")
            head = self.capture(["git", "rev-parse", "HEAD"], cwd=repo)
            wanted_head = str(spec.get("head", ""))
            if head != wanted_head:
                raise StopAutopilot(
                    f"source HEAD mismatch {rel}: {head} != {wanted_head}"
                )
            patch_sha = self.git_diff_sha256(repo)
            wanted_patch = str(spec.get("patch_sha256", ""))
            if patch_sha != wanted_patch:
                raise StopAutopilot(
                    f"source patch mismatch {rel}: {patch_sha} != {wanted_patch}"
                )
            untracked = self.git_untracked(repo)
            if untracked:
                raise StopAutopilot(
                    f"unexpected untracked files in fixed source repo {rel}: "
                    f"{untracked[:30]}"
                )
            static_report[rel] = {
                "head": head,
                "patch_sha256": patch_sha,
                "status": self.git_status(repo).splitlines(),
                "untracked": [],
            }
            self.say(f"SOURCE_CONTRACT[{rel}]=PASS")

        (self.report / "static-source-state.json").write_text(
            json.dumps(static_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.say(f"STATIC_SOURCE_PROVENANCE_COUNT={len(static_report)}")
        self.say("STATIC_SOURCE_PROVENANCE_CAPTURE=PASS")

        host = contracts.get("host_only_repo", {})
        if isinstance(host, dict) and host:
            rel = str(host.get("path", ""))
            repo = self.root / rel
            if not repo.is_dir():
                raise StopAutopilot(f"host-only source repo missing: {rel}")
            head = self.capture(["git", "rev-parse", "HEAD"], cwd=repo)
            if head != str(host.get("head", "")):
                raise StopAutopilot(f"host-only repo HEAD mismatch: {rel} {head}")
            prefix = str(host.get("allowed_dirty_prefix", ""))
            bad = []
            for raw in self.git_status(repo).splitlines():
                if not raw.strip():
                    continue
                path = raw[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                if not path.startswith(prefix):
                    bad.append(raw)
            if bad:
                raise StopAutopilot(
                    f"host-only repo has target-relevant dirty paths: {bad[:30]}"
                )
            self.say(f"HOST_ONLY_DIRTY_SCOPE[{rel}]=PASS")

        gps_rel = str(
            contracts.get(
                "required_unproven_repo", "hardware/qcom-caf/msm8996/gps"
            )
        )
        gps = self.root / gps_rel
        gps_report: dict[str, object] = {
            "path": gps_rel,
            "exists": gps.exists(),
            "is_dir": gps.is_dir(),
        }
        gps_contract = contracts.get("gps_repo")
        actual_mode = "absent"
        if gps.is_dir():
            inside = self.capture(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=gps,
                check=False,
            )
            gps_report["git_worktree"] = inside == "true"
            if inside == "true":
                actual_mode = "git"
                gps_head = self.capture(["git", "rev-parse", "HEAD"], cwd=gps)
                gps_status = self.git_status(gps)
                gps_patch_sha = self.git_diff_sha256(gps)
                gps_untracked_sha, gps_untracked = self.untracked_manifest(gps)
                gps_report.update(
                    {
                        "head": gps_head,
                        "status": gps_status.splitlines(),
                        "patch_sha256": gps_patch_sha,
                        "untracked_manifest_sha256": gps_untracked_sha,
                        "untracked": gps_untracked,
                    }
                )
            else:
                raise StopAutopilot(
                    f"GPS source must be a git worktree for canonical v2 "
                    f"provenance/export: {gps_rel}"
                )

        gps_report["mode"] = actual_mode
        if isinstance(gps_contract, dict) and gps_contract:
            wanted_mode = str(gps_contract.get("mode", "git"))
            if actual_mode != wanted_mode:
                raise StopAutopilot(
                    f"GPS source mode mismatch: {actual_mode} != {wanted_mode}"
                )
            if wanted_mode == "git":
                if gps_report.get("head") != gps_contract.get("head"):
                    raise StopAutopilot(
                        f"GPS source HEAD mismatch: {gps_report.get('head')} != "
                        f"{gps_contract.get('head')}"
                    )
                if gps_report.get("patch_sha256") != gps_contract.get(
                    "patch_sha256"
                ):
                    raise StopAutopilot("GPS source patch fingerprint mismatch")
                if gps_report.get("untracked_manifest_sha256") != gps_contract.get(
                    "untracked_manifest_sha256"
                ):
                    raise StopAutopilot("GPS untracked fingerprint mismatch")
            elif wanted_mode != "absent":
                raise StopAutopilot(
                    f"unsupported GPS contract mode for canonical v2 seed: "
                    f"{wanted_mode}"
                )
            self.say(f"GPS_SOURCE_CONTRACT=PASS mode={wanted_mode}")
        else:
            self.unproven_sources.append(gps_rel)

        (self.report / "gps-source-state.json").write_text(
            json.dumps(gps_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if self.unproven_sources:
            self.say(
                "UNPROVEN_SOURCE_REPOS=" + ",".join(sorted(set(self.unproven_sources)))
            )
            if self.goal != "converge":
                raise StopAutopilot(
                    "build goal refused until all required source repos have "
                    "an explicit integrity contract"
                )
            self.say("SOURCE_PROVENANCE=PARTIAL_CONVERGE_ONLY")
        else:
            self.say("SOURCE_PROVENANCE=PASS")

    def validate_dynamic_primary_contracts(self) -> None:
        contracts = self.knowledge.get("source_integrity_contracts", {})
        dynamic = contracts.get("dynamic_primary_repos", {})
        if not isinstance(dynamic, dict) or not dynamic:
            raise StopAutopilot("dynamic primary source contracts missing")

        report: dict[str, object] = {}
        missing_contracts: list[str] = []
        for rel, spec in sorted(dynamic.items()):
            repo = self.root / rel
            if not repo.is_dir():
                raise StopAutopilot(f"dynamic primary repo missing: {rel}")
            head = self.capture(["git", "rev-parse", "HEAD"], cwd=repo)
            wanted_head = str(spec.get("head", ""))
            if head != wanted_head:
                raise StopAutopilot(
                    f"dynamic primary HEAD mismatch {rel}: {head} != {wanted_head}"
                )
            patch_sha = self.git_diff_sha256(repo)
            untracked_sha, untracked_rows = self.untracked_manifest(repo)
            report[rel] = {
                "head": head,
                "patch_sha256": patch_sha,
                "untracked_manifest_sha256": untracked_sha,
                "untracked": untracked_rows,
                "status": self.git_status(repo).splitlines(),
            }
            wanted_patch = spec.get("post_converge_patch_sha256")
            wanted_untracked = spec.get("post_converge_untracked_manifest_sha256")
            if not wanted_patch or not wanted_untracked:
                missing_contracts.append(rel)
                continue
            if patch_sha != wanted_patch:
                raise StopAutopilot(
                    f"post-convergence patch mismatch {rel}: "
                    f"{patch_sha} != {wanted_patch}"
                )
            if untracked_sha != wanted_untracked:
                raise StopAutopilot(
                    f"post-convergence untracked manifest mismatch {rel}: "
                    f"{untracked_sha} != {wanted_untracked}"
                )
            self.say(f"POST_CONVERGENCE_SOURCE_CONTRACT[{rel}]=PASS")

        (self.report / "primary-source-state.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if missing_contracts:
            self.say(
                "UNPROVEN_PRIMARY_SOURCE_REPOS="
                + ",".join(sorted(missing_contracts))
            )
            if self.goal != "converge":
                raise StopAutopilot(
                    "build goal refused until post-convergence device/vendor "
                    "fingerprints are explicitly approved"
                )
            self.say("PRIMARY_SOURCE_PROVENANCE=PARTIAL_CONVERGE_ONLY")
        else:
            self.say("PRIMARY_SOURCE_PROVENANCE=PASS")

    def compute_source_fingerprint(self) -> str:
        contracts = self.knowledge.get("source_integrity_contracts", {})
        rels = set(EXPECTED_HEADS)
        rels.update(contracts.get("static_target_repos", {}).keys())
        gps_rel = str(
            contracts.get(
                "required_unproven_repo", "hardware/qcom-caf/msm8996/gps"
            )
        )
        rels.add(gps_rel)

        h = hashlib.sha256()
        if not self.workspace_revision_fingerprint:
            raise StopAutopilot("workspace revision fingerprint missing")
        h.update(
            b"WORKSPACE\0"
            + self.workspace_revision_fingerprint.encode("ascii")
            + b"\0"
        )
        for rel in sorted(rels):
            repo = self.root / rel
            h.update(rel.encode("utf-8") + b"\0")
            if repo.is_dir():
                inside = self.capture(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=repo,
                    check=False,
                )
            else:
                inside = ""
            if inside == "true":
                head = self.capture(["git", "rev-parse", "HEAD"], cwd=repo)
                h.update(b"G\0" + head.encode("ascii") + b"\0")
                h.update(self.git_raw(repo, ["diff", "--binary", "HEAD"]))
                self.hash_untracked(repo, h)
            elif repo.is_dir():
                h.update(b"T\0" + self.hash_plain_tree(repo).encode("ascii"))
            else:
                h.update(b"MISSING")
        digest = h.hexdigest()
        self.say(f"SOURCE_FINGERPRINT={digest}")
        return digest

    def snapshot_sources(self, label: str) -> None:
        for name, repo in (("device", self.device), ("vendor", self.vendor), ("kernel", self.kernel)):
            status = self.git_status(repo)
            diff = self.git_raw(repo, ["diff", "--binary", "HEAD"]).decode(
                "utf-8", "replace"
            ).rstrip("\n")
            (self.snapshots / f"{name}-{label}.status.txt").write_text(status + ("\n" if status else ""), encoding="utf-8")
            (self.snapshots / f"{name}-{label}.patch").write_text(diff + ("\n" if diff else ""), encoding="utf-8")

    def verify_source_heads(self) -> None:
        for rel, expected in EXPECTED_HEADS.items():
            repo = self.root / rel
            actual = self.capture(["git", "rev-parse", "HEAD"], cwd=repo)
            self.say(f"HEAD[{rel}]={actual}")
            if actual != expected:
                raise StopAutopilot(f"unexpected source HEAD for {rel}: {actual} != {expected}")

        dirty = self.git_status(self.kernel)
        unknown: list[str] = []
        for raw in dirty.splitlines():
            if not raw.strip():
                continue
            path = raw[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path not in ALLOWED_KERNEL_DIRTY:
                unknown.append(raw)
        if unknown:
            raise StopAutopilot(f"unknown kernel dirty state: {unknown}")
        if dirty:
            self.say("KERNEL_DIRTY_STATE=KNOWN_BUILD_ONLY")
        else:
            self.say("KERNEL_DIRTY_STATE=CLEAN")

        self.audit_workspace_repo_state()
        # The helper accepts only the exact approved audio HEAD and either
        # original bytes or its deterministic reviewed postimage. The static
        # provenance gate immediately binds the resulting full patch hash.
        self.run_idempotent_transform(
            "apply-audio-header-compat.py",
            lambda report, patch: [
                "--audio", str(self.root / "hardware/qcom-caf/msm8996/audio"),
                "--patch-out", str(patch), "--report-out", str(report),
            ],
            "AUDIO_HEADER_COMPAT_STATE", "AUDIO_HEADER_COMPAT", "audio-header-compat",
        )
        self.verify_extended_source_contracts()
        self.say("SOURCE_HEAD_GATE=PASS")

    @staticmethod
    def parse_kv(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw in text.splitlines():
            if "=" in raw:
                k, v = raw.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def helper(self, name: str, args: list[str], log_name: str) -> CommandResult:
        return self.run(["python3", str(self.tools / name), *args], log_name=log_name)

    def run_idempotent_transform(
        self,
        name: str,
        args_factory: Callable[[Path, Path], list[str]],
        state_key: str,
        pass_key: str,
        prefix: str,
    ) -> bool:
        changed = False
        first_report = self.report / f"{prefix}-first.txt"
        first_patch = self.report / f"{prefix}-first.patch"
        r = self.helper(
            name,
            args_factory(first_report, first_patch),
            f"{prefix}-first.log",
        )
        if r.rc != 0:
            raise StopAutopilot(f"{prefix} transform failed rc={r.rc}")
        kv = self.parse_kv(first_report.read_text("utf-8", errors="replace"))
        if kv.get(pass_key) != "PASS":
            raise StopAutopilot(f"{prefix} did not report {pass_key}=PASS")
        if kv.get(state_key) == "APPLIED" or int(kv.get("CHANGED_FILES", "0") or 0) > 0:
            changed = True

        second_report = self.report / f"{prefix}-second.txt"
        second_patch = self.report / f"{prefix}-second.patch"
        r2 = self.helper(
            name,
            args_factory(second_report, second_patch),
            f"{prefix}-second.log",
        )
        if r2.rc != 0:
            raise StopAutopilot(f"{prefix} second-run idempotency failed rc={r2.rc}")
        kv2 = self.parse_kv(second_report.read_text("utf-8", errors="replace"))
        if kv2.get(pass_key) != "PASS":
            raise StopAutopilot(f"{prefix} second run did not report PASS")
        if kv2.get(state_key) != "ALREADY_APPLIED":
            raise StopAutopilot(f"{prefix} second run is not ALREADY_APPLIED: {kv2}")
        if int(kv2.get("CHANGED_FILES", "0") or 0) != 0:
            raise StopAutopilot(f"{prefix} second run changed files: {kv2}")
        if second_patch.exists() and second_patch.stat().st_size != 0:
            raise StopAutopilot(f"{prefix} second-run patch is not empty")
        self.say(f"{prefix.upper()}_IDEMPOTENCY=PASS")
        return changed

    def converge_sources(self) -> None:
        self.say("")
        self.say("=== SOURCE CONVERGENCE ===")

        runtime_changed = self.run_idempotent_transform(
            "apply-runtime-cleanup.py",
            lambda report, patch: [
                "--device", str(self.device),
                "--patch-out", str(patch),
                "--report-out", str(report),
            ],
            "RUNTIME_CLEANUP_STATE",
            "RUNTIME_CLEANUP",
            "runtime-cleanup",
        )

        residual_changed = self.run_idempotent_transform(
            "apply-residual-cleanup.py",
            lambda report, patch: [
                "--device", str(self.device),
                "--vendor", str(self.vendor),
                "--patch-out", str(patch),
                "--report-out", str(report),
            ],
            "RESIDUAL_CLEANUP_STATE",
            "RESIDUAL_CLEANUP",
            "residual-cleanup",
        )

        product_changed = self.run_idempotent_transform(
            "apply-product-compat.py",
            lambda report, patch: [
                "--device", str(self.device),
                "--patch-out", str(patch),
                "--report-out", str(report),
            ],
            "PRODUCT_COMPAT_STATE",
            "PRODUCT_COMPAT",
            "product-compat",
        )

        camera_changed = self.run_idempotent_transform(
            "apply-camera-compat.py",
            lambda report, patch: [
                "--device", str(self.device),
                "--patch-out", str(patch),
                "--report-out", str(report),
            ],
            "CAMERA_COMPAT_STATE", "CAMERA_COMPAT", "camera-compat",
        )

        performance_changed = self.run_idempotent_transform(
            "apply-performance-compat.py",
            lambda report, patch: [
                "--device", str(self.device),
                "--patch-out", str(patch),
                "--report-out", str(report),
            ],
            "PERFORMANCE_COMPAT_STATE",
            "PERFORMANCE_COMPAT",
            "performance-compat",
        )

        golden_changed = self.run_idempotent_transform(
            "apply-golden-profile.py",
            lambda report, patch: [
                "--device", str(self.device),
                "--patch-out", str(patch),
                "--report-out", str(report),
            ],
            "GOLDEN_PROFILE_STATE", "GOLDEN_PROFILE", "golden-profile",
        )

        self.source_changed = (
            golden_changed
            or runtime_changed
            or residual_changed
            or product_changed
            or camera_changed
            or performance_changed
        )
        self.say(f"SOURCE_CHANGED_THIS_RUN={'YES' if self.source_changed else 'NO'}")

        shell_files = sorted((self.device / "rootdir").rglob("*.sh"))
        if not shell_files:
            raise StopAutopilot("no device rootdir shell scripts found")
        for p in shell_files:
            r = self.run(["bash", "-n", str(p)], log_name=f"bash-n-{p.name}.log")
            if r.rc != 0:
                raise StopAutopilot(f"shell syntax failure: {p}")
        self.say(f"ROOTDIR_SHELL_SYNTAX_COUNT={len(shell_files)}")
        self.say("ROOTDIR_SHELL_SYNTAX=PASS")

        r = self.helper(
            "audit-product-compat.py",
            ["--device", str(self.device)],
            "audit-product-compat.log",
        )
        if r.rc != 0 or "TB8504_PRODUCT_COMPAT_AUDIT=PASS" not in r.text:
            raise StopAutopilot("product compatibility audit failed")

        r = self.helper(
            "audit-performance-compat.py",
            ["--device", str(self.device)],
            "audit-performance-compat.log",
        )
        if r.rc != 0 or "TB8504_PERFORMANCE_SOURCE_AUDIT=PASS" not in r.text:
            raise StopAutopilot("low-end graphics performance source audit failed")

        r = self.helper(
            "audit-residual-contracts.py",
            ["--device", str(self.device), "--vendor", str(self.vendor)],
            "audit-residual-contracts.log",
        )
        if r.rc != 0 or "TB8504_RESIDUAL_AUDIT=PASS" not in r.text:
            raise StopAutopilot("residual cross-layer audit failed")

        stage_dir = self.report / "stage8n"
        stage_dir.mkdir(exist_ok=True)
        r = self.helper(
            "audit-stage8n.py",
            [
                "--vendor", str(self.vendor),
                "--device", str(self.device),
                "--report-dir", str(stage_dir),
            ],
            "audit-stage8n.log",
        )
        if r.rc != 0:
            raise StopAutopilot("STAGE8N ELF audit failed")
        for marker in (
            "UNRESOLVED_EDGES=0",
            "WRONG_BITNESS_EDGES=0",
            "GNSS_BAD_EDGES=0",
            "STAGE8N_GITHUB_SOURCE_AUDIT=PASS",
        ):
            if marker not in r.text:
                raise StopAutopilot(f"missing STAGE8N success marker: {marker}")

        runtime_dir = self.report / "runtime-source-only"
        runtime_dir.mkdir(exist_ok=True)
        r = self.helper(
            "audit-runtime-contracts.py",
            [
                "--vendor", str(self.vendor),
                "--device", str(self.device),
                "--report-dir", str(runtime_dir),
            ],
            "audit-runtime-contracts-source-only.log",
        )
        if r.rc != 0 or "RUNTIME_CONTRACT_FAILURES=0" not in r.text:
            raise StopAutopilot("source-only runtime init/VINTF contract audit failed")
        self.say("RUNTIME_MODULE_INFO_MODE=SOURCE_ONLY")
        self.say("RUNTIME_SOURCE_ONLY_AUDIT=PASS")

        self.snapshot_sources("after-convergence")
        self.validate_dynamic_primary_contracts()
        self.current_source_fingerprint = self.compute_source_fingerprint()
        self.status["source_fingerprint"] = self.current_source_fingerprint
        self.state["source_fingerprint"] = self.current_source_fingerprint
        self.save_state()
        self.say("SOURCE_CONVERGENCE=PASS")

    def android_shell(self, body: str, log_name: str | None = None) -> CommandResult:
        prefix = (
            "set -eo pipefail; set +u; "
            f"cd {sh_quote(str(self.root))}; "
            "source build/envsetup.sh >/dev/null; "
            "lunch lineage_TB8504 trunk_staging userdebug >/dev/null; "
        )
        return self.run(
            ["bash", "-lc", prefix + body],
            cwd=self.root,
            log_name=log_name,
        )

    def release_gate(self) -> None:
        self.say("")
        self.say("=== ANDROID 16 RELEASE IDENTITY ===")
        body = r'''
printf 'TARGET_PRODUCT=%s\n' "$TARGET_PRODUCT"
printf 'TARGET_RELEASE=%s\n' "$TARGET_RELEASE"
printf 'TARGET_VARIANT=%s\n' "$TARGET_BUILD_VARIANT"
printf 'PLATFORM_VERSION=%s\n' "$(get_build_var PLATFORM_VERSION)"
printf 'PLATFORM_SDK_VERSION=%s\n' "$(get_build_var PLATFORM_SDK_VERSION)"
printf 'LINEAGE_VERSION=%s\n' "$(get_build_var LINEAGE_VERSION)"
printf 'BUILD_ID=%s\n' "$(get_build_var BUILD_ID)"
printf 'BOARD_VENDORIMAGE_PARTITION_SIZE=%s\n' "$(get_build_var BOARD_VENDORIMAGE_PARTITION_SIZE 2>/dev/null || true)"
printf 'TARGET_COPY_OUT_VENDOR=%s\n' "$(get_build_var TARGET_COPY_OUT_VENDOR 2>/dev/null || true)"
printf 'BOARD_SYSTEMIMAGE_PARTITION_SIZE=%s\n' "$(get_build_var BOARD_SYSTEMIMAGE_PARTITION_SIZE 2>/dev/null || true)"
'''
        r = self.android_shell(body, "release-identity.log")
        if r.rc != 0:
            raise StopAutopilot("Android release identity query failed")
        info = self.parse_kv(r.text)
        expected = {
            "TARGET_PRODUCT": "lineage_TB8504",
            "TARGET_RELEASE": "trunk_staging",
            "TARGET_VARIANT": "userdebug",
            "PLATFORM_SDK_VERSION": "36",
        }
        for key, wanted in expected.items():
            if info.get(key) != wanted:
                raise StopAutopilot(f"release identity mismatch {key}: {info.get(key)!r} != {wanted!r}")
        if not is_android16_platform_version(
            info.get("PLATFORM_VERSION", ""), info.get("PLATFORM_SDK_VERSION", "")
        ):
            raise StopAutopilot(
                "platform is not Android 16: "
                f"version={info.get('PLATFORM_VERSION')!r} "
                f"sdk={info.get('PLATFORM_SDK_VERSION')!r}"
            )
        if not info.get("LINEAGE_VERSION", "").startswith("23.2-"):
            raise StopAutopilot(f"Lineage version is not 23.2: {info.get('LINEAGE_VERSION')!r}")
        self.release_info = info
        release_file = self.report / "release.txt"
        release_file.write_text("\n".join(f"{k}={v}" for k, v in info.items()) + "\n", encoding="utf-8")
        self.say("ANDROID16_RELEASE_IDENTITY=PASS")

    def refresh_module_metadata(self) -> None:
        """Regenerate module-info.json after source convergence before builds.

        For converge this is metadata generation only, not an Android image
        build. The audited v2 seed is exported only after module-info has been
        regenerated and runtime contracts have passed against that exact file.
        """
        self.say("")
        self.say("=== REFRESH ANDROID MODULE METADATA ===")
        module_info = self.product_out / "module-info.json"
        before_sha = self.sha_file(module_info) if module_info.is_file() else ""
        before_mtime = module_info.stat().st_mtime_ns if module_info.is_file() else 0

        r = self.android_shell("refreshmod", "refresh-module-info.log")
        if r.rc != 0:
            raise StopAutopilot(
                f"refreshmod failed before build goal rc={r.rc}"
            )
        if not module_info.is_file() or module_info.stat().st_size <= 0:
            raise StopAutopilot(
                f"refreshmod did not produce module-info.json: {module_info}"
            )
        try:
            parsed=json.loads(module_info.read_text("utf-8",errors="replace"))
        except Exception as exc:
            raise StopAutopilot(f"refreshed module-info.json is invalid: {exc}")
        if not isinstance(parsed,dict) or not parsed:
            raise StopAutopilot("refreshed module-info.json is empty/non-object")

        after_sha=self.sha_file(module_info)
        after_mtime=module_info.stat().st_mtime_ns
        self.say(f"MODULE_INFO_BEFORE_SHA256={before_sha}")
        self.say(f"MODULE_INFO_AFTER_SHA256={after_sha}")
        self.say(f"MODULE_INFO_ENTRIES={len(parsed)}")
        self.say(
            "MODULE_INFO_REFRESHED="
            + ("YES" if after_sha!=before_sha or after_mtime!=before_mtime else "UNCHANGED_VALID")
        )

        runtime_dir=self.report/"runtime-refreshed-module-info"
        runtime_dir.mkdir(exist_ok=True)
        rr=self.helper(
            "audit-runtime-contracts.py",
            [
                "--vendor",str(self.vendor),
                "--device",str(self.device),
                "--report-dir",str(runtime_dir),
                "--module-info",str(module_info),
            ],
            "audit-runtime-contracts-refreshed-module-info.log",
        )
        if rr.rc!=0 or "RUNTIME_CONTRACT_FAILURES=0" not in rr.text:
            raise StopAutopilot(
                "runtime contract audit failed with refreshed module-info.json"
            )

        self.state["module_info"]={
            "sha256":after_sha,
            "source_fingerprint":self.current_source_fingerprint,
            "entries":len(parsed),
            "audited":dt.datetime.now().isoformat(),
            "tooling_ref":self.tooling_ref,
        }
        self.save_state()
        self.say("MODULE_INFO_SOURCE_BINDING=PASS")
        self.say("RUNTIME_REFRESHED_MODULE_INFO_AUDIT=PASS")

        if self.goal == "converge":
            self.export_converged_seed()

    def export_converged_seed(self) -> Path:
        """Export an audited v2 seed from the exact converged source state.

        No Android build is performed here. Upload is allowed only when the
        user explicitly selected --upload-seed. The exporter and its
        cloud-seed auditor are pinned to the same immutable tooling commit as
        this running autopilot.
        """
        self.say("")
        self.say("=== EXPORT AUDITED CLOUD SEED V2 ===")
        exporter=self.tools/"export-cloud-seed.sh"
        if not exporter.is_file() or exporter.stat().st_size<=0:
            raise StopAutopilot(f"pinned seed exporter missing: {exporter}")

        r=self.run(
            [
                "env",
                f"TB8504_TOOLING_REF={self.tooling_ref}",
                f"TB8504_PROVENANCE_DIR={self.report}",
                f"TB8504_UPLOAD_DRAFT={'1' if self.upload_seed else '0'}",
                f"TB8504_GITHUB_REPO={REPO}",
                f"TB8504_GITHUB_TARGET={BRANCH}",
                "bash",str(exporter),str(self.root),
            ],
            log_name="export-cloud-seed-v2.log",
        )
        if r.rc!=0:
            raise StopAutopilot(f"cloud seed v2 export failed rc={r.rc}")
        kv=self.parse_kv(r.text)
        if kv.get("FINAL_RC")!="0":
            raise StopAutopilot(
                f"cloud seed exporter did not report FINAL_RC=0: {kv}"
            )
        if "ARCHIVE_INTEGRITY=PASS" not in r.text:
            raise StopAutopilot("cloud seed archive integrity marker missing")
        if "LOCAL_CLOUD_SEED_AUDIT=PASS" not in r.text:
            raise StopAutopilot("local cloud-seed auditor did not pass")

        raw=kv.get("CLOUD_SEED_ARCHIVE","")
        archive=Path(raw)
        if not raw or not archive.is_file() or archive.stat().st_size<=0:
            raise StopAutopilot(f"cloud seed archive missing after export: {raw!r}")
        seed_sha=self.sha_file(archive)
        uploaded=False
        release_tag=kv.get("DRAFT_RELEASE_TAG","")
        request_path=kv.get("STAGE8N_REQUEST_PATH","")
        request_commit=kv.get("STAGE8N_REQUEST_COMMIT","")
        request_parent=kv.get("STAGE8N_REQUEST_PARENT","")
        if self.upload_seed:
            if not release_tag or release_tag=="NONE":
                raise StopAutopilot(
                    "seed upload requested but no draft release tag was produced"
                )
            if not request_path.startswith(
                "tb8504-build/requests/live/stage8n-"
            ):
                raise StopAutopilot(
                    "seed upload requested but STAGE8N request was not committed"
                )
            if not re.fullmatch(r"[0-9a-f]{40}", request_commit):
                raise StopAutopilot(
                    "seed upload requested but request commit SHA is missing/invalid"
                )
            if request_parent != self.tooling_ref:
                raise StopAutopilot(
                    f"STAGE8N request parent mismatch: "
                    f"{request_parent!r} != {self.tooling_ref!r}"
                )
            if "STAGE8N_HANDOFF_PROVENANCE=PASS" not in r.text:
                raise StopAutopilot(
                    "seed handoff provenance marker missing"
                )
            uploaded=True

        self.status["cloud_seed_v2"]={
            "path":str(archive),
            "sha256":seed_sha,
            "size":archive.stat().st_size,
            "tooling_ref":self.tooling_ref,
            "uploaded":uploaded,
            "release_tag":release_tag or "NONE",
            "stage8n_request":request_path or "NONE",
            "stage8n_request_commit":request_commit or "NONE",
            "stage8n_request_parent":request_parent or "NONE",
        }
        self.say(f"CLOUD_SEED_V2={archive}")
        self.say(f"CLOUD_SEED_V2_SIZE={archive.stat().st_size}")
        self.say(f"CLOUD_SEED_V2_SHA256={seed_sha}")
        self.say(f"CLOUD_SEED_V2_UPLOAD={'YES' if uploaded else 'NO'}")
        if uploaded:
            self.say(f"CLOUD_SEED_V2_RELEASE_TAG={release_tag}")
            self.say(f"CLOUD_SEED_V2_STAGE8N_REQUEST={request_path}")
            self.say(f"CLOUD_SEED_V2_STAGE8N_COMMIT={request_commit}")
            self.say(f"CLOUD_SEED_V2_STAGE8N_PARENT={request_parent}")
            self.say("CLOUD_SEED_V2_HANDOFF=PASS")
        self.say("CLOUD_SEED_V2_EXPORT=PASS")
        return archive

    def detect_vendor_partition(self) -> bool:
        size = self.release_info.get("BOARD_VENDORIMAGE_PARTITION_SIZE", "").strip()
        numeric = int(size, 0) if size and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", size) else 0
        fstab = self.device / "rootdir/fstab.qcom"
        has_vendor_mount = False
        if fstab.is_file():
            for line in fstab.read_text("utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                cols = s.split()
                if len(cols) >= 2 and cols[1] == "/vendor":
                    has_vendor_mount = True
                    break
        separate = numeric > 0 or has_vendor_mount
        self.say(f"VENDOR_PARTITION_SIZE={numeric}")
        self.say(f"FSTAB_VENDOR_MOUNT={'YES' if has_vendor_mount else 'NO'}")
        self.say(f"SEPARATE_VENDOR_PARTITION={'YES' if separate else 'NO'}")
        if separate:
            raise StopAutopilot(
                "unexpected separate vendor partition: TB8504 physical layout "
                "requires integrated system/vendor"
            )
        self.say("VENDOR_TOPOLOGY=INTEGRATED_SYSTEM_VENDOR")
        return False

    def sha_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def image_binding_current(self, kind: str, image: Path) -> bool:
        if not self.current_source_fingerprint:
            raise StopAutopilot("source fingerprint missing before image audit")
        row = self.state.get("images", {}).get(kind, {})
        if not isinstance(row, dict):
            return False
        if row.get("source_fingerprint") != self.current_source_fingerprint:
            return False
        if row.get("sha256") != self.sha_file(image):
            return False
        return True

    def audit_image(self, kind: str, fresh: bool = False) -> bool:
        image = self.product_out / f"{kind}.img"
        if not image.is_file():
            return False
        sha = self.sha_file(image)
        known_recovery = (
            kind == "recovery"
            and image.stat().st_size == KNOWN_RECOVERY_SIZE
            and sha == KNOWN_RECOVERY_SHA256
        )
        if not fresh and not self.image_binding_current(kind, image):
            self.say(f"{kind.upper()}_SOURCE_BINDING=STALE_OR_MISSING")
            return False

        release_file = self.report / "release.txt"
        r = self.helper(
            "audit-local-image.py",
            [
                "--root", str(self.root),
                "--kind", kind,
                "--release-report", str(release_file),
                "--image", str(image),
            ],
            f"audit-{kind}-image.log",
        )
        if r.rc != 0:
            self.say(f"{kind.upper()}_EXISTING_IMAGE_AUDIT=STALE_OR_FAIL")
            return False
        marker = f"{kind.upper()}_IMAGE_AUDIT=PASS"
        if marker not in r.text:
            return False
        self.state.setdefault("images", {})[kind] = {
            "sha256": sha,
            "size": image.stat().st_size,
            "source_fingerprint": self.current_source_fingerprint,
            "audited": dt.datetime.now().isoformat(),
            "tooling_ref": self.tooling_ref,
        }
        self.save_state()
        self.say(f"{kind.upper()}_SOURCE_BINDING=PASS")
        self.say(f"{kind.upper()}_IMAGE_SHA256={sha}")
        self.say(f"{kind.upper()}_IMAGE_AUDIT=PASS")
        if kind == "recovery":
            if known_recovery:
                self.say("RECOVERY_MATCHES_20261007_PROVEN_BASELINE=YES")
            if KNOWN_RECOVERY_KERNEL_SHA256 in r.text:
                self.say("RECOVERY_KERNEL_MATCHES_20261007_PROVEN_BASELINE=YES")
        return True

    def classify_failure(self, text: str, target: str) -> tuple[str, str] | None:
        rules: list[tuple[str, str, tuple[str, ...], bool]] = [
            (
                "audio_thread_signature",
                "legacy speaker calibration pthread entry point signature",
                (r"spkr_protection\.c:\d+:\d+: error: incompatible function pointer types passing",),
                True,
            ),
            (
                "audio_headers",
                "legacy audio module missing generated kernel header dependency",
                ("fatal error: 'sound/voice_params.h' file not found",),
                False,
            ),
            (
                "camera_compat",
                "unguarded Qualcomm camera metadata callback in VANILLA_HAL",
                ("error: use of undeclared identifier 'CAMERA_MSG_META_DATA'",),
                False,
            ),
            (
                "product_compat",
                "legacy product property override",
                (
                    'Key "PRIVATE_BUILD_DESC" isn\'t a valid prop override',
                    'Key "TARGET_DEVICE" isn\'t a valid prop override',
                    "PRIVATE_BUILD_DESC",
                ),
                False,
            ),
            (
                "residual_cleanup",
                "residual IMS/WFD/init contract regression",
                (
                    "boot ramdisk missing restored IMS service",
                    "stale removed-service controls",
                    "RESIDUAL_CLEANUP_FAIL=",
                    "WfdService",
                    "WfdCommon",
                ),
                False,
            ),
            (
                "source_convergence",
                "VINTF/runtime source regression",
                (
                    r"^\s*INCOMPATIBLE(?:\s|$)",
                    r"^CHECKVINTF_RC=[1-9]\d*\s*$",
                    r"^STALE_VINTF_HAL_DECLARATIONS=[1-9]\d*\s*$",
                    r"^UNRESOLVED_INIT_SERVICES=[1-9]\d*\s*$",
                ),
                True,
            ),
            (
                "release_identity",
                "wrong Android lunch/release identity",
                (
                    "Invalid lunch combo",
                    "Valid combos must be of the form",
                    "TOP: unbound variable",
                ),
                False,
            ),
            (
                "no_space",
                "insufficient disk space",
                ("No space left on device",),
                False,
            ),
            (
                "vendor_topology",
                "vendorimage requested on non-separate vendor topology",
                ("No rule to make target 'vendorimage'", "unknown target 'vendorimage'"),
                False,
            ),
            (
                "selinux_unknown",
                "SELinux policy failure requires explicit review",
                ("neverallow", "sepolicy"),
                False,
            ),
        ]
        low = text.lower()
        for handler, reason, pats, regex in rules:
            for pat in pats:
                if (re.search(pat, text, re.I | re.M) if regex else pat.lower() in low):
                    return handler, reason
        return None

    def apply_handler(self, handler: str, target: str) -> bool:
        used = self.handler_uses.get(handler, 0)
        if used >= 1:
            return False
        self.handler_uses[handler] = used + 1
        self.say(f"SELF_HEAL_HANDLER={handler}")

        if handler == "product_compat":
            report = self.report / "handler-product-compat.txt"
            patch = self.report / "handler-product-compat.patch"
            r = self.helper(
                "apply-product-compat.py",
                [
                    "--device", str(self.device),
                    "--patch-out", str(patch),
                    "--report-out", str(report),
                ],
                "handler-product-compat.log",
            )
            if r.rc != 0:
                return False
            self.converge_sources()
            self.release_gate()
            return True

        if handler == "residual_cleanup":
            self.converge_sources()
            self.release_gate()
            return True

        if handler in {"source_convergence", "camera_compat"}:
            self.converge_sources()
            self.release_gate()
            return True

        if handler == "release_identity":
            self.release_gate()
            return True

        if handler == "vendor_topology":
            return not self.detect_vendor_partition()

        if handler in {"no_space", "selinux_unknown"}:
            return False

        return False

    def artifact_state(self, path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        st = path.stat()
        return {
            "sha256": self.sha_file(path),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }

    def build_binding_current(self, kind: str) -> bool:
        if kind in {"boot", "recovery"}:
            image = self.product_out / f"{kind}.img"
            return image.is_file() and self.image_binding_current(kind, image)
        return self.partition_binding_current(kind)

    def build_target(self, target: str) -> str:
        self.say("")
        self.say(f"=== BUILD TARGET: {target} ===")
        target_kinds = {
            "bootimage": ("boot",),
            "recoveryimage": ("recovery",),
            "bootimage recoveryimage": ("boot", "recovery"),
            "systemimage": ("system",),
            "vendorimage": ("vendor",),
            "bacon": ("boot", "recovery", "system"),
        }.get(target, ())
        for attempt in range(1, self.max_attempts + 1):
            before_artifacts = {
                kind: self.artifact_state(self.product_out / f"{kind}.img")
                for kind in target_kinds
            }
            stale_before = {
                kind: not self.build_binding_current(kind)
                for kind in target_kinds
            }
            for kind in target_kinds:
                if not stale_before[kind]:
                    continue
                stale_path = self.product_out / f"{kind}.img"
                if stale_path.is_file() or stale_path.is_symlink():
                    old_sha = self.sha_file(stale_path) if stale_path.is_file() else "SYMLINK"
                    self.say(
                        f"{kind.upper()}_STALE_ARTIFACT_REMOVE="
                        f"{stale_path} sha256={old_sha}"
                    )
                    stale_path.unlink()
                    if stale_path.exists() or stale_path.is_symlink():
                        raise StopAutopilot(
                            f"cannot remove stale {kind}.img before rebuild"
                        )
            self.say(f"BUILD_ATTEMPT={attempt}/{self.max_attempts}")
            command = "mka " + " ".join(sh_quote(part) for part in target.split())
            r = self.android_shell(command, f"build-{target.replace(chr(32), chr(45))}-attempt{attempt}.log")
            if r.rc == 0:
                for kind in target_kinds:
                    after = self.artifact_state(self.product_out / f"{kind}.img")
                    if after is None:
                        raise StopAutopilot(
                            f"successful {target} produced no {kind}.img"
                        )
                    if stale_before[kind]:
                        self.say(
                            f"{kind.upper()}_STALE_ARTIFACT_FORCE_REBUILD=PASS "
                            f"target={target}"
                        )
                        self.say(
                            f"{kind.upper()}_ARTIFACT_REFRESH=PASS "
                            f"target={target}"
                        )
                self.say(f"BUILD_TARGET={target}")
                self.say("BUILD_RC=0")
                return "BUILT"

            if r.rc in (130, -2):
                raise StopAutopilot(f"user/interruption stopped build target {target}")

            classified = self.classify_failure(r.text, target)
            if not classified:
                self.write_unknown_error(target, r)
                raise StopAutopilot(f"UNKNOWN_BUILD_ERROR target={target} rc={r.rc}")

            handler, reason = classified
            self.say(f"KNOWN_ERROR={reason}")
            if handler == "vendor_topology" and target == "vendorimage":
                if self.apply_handler(handler, target):
                    self.say("VENDORIMAGE=SKIPPED_NOT_A_REAL_PARTITION")
                    return "SKIPPED"
                raise StopAutopilot("vendorimage topology mismatch could not be resolved")

            if not self.apply_handler(handler, target):
                self.write_unknown_error(target, r)
                raise StopAutopilot(
                    f"known error handler refused/failed: handler={handler} target={target}"
                )
            self.say("SELF_HEAL=APPLIED_REAUDIT_COMPLETE")

        raise StopAutopilot(f"build attempts exhausted for {target}")

    def ensure_image(self, kind: str) -> None:
        self.say("")
        self.say(f"=== ENSURE {kind.upper()} IMAGE ===")
        if not self.source_changed and self.audit_image(kind):
            self.say(f"{kind.upper()}_BUILD=REUSED_VALIDATED")
            return

        target = f"{kind}image"
        self.build_target(target)
        if not self.audit_image(kind, fresh=True):
            rlog = (self.logs / f"audit-{kind}-image.log")
            text = rlog.read_text("utf-8", errors="replace") if rlog.is_file() else ""
            classified = self.classify_failure(text, target)
            if classified and self.apply_handler(classified[0], target):
                self.build_target(target)
                if self.audit_image(kind, fresh=True):
                    return
            raise StopAutopilot(f"{kind} image audit failed after successful build")
        self.say(f"{kind.upper()}_BUILD=PASS")

    def partition_binding_current(self, kind: str) -> bool:
        image = self.product_out / f"{kind}.img"
        if not image.is_file() or not self.current_source_fingerprint:
            return False
        row = self.state.get("images", {}).get(kind, {})
        if not isinstance(row, dict):
            return False
        return (
            row.get("source_fingerprint") == self.current_source_fingerprint
            and row.get("sha256") == self.sha_file(image)
        )

    def audit_partition_image(self, kind: str, limit: int | None) -> None:
        image = self.product_out / f"{kind}.img"
        if not image.is_file() or image.stat().st_size <= 0:
            raise StopAutopilot(f"{kind}.img missing/empty after successful build")
        stored = image.stat().st_size
        with image.open("rb") as fh:
            header = fh.read(28)
        expanded = stored
        sparse = False
        if len(header) >= 28 and struct.unpack_from("<I", header, 0)[0] == 0xED26FF3A:
            sparse = True
            (
                _magic, major, minor, file_hdr_sz, chunk_hdr_sz,
                blk_sz, total_blks, total_chunks, checksum,
            ) = struct.unpack_from("<I4H4I", header, 0)
            if major != 1 or file_hdr_sz < 28 or chunk_hdr_sz < 12 or blk_sz <= 0:
                raise StopAutopilot(f"{kind}.img has invalid Android sparse header")
            expanded = blk_sz * total_blks
        if limit is not None and expanded > limit:
            raise StopAutopilot(
                f"{kind}.img expanded size exceeds partition: {expanded} > {limit}"
            )
        sha = self.sha_file(image)
        self.say(f"{kind.upper()}_IMAGE_SPARSE={'YES' if sparse else 'NO'}")
        self.say(f"{kind.upper()}_IMAGE_STORED_SIZE={stored}")
        self.say(f"{kind.upper()}_IMAGE_EXPANDED_SIZE={expanded}")
        if limit is not None:
            self.say(f"{kind.upper()}_PARTITION_LIMIT={limit}")
        self.say(f"{kind.upper()}_IMAGE_SHA256={sha}")
        self.state.setdefault("images", {})[kind] = {
            "sha256": sha,
            "size": stored,
            "expanded_size": expanded,
            "source_fingerprint": self.current_source_fingerprint,
            "audited": dt.datetime.now().isoformat(),
            "tooling_ref": self.tooling_ref,
        }
        self.save_state()
        self.say(f"{kind.upper()}_BASIC_IMAGE_AUDIT=PASS")

    def run_built_output_audit(self, log_name: str) -> CommandResult:
        output_dir = self.report / "actual-built-output"
        output_dir.mkdir(exist_ok=True)
        return self.helper(
            "audit-built-output.py",
            ["--root", str(self.root), "--report-dir", str(output_dir)],
            log_name,
        )

    def require_built_output_audit(self, target: str, rebuild_target: str) -> None:
        r = self.run_built_output_audit(f"audit-built-output-{target}.log")
        if r.rc == 0 and "ACTUAL_BUILT_OUTPUT_AUDIT=PASS" in r.text:
            self.say("ACTUAL_BUILT_OUTPUT_AUDIT=PASS")
            return

        classified = self.classify_failure(r.text, target)
        if classified and self.apply_handler(classified[0], target):
            self.say("BUILT_OUTPUT_SELF_HEAL_REQUIRES_REBUILD=YES")
            self.build_target(rebuild_target)
            if rebuild_target == "systemimage":
                raw = self.release_info.get("BOARD_SYSTEMIMAGE_PARTITION_SIZE", "")
                limit = (
                    int(raw, 0)
                    if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw)
                    else PARTITION_LIMITS["system"]
                )
                self.audit_partition_image("system", limit)
            elif rebuild_target == "bacon":
                self.source_changed = False
                for kind in ("boot", "recovery"):
                    if not self.audit_image(kind, fresh=True):
                        raise StopAutopilot(
                            f"{kind} image audit failed after bacon rebuild"
                        )
            r = self.run_built_output_audit(
                f"audit-built-output-{target}-retry.log"
            )
        if r.rc != 0 or "ACTUAL_BUILT_OUTPUT_AUDIT=PASS" not in r.text:
            self.write_unknown_error(target, r)
            raise StopAutopilot(f"actual built-output audit failed: {target}")
        self.say("ACTUAL_BUILT_OUTPUT_AUDIT=PASS")

    def ensure_vendor_if_real(self) -> None:
        if not self.detect_vendor_partition():
            self.say("VENDOR_STAGE=NOT_APPLICABLE")
            return
        if self.partition_binding_current("vendor"):
            raw = self.release_info.get("BOARD_VENDORIMAGE_PARTITION_SIZE", "")
            limit = (
                int(raw, 0)
                if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw)
                else None
            )
            self.audit_partition_image("vendor", limit)
            self.say("VENDOR_STAGE=REUSED_VALIDATED")
            return
        status = self.build_target("vendorimage")
        if status == "SKIPPED":
            return
        raw = self.release_info.get("BOARD_VENDORIMAGE_PARTITION_SIZE", "")
        limit = (
            int(raw, 0)
            if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw)
            else None
        )
        self.audit_partition_image("vendor", limit)
        self.say("VENDOR_STAGE=PASS")

    def require_boot_recovery_kernel_coherence(self) -> None:
        hashes = []
        for kind in ("boot", "recovery"):
            image = self.product_out / f"{kind}.img"
            data = image.read_bytes()
            if len(data) < 48 or data[:8] != b"ANDROID!":
                raise StopAutopilot(f"{kind} header missing before pair audit")
            kernel_size = struct.unpack_from("<I", data, 8)[0]
            page_size = struct.unpack_from("<I", data, 36)[0]
            payload = data[page_size:page_size + kernel_size]
            if kernel_size <= 0 or len(payload) != kernel_size or page_size != 2048:
                raise StopAutopilot(f"{kind} kernel bounds invalid before pair audit")
            hashes.append(hashlib.sha256(payload).hexdigest())
        if len(set(hashes)) != 1:
            raise StopAutopilot(f"boot/recovery kernel mismatch: {hashes}")
        self.say(f"BOOT_RECOVERY_KERNEL_SHA256={hashes[0]}")
        self.say("BOOT_RECOVERY_KERNEL_COHERENCE=PASS")

    def ensure_current_boot_recovery(self) -> None:
        valid = [self.audit_image(kind) for kind in ("boot", "recovery")]
        if not all(valid):
            # One Android invocation builds the kernel once and packages both
            # images. Sequential image builds can change the kernel timestamp.
            self.build_target("bootimage recoveryimage")
            for kind in ("boot", "recovery"):
                if not self.audit_image(kind, fresh=True):
                    raise StopAutopilot(f"{kind} image failed the shared-kernel pair audit")
        self.require_boot_recovery_kernel_coherence()
        self.say("BOOT_RECOVERY_SOURCE_BINDING=PASS")

    def ensure_system(self) -> None:
        self.ensure_vendor_if_real()
        raw = self.release_info.get("BOARD_SYSTEMIMAGE_PARTITION_SIZE", "")
        limit = (
            int(raw, 0)
            if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw)
            else PARTITION_LIMITS["system"]
        )
        if self.partition_binding_current("system"):
            self.audit_partition_image("system", limit)
            self.require_built_output_audit("system-reuse", "systemimage")
            self.ensure_current_boot_recovery()
            self.say("SYSTEM_STAGE=REUSED_VALIDATED")
            return
        self.build_target("systemimage")
        self.audit_partition_image("system", limit)
        self.require_built_output_audit("system", "systemimage")
        self.ensure_current_boot_recovery()
        self.say("SYSTEM_STAGE=PASS")

    def latest_rom(self) -> Path:
        zips = sorted(
            self.product_out.glob("lineage-23.2-*-UNOFFICIAL-TB8504.zip"),
            key=lambda p: p.stat().st_mtime,
        )
        if not zips:
            zips = sorted(
                self.product_out.glob("lineage-*.zip"),
                key=lambda p: p.stat().st_mtime,
            )
        if not zips:
            raise StopAutopilot("no Lineage ZIP found")
        rom = zips[-1]
        if rom.stat().st_size <= 0:
            raise StopAutopilot("final Lineage ZIP is empty")
        return rom

    def audit_final_package(self, rom: Path, min_mtime: float) -> None:
        package_dir = self.report / "final-package"
        package_dir.mkdir(exist_ok=True)
        r = self.helper(
            "audit-final-package.py",
            [
                "--root", str(self.root),
                "--rom", str(rom),
                "--min-mtime", str(min_mtime),
                "--report-dir", str(package_dir),
            ],
            "audit-final-package.log",
        )
        if r.rc != 0 or "FINAL_PACKAGE_AUDIT=PASS" not in r.text:
            self.write_unknown_error("final-package", r)
            raise StopAutopilot("final ROM package audit failed")
        sha = self.sha_file(rom)
        self.state["rom"] = {
            "path": str(rom),
            "sha256": sha,
            "size": rom.stat().st_size,
            "source_fingerprint": self.current_source_fingerprint,
            "audited": dt.datetime.now().isoformat(),
            "tooling_ref": self.tooling_ref,
        }
        self.save_state()
        self.say(f"FINAL_ROM={rom}")
        self.say(f"FINAL_ROM_SIZE={rom.stat().st_size}")
        self.say(f"FINAL_ROM_SHA256={sha}")
        self.say("FINAL_PACKAGE_AUDIT=PASS")

    def rom_binding_current(self) -> bool:
        row = self.state.get("rom", {})
        if not isinstance(row, dict):
            return False
        p = Path(str(row.get("path", "")))
        if not p.is_file() or not self.current_source_fingerprint:
            return False
        return (
            row.get("source_fingerprint") == self.current_source_fingerprint
            and row.get("sha256") == self.sha_file(p)
        )

    def full_rom(self) -> None:
        self.say("")
        self.say("=== FINAL FULL ROM BUILD ===")
        bacon_started = time.time()
        self.build_target("bacon")
        self.source_changed = False
        for kind in ("boot", "recovery"):
            if not self.audit_image(kind, fresh=True):
                raise StopAutopilot(
                    f"{kind} image audit failed after bacon build"
                )

        self.converge_sources()
        if self.source_changed:
            self.say("POSTBUILD_SOURCE_CHANGE=INVALIDATES_ARTIFACTS")
            self.build_target("bacon")
            self.source_changed = False
            for kind in ("boot", "recovery"):
                if not self.audit_image(kind, fresh=True):
                    raise StopAutopilot(
                        f"{kind} image audit failed after post-build bacon rebuild"
                    )
            self.converge_sources()
            if self.source_changed:
                raise StopAutopilot(
                    "source convergence changed files again after post-build rebuild"
                )
        self.say("FINAL_ROM_POSTBUILD_SOURCE_GATES=PASS")

        self.require_built_output_audit("bacon-postbuild", "bacon")
        self.require_boot_recovery_kernel_coherence()

        # A handler inside the built-output gate may have rebuilt bacon.
        # Select and hash the package only after every possible rebuild.
        rom = self.latest_rom()
        self.audit_final_package(rom, bacon_started)
        self.say("FULL_ROM_STAGE=PASS")

    def next_stage(self) -> None:
        self.say("")
        self.say("=== NEXT UNVALIDATED STAGE ===")
        if not self.audit_image("boot"):
            self.build_target("bootimage")
            if not self.audit_image("boot", fresh=True):
                raise StopAutopilot("next-stage boot audit failed")
            self.say("NEXT_COMPLETED=BOOT")
            return

        if not self.audit_image("recovery"):
            self.ensure_current_boot_recovery()
            self.say("NEXT_COMPLETED=RECOVERY")
            return
        self.require_boot_recovery_kernel_coherence()

        if self.detect_vendor_partition():
            raw = self.release_info.get("BOARD_VENDORIMAGE_PARTITION_SIZE", "")
            limit = (
                int(raw, 0)
                if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw)
                else None
            )
            if not self.partition_binding_current("vendor"):
                status = self.build_target("vendorimage")
                if status != "SKIPPED":
                    self.audit_partition_image("vendor", limit)
                self.say("NEXT_COMPLETED=VENDOR")
                return
            self.audit_partition_image("vendor", limit)

        raw = self.release_info.get("BOARD_SYSTEMIMAGE_PARTITION_SIZE", "")
        limit = (
            int(raw, 0)
            if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw)
            else PARTITION_LIMITS["system"]
        )
        if not self.partition_binding_current("system"):
            self.build_target("systemimage")
            self.audit_partition_image("system", limit)
            self.require_built_output_audit("next-system", "systemimage")
            self.say("NEXT_COMPLETED=SYSTEM")
            return

        self.audit_partition_image("system", limit)
        self.require_built_output_audit("next-system-reuse", "systemimage")

        if self.rom_binding_current():
            rom = Path(str(self.state["rom"]["path"]))
            self.audit_final_package(rom, 0.0)
            self.say("NEXT_COMPLETED=ALL_STAGES_ALREADY_VALIDATED")
            return

        self.full_rom()
        self.say("NEXT_COMPLETED=ROM")

    def write_unknown_error(self, target: str, result: CommandResult) -> None:
        d = self.report / "unknown-error"
        d.mkdir(exist_ok=True)
        (d / "target.txt").write_text(
            f"TARGET={target}\nRC={result.rc}\nCOMMAND={result.command}\n",
            encoding="utf-8",
        )
        tail = "\n".join(result.text.splitlines()[-500:])
        (d / "last-500-lines.log").write_text(tail + "\n", encoding="utf-8")
        self.snapshot_sources("unknown-error")
        try:
            release = "\n".join(f"{k}={v}" for k, v in self.release_info.items())
            (d / "release.txt").write_text(release + "\n", encoding="utf-8")
        except Exception:
            pass
        (d / "instruction.txt").write_text(
            "UNKNOWN_ERROR=YES\n"
            "AUTOMATIC_MUTATION=REFUSED\n"
            "Provide this report bundle for a new audited handler before retrying.\n",
            encoding="utf-8",
        )

    def archive_report(self) -> Path:
        self.full_log.flush()
        zip_path = Path(str(self.report) + ".zip")
        base = str(zip_path.with_suffix(""))
        shutil.make_archive(base, "zip", root_dir=self.report)
        return zip_path

    def finish(self, success: bool, message: str) -> None:
        self.status["finished"] = dt.datetime.now().isoformat()
        self.status["success"] = success
        self.status["message"] = message
        self.status["source_changed"] = self.source_changed
        self.status["handler_uses"] = self.handler_uses
        (self.report / "STATUS.json").write_text(
            json.dumps(self.status, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            self.snapshot_sources("final")
        except Exception as exc:
            self.say(f"FINAL_SNAPSHOT_WARNING={type(exc).__name__}: {exc}")
        self.full_log.flush()
        try:
            archive = self.archive_report()
        except Exception as exc:
            archive = Path("")
            self.say(f"REPORT_ARCHIVE_WARNING={type(exc).__name__}: {exc}")
        self.say(f"AUTOPILOT_STATUS={'PASS' if success else 'BLOCKED'}")
        self.say("NO_FLASH=YES")
        self.say(f"REPORT={self.report}")
        if str(archive):
            self.say(f"REPORT_ZIP={archive}")

    def execute(self) -> None:
        self.preflight()
        self.converge_sources()
        self.release_gate()

        # refresh_module_metadata() owns the converge-side cloud-seed v2
        # export and the build-goal module-info refresh. Call it for every goal
        # so the documented single-entry pipeline cannot skip either contract.
        self.refresh_module_metadata()

        if self.goal == "converge":
            return
        if self.goal == "boot":
            self.ensure_image("boot")
            return
        if self.goal == "recovery":
            self.ensure_current_boot_recovery()
            return
        if self.goal == "next":
            self.next_stage()
            return
        if self.goal == "system":
            self.ensure_current_boot_recovery()
            self.ensure_system()
            return
        if self.goal == "rom":
            self.ensure_current_boot_recovery()
            self.ensure_system()
            self.full_rom()
            return
        raise StopAutopilot(f"unsupported goal: {self.goal}")

def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"

def static_self_test() -> None:
    for platform, sdk, expected in (
        ("16", "36", True),
        ("Baklava", "36", True),
        ("15", "36", False),
        ("17", "36", False),
        ("VanillaIceCream", "36", False),
        ("16.1", "36", False),
        ("16", "35", False),
        ("Baklava", "35", False),
        ("", "36", False),
    ):
        if is_android16_platform_version(platform, sdk) != expected:
            raise RuntimeError(
                f"Android 16 release identity self-test failed: "
                f"platform={platform!r}, sdk={sdk!r}, expected={expected}"
            )
    repo_root = Path(__file__).resolve().parents[2]
    knowledge_file = repo_root / KNOWLEDGE_PATH
    if not knowledge_file.is_file():
        raise RuntimeError(f"knowledge base missing: {knowledge_file}")
    knowledge = json.loads(knowledge_file.read_text("utf-8"))
    if knowledge.get("schema_version") != 1:
        raise RuntimeError("unexpected knowledge schema")
    safety = knowledge.get("safety_contract", {})
    for key in (
        "no_adb", "no_fastboot", "no_flash", "no_device_block_writes",
        "no_clean", "no_installclean", "no_clobber",
        "no_private_signing_key_export",
    ):
        if safety.get(key) is not True:
            raise RuntimeError(f"safety contract missing/false: {key}")
    recovery = knowledge.get("successful_artifacts", {}).get("recovery_current", {})
    if recovery.get("sha256") != KNOWN_RECOVERY_SHA256:
        raise RuntimeError("recovery knowledge SHA mismatch")
    if recovery.get("size") != KNOWN_RECOVERY_SIZE:
        raise RuntimeError("recovery knowledge size mismatch")
    if recovery.get("kernel_sha256") != KNOWN_RECOVERY_KERNEL_SHA256:
        raise RuntimeError("recovery kernel knowledge SHA mismatch")
    source = knowledge.get("source_baselines", {})
    if source.get("device", {}).get("sha") != EXPECTED_HEADS["device/lenovo/TB8504"]:
        raise RuntimeError("device baseline mismatch")
    if source.get("vendor", {}).get("sha") != EXPECTED_HEADS["vendor/lenovo/TB8504"]:
        raise RuntimeError("vendor baseline mismatch")
    if source.get("kernel", {}).get("sha") != EXPECTED_HEADS["kernel/lenovo/msm8917"]:
        raise RuntimeError("kernel baseline mismatch")
    integrity = knowledge.get("source_integrity_contracts", {})
    static = integrity.get("static_target_repos", {})
    expected_static = {
        "hardware/qcom-caf/msm8996/audio",
        "hardware/qcom-caf/msm8996/media",
        "hardware/qcom-caf/msm8996/display",
        "hardware/lineage/compat",
        "device/qcom/sepolicy-legacy-um",
        "external/XMP-Toolkit-SDK",
        "external/google-highway",
        "external/skia",
    }
    if set(static) != expected_static:
        raise RuntimeError(
            f"static source-integrity repo set mismatch: {sorted(static)}"
        )
    for rel, spec in static.items():
        if not re.fullmatch(r"[0-9a-f]{40}", str(spec.get("head", ""))):
            raise RuntimeError(f"invalid source-integrity HEAD: {rel}")
        if not re.fullmatch(
            r"[0-9a-f]{64}", str(spec.get("patch_sha256", ""))
        ):
            raise RuntimeError(f"invalid source-integrity patch hash: {rel}")

    dynamic = integrity.get("dynamic_primary_repos", {})
    if set(dynamic) != {"device/lenovo/TB8504", "vendor/lenovo/TB8504"}:
        raise RuntimeError(
            f"dynamic primary repo contract mismatch: {sorted(dynamic)}"
        )
    workspace = integrity.get("workspace_repo_contract", {})
    if not isinstance(workspace, dict):
        raise RuntimeError("workspace repo contract missing")
    if "revision_fingerprint" not in workspace:
        raise RuntimeError("workspace revision fingerprint field missing")
    if "expected_project_count" not in workspace:
        raise RuntimeError("workspace project-count field missing")
    if (
        integrity.get("required_unproven_repo")
        != "hardware/qcom-caf/msm8996/gps"
    ):
        raise RuntimeError("unexpected GPS provenance target")
    host = integrity.get("host_only_repo", {})
    if host.get("path") != "prebuilts/rust":
        raise RuntimeError("host-only Rust contract missing")
    if host.get("allowed_dirty_prefix") != "windows-x86/":
        raise RuntimeError("host-only Rust dirty scope mismatch")

    supported = set(knowledge.get("autopilot_policy", {}).get("supported_goals", []))
    expected_goals = {"converge","boot","recovery","next","system","rom"}
    if supported != expected_goals:
        raise RuntimeError(f"supported-goal mismatch: {supported}")
    for name in HELPERS:
        if not (repo_root / "tb8504-build/scripts" / name).is_file():
            raise RuntimeError(f"helper missing from repository: {name}")
    for name in AUXILIARY_TOOLS:
        if not (repo_root / "tb8504-build/scripts" / name).is_file():
            raise RuntimeError(f"auxiliary tool missing from repository: {name}")

    for legacy_name in (
        "local-pre-recovery.sh",
        "local-recovery-stage.sh",
        "local-stage-image.sh",
        "local-finalize-android16.sh",
    ):
        legacy_path = repo_root / "tb8504-build/scripts" / legacy_name
        if not legacy_path.is_file():
            raise RuntimeError(f"legacy support runner missing: {legacy_name}")
        legacy_text = legacy_path.read_text("utf-8", errors="replace")
        if "LEGACY_RUNNER_LOCKED=YES" not in legacy_text:
            raise RuntimeError(f"legacy runner is not locked: {legacy_name}")

    source_text = Path(__file__).read_text("utf-8", errors="replace")
    required_guards = (
        "FORBIDDEN_COMMAND_PATTERNS",
        "self.safety_check_command(printable)",
        "UNKNOWN_BUILD_ERROR",
        "STOP_WITH_DIAGNOSTIC_BUNDLE",
        "did not refresh stale",
        "STATIC_SOURCE_PROVENANCE_CAPTURE=PASS",
        "_ARTIFACT_REFRESH=PASS",
        "_STALE_ARTIFACT_FORCE_REBUILD=PASS",
        "--upload-seed",
        "CLOUD_SEED_V2_HANDOFF=PASS",
        "SEED_HANDOFF_PREFLIGHT=PASS",
        "TB8504_PROVENANCE_DIR",
        "TOOLING_CI_GATE=PASS",
        "TB8504 cloud lane lint",
    )
    for guard in required_guards:
        if guard not in source_text and guard not in json.dumps(knowledge):
            raise RuntimeError(f"required autopilot guard missing: {guard}")

    built_output_text = (
        repo_root / "tb8504-build/scripts/audit-built-output.py"
    ).read_text("utf-8", errors="replace")
    for guard in (
        "ACTUAL_VENDOR_ELF_SCOPE=ALL_INSTALLED_VENDOR_ELFS",
        'for p in vendor_root.rglob("*")',
        "ACTUAL_OUTPUT_UNRESOLVED_EDGES",
        "ACTUAL_OUTPUT_WRONG_BITNESS_EDGES",
        'EXPECTED_FIRST_API_LEVEL = "25"',
        "CHECKVINTF_KERNEL_REQUIREMENTS=ENFORCED",
        "A/B or dynamic-partition payload found in legacy non-A/B",
    ):
        if guard not in built_output_text:
            raise RuntimeError(
                f"required installed-output audit guard missing: {guard}"
            )

    local_image_text = (
        repo_root / "tb8504-build/scripts/audit-local-image.py"
    ).read_text("utf-8", errors="replace")
    for guard in (
        "FORBIDDEN_CMDLINE_TOKENS",
        "BOOT_CMDLINE_SELINUX_ENFORCEMENT=PASS",
        "androidboot.selinux=permissive",
        "enforcing=0",
    ):
        if guard not in local_image_text:
            raise RuntimeError(
                f"required boot-image SELinux guard missing: {guard}"
            )

    exporter_text = (
        repo_root / "tb8504-build/scripts/export-cloud-seed.sh"
    ).read_text("utf-8", errors="replace")
    for guard in (
        "STAGE8N_HANDOFF_PROVENANCE=PASS",
        '"parents":[parent]',
        'ACTUAL_PARENTS',
        'request parent mismatch before ref update',
        'force=false',
        'TOOLING_REF=$TOOLING_REF',
        "workspace-source-state.json",
        "static-source-state.json",
        "gps-source-state.json",
        "primary-source-state.json",
        "PROVENANCE_CAPTURED=YES",
        "untracked symlink unsupported by canonical seed",
        "hardware/qcom-caf/msm8996/gps",
    ):
        if guard not in exporter_text:
            raise RuntimeError(
                f"required seed-handoff provenance guard missing: {guard}"
            )

    provenance_helper = (
        repo_root / "tb8504-build/scripts/accept-converged-provenance.py"
    )
    if not provenance_helper.is_file():
        raise RuntimeError("provenance acceptance helper missing")
    provenance_text = provenance_helper.read_text("utf-8", errors="replace")
    for guard in (
        "PROVENANCE_ACCEPTANCE_CANDIDATE=PASS",
        "PROVENANCE_ACCEPT_STATIC_BINDING=PASS",
        "PROVENANCE_ACCEPTED_AFTER_STAGE8N",
        "workspace_revision_fingerprint",
        "verify_untracked_seed",
        "untracked seed coverage mismatch",
        "bundled GNSS source incomplete in device tar",
    ):
        if guard not in provenance_text:
            raise RuntimeError(
                f"required provenance acceptance guard missing: {guard}"
            )

    stage8n_workflow = (
        repo_root / ".github/workflows/tb8504-stage8n.yml"
    ).read_text("utf-8", errors="replace")
    for guard in (
        "SEED_TOOLING_REF=$EXPECTED_TOOLING_REF",
        "DEVICE_RECONSTRUCTED_FROM_PROVENANCE=PASS",
        "DEVICE_STATUS_PROVENANCE_BINDING=PASS",
        "DEVICE_TREE_METADATA_BINDING=PASS",
        "DEVICE_TAR_PROVENANCE_BINDING=PASS",
        "VENDOR_RECONSTRUCTED_FROM_PROVENANCE=PASS",
        "VENDOR_STATUS_PROVENANCE_BINDING=PASS",
        "accept-converged-provenance.py",
        "PROVENANCE_KNOWLEDGE_PROMOTION=PASS",
    ):
        if guard not in stage8n_workflow:
            raise RuntimeError(
                f"required STAGE8N provenance gate missing: {guard}"
            )

    final_package_text = (
        repo_root / "tb8504-build/scripts/audit-final-package.py"
    ).read_text("utf-8", errors="replace")
    for guard in (
        "A/B or dynamic-partition payload forbidden on legacy non-A/B",
        '{"payload.bin","super.img"}',
        "expected exactly one boot.img",
    ):
        if guard not in final_package_text:
            raise RuntimeError(
                f"required final-package audit guard missing: {guard}"
            )
    print("AUTOPILOT_SELF_TEST=PASS")
    print(f"KNOWLEDGE_PROBLEMS={len(knowledge.get('known_build_failures_and_fixes', []))}")
    print(f"RECOVERY_BASELINE_SHA256={KNOWN_RECOVERY_SHA256}")
    print("NO_FLASH=YES")

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="TB8504 deterministic staged self-healing Android 16 autopilot"
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=Path("/home/dre/android16-tb8504/lineage-23.2"),
    )
    ap.add_argument(
        "--goal",
        choices=("converge", "boot", "recovery", "next", "system", "rom"),
        default="next",
        help=(
            "converge=source gates only; boot/recovery=through that image; "
            "next=one next unvalidated stage; system=through system image; "
            "rom=full bacon only after all staged gates"
        ),
    )
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument(
        "--upload-seed",
        action="store_true",
        help=(
            "with --goal converge, upload the audited v2 seed to a private "
            "GitHub draft release and commit the STAGE8N request; never builds "
            "or touches the tablet"
        ),
    )
    ap.add_argument("--show-knowledge", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    return ap.parse_args()

def main() -> int:
    args = parse_args()
    if args.self_test:
        try:
            static_self_test()
            return 0
        except Exception as exc:
            print(
                f"AUTOPILOT_SELF_TEST=FAIL: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 2
    if args.max_attempts < 1 or args.max_attempts > 5:
        print("max-attempts must be in 1..5", file=sys.stderr)
        return 2
    if args.upload_seed and args.goal != "converge":
        print("--upload-seed is valid only with --goal converge", file=sys.stderr)
        return 2

    auto = Autopilot(
        args.root,
        args.goal,
        args.max_attempts,
        upload_seed=args.upload_seed,
    )
    try:
        if args.show_knowledge:
            auto.preflight()
            print(json.dumps(auto.knowledge, indent=2, sort_keys=True))
            auto.finish(True, "knowledge displayed")
            return 0

        auto.execute()
        auto.finish(True, f"goal {auto.goal} completed")
        return 0
    except KeyboardInterrupt:
        auto.status["interrupted"] = True
        auto.finish(False, "user interrupted")
        return 130
    except StopAutopilot as exc:
        auto.say(f"BLOCKER={exc}")
        auto.finish(False, str(exc))
        return 2
    except Exception as exc:
        auto.say(f"INTERNAL_AUTOPILOT_ERROR={type(exc).__name__}: {exc}")
        auto.finish(False, f"internal error: {type(exc).__name__}: {exc}")
        return 3
    finally:
        try:
            auto.full_log.close()
        except Exception:
            pass

if __name__ == "__main__":
    raise SystemExit(main())
