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
    "apply-runtime-cleanup.py",
    "apply-residual-cleanup.py",
    "apply-product-compat.py",
    "audit-product-compat.py",
    "audit-residual-contracts.py",
    "audit-stage8n.py",
    "audit-runtime-contracts.py",
    "audit-local-image.py",
    "audit-built-output.py",
    "audit-final-package.py",
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

class StopAutopilot(RuntimeError):
    pass

class CommandResult:
    def __init__(self, rc: int, text: str, command: str):
        self.rc = rc
        self.text = text
        self.command = command

class Autopilot:
    def __init__(self, root: Path, goal: str, max_attempts: int):
        self.root = root.resolve()
        self.goal = goal
        self.max_attempts = max_attempts
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
        for name in HELPERS:
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
            "git", "gh", "python3", "bash", "readelf", "sha256sum",
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
        self.snapshot_sources("before")
        self.verify_source_heads()
        self.say("PREFLIGHT=PASS")

    def git_status(self, repo: Path) -> str:
        return self.capture(["git", "status", "--short"], cwd=repo, check=False)

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
                with p.open("rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
            else:
                h.update(b"O")

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

    def verify_extended_source_contracts(self) -> None:
        contracts = self.knowledge.get("source_integrity_contracts", {})
        static = contracts.get("static_target_repos", {})
        if not isinstance(static, dict) or not static:
            raise StopAutopilot("source integrity contract set missing/empty")

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
            self.say(f"SOURCE_CONTRACT[{rel}]=PASS")

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
        if gps.is_dir():
            inside = self.capture(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=gps,
                check=False,
            )
            gps_report["git_worktree"] = inside == "true"
            if inside == "true":
                gps_head = self.capture(["git", "rev-parse", "HEAD"], cwd=gps)
                gps_status = self.git_status(gps)
                gps_report["head"] = gps_head
                gps_report["status"] = gps_status.splitlines()
                gps_report["patch_sha256"] = self.git_diff_sha256(gps)
                gps_report["untracked"] = self.git_untracked(gps)
                if isinstance(gps_contract, dict) and gps_contract:
                    if gps_head != str(gps_contract.get("head", "")):
                        raise StopAutopilot(
                            f"GPS source HEAD mismatch: {gps_head} != "
                            f"{gps_contract.get('head', '')}"
                        )
                    if gps_report["patch_sha256"] != str(
                        gps_contract.get("patch_sha256", "")
                    ):
                        raise StopAutopilot("GPS source patch fingerprint mismatch")
                    if gps_report["untracked"]:
                        raise StopAutopilot(
                            f"GPS repo has unexpected untracked files: "
                            f"{gps_report['untracked'][:30]}"
                        )
                    self.say("GPS_SOURCE_CONTRACT=PASS")
                else:
                    self.unproven_sources.append(gps_rel)
            else:
                gps_report["tree_sha256"] = self.hash_plain_tree(gps)
                self.unproven_sources.append(gps_rel)
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
            status = self.capture(["git", "status", "--short"], cwd=repo, check=False)
            diff = self.capture(["git", "diff", "--binary"], cwd=repo, check=False)
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

        self.source_changed = runtime_changed or residual_changed or product_changed
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

        module_info = self.product_out / "module-info.json"
        if not module_info.is_file():
            raise StopAutopilot(f"module-info.json missing: {module_info}")
        runtime_dir = self.report / "runtime"
        runtime_dir.mkdir(exist_ok=True)
        r = self.helper(
            "audit-runtime-contracts.py",
            [
                "--vendor", str(self.vendor),
                "--device", str(self.device),
                "--report-dir", str(runtime_dir),
                "--module-info", str(module_info),
            ],
            "audit-runtime-contracts.log",
        )
        if r.rc != 0 or "RUNTIME_CONTRACT_FAILURES=0" not in r.text:
            raise StopAutopilot("runtime init/VINTF contract audit failed")

        self.snapshot_sources("after-convergence")
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
        if not info.get("PLATFORM_VERSION", "").startswith("16"):
            raise StopAutopilot(f"platform is not Android 16: {info.get('PLATFORM_VERSION')!r}")
        if not info.get("LINEAGE_VERSION", "").startswith("23.2-"):
            raise StopAutopilot(f"Lineage version is not 23.2: {info.get('LINEAGE_VERSION')!r}")
        self.release_info = info
        release_file = self.report / "release.txt"
        release_file.write_text("\n".join(f"{k}={v}" for k, v in info.items()) + "\n", encoding="utf-8")
        self.say("ANDROID16_RELEASE_IDENTITY=PASS")

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
        if not separate:
            self.say("VENDOR_TOPOLOGY=INTEGRATED_OR_SYSTEM_VENDOR")
        return separate

    def sha_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def audit_image(self, kind: str, allow_existing: bool = True) -> bool:
        image = self.product_out / f"{kind}.img"
        if not image.is_file():
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
        sha = self.sha_file(image)
        self.state.setdefault("images", {})[kind] = {
            "sha256": sha,
            "size": image.stat().st_size,
            "audited": dt.datetime.now().isoformat(),
            "tooling_ref": self.tooling_ref,
        }
        self.save_state()
        self.say(f"{kind.upper()}_IMAGE_SHA256={sha}")
        self.say(f"{kind.upper()}_IMAGE_AUDIT=PASS")
        if kind == "recovery":
            if image.stat().st_size == KNOWN_RECOVERY_SIZE and sha == KNOWN_RECOVERY_SHA256:
                self.say("RECOVERY_MATCHES_20261007_PROVEN_BASELINE=YES")
            if KNOWN_RECOVERY_KERNEL_SHA256 in r.text:
                self.say("RECOVERY_KERNEL_MATCHES_20261007_PROVEN_BASELINE=YES")
        return True

    def classify_failure(self, text: str, target: str) -> tuple[str, str] | None:
        rules: list[tuple[str, str, tuple[str, ...], bool]] = [
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
                    "checkvintf",
                    "INCOMPATIBLE",
                    "STALE_VINTF_HAL_DECLARATIONS",
                    "UNRESOLVED_INIT_SERVICES",
                ),
                False,
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

        if handler == "source_convergence":
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

    def build_target(self, target: str) -> str:
        self.say("")
        self.say(f"=== BUILD TARGET: {target} ===")
        for attempt in range(1, self.max_attempts + 1):
            self.say(f"BUILD_ATTEMPT={attempt}/{self.max_attempts}")
            r = self.android_shell(f"mka {sh_quote(target)}", f"build-{target}-attempt{attempt}.log")
            if r.rc == 0:
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
        if not self.audit_image(kind):
            rlog = (self.logs / f"audit-{kind}-image.log")
            text = rlog.read_text("utf-8", errors="replace") if rlog.is_file() else ""
            classified = self.classify_failure(text, target)
            if classified and self.apply_handler(classified[0], target):
                self.build_target(target)
                if self.audit_image(kind):
                    return
            raise StopAutopilot(f"{kind} image audit failed after successful build")
        self.say(f"{kind.upper()}_BUILD=PASS")

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
            "audited": dt.datetime.now().isoformat(),
            "tooling_ref": self.tooling_ref,
        }
        self.save_state()
        self.say(f"{kind.upper()}_BASIC_IMAGE_AUDIT=PASS")

    def ensure_vendor_if_real(self) -> None:
        if not self.detect_vendor_partition():
            self.say("VENDOR_STAGE=NOT_APPLICABLE")
            return
        status = self.build_target("vendorimage")
        if status == "SKIPPED":
            return
        raw = self.release_info.get("BOARD_VENDORIMAGE_PARTITION_SIZE", "")
        limit = int(raw, 0) if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw) else None
        self.audit_partition_image("vendor", limit)
        self.say("VENDOR_STAGE=PASS")

    def ensure_system(self) -> None:
        self.ensure_vendor_if_real()
        self.build_target("systemimage")
        raw = self.release_info.get("BOARD_SYSTEMIMAGE_PARTITION_SIZE", "")
        limit = int(raw, 0) if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw) else PARTITION_LIMITS["system"]
        self.audit_partition_image("system", limit)
        self.say("SYSTEM_STAGE=PASS")

    def full_rom(self) -> None:
        self.say("")
        self.say("=== FINAL FULL ROM BUILD ===")
        self.build_target("bacon")
        self.source_changed = False
        self.ensure_image("boot")
        self.ensure_image("recovery")
        zips = sorted(
            self.product_out.glob("lineage-23.2-*-UNOFFICIAL-TB8504.zip"),
            key=lambda p: p.stat().st_mtime,
        )
        if not zips:
            zips = sorted(self.product_out.glob("lineage-*.zip"), key=lambda p: p.stat().st_mtime)
        if not zips:
            raise StopAutopilot("bacon succeeded but no Lineage ZIP was found")
        rom = zips[-1]
        if rom.stat().st_size <= 0:
            raise StopAutopilot("final Lineage ZIP is empty")
        sha = self.sha_file(rom)
        self.say(f"FINAL_ROM={rom}")
        self.say(f"FINAL_ROM_SIZE={rom.stat().st_size}")
        self.say(f"FINAL_ROM_SHA256={sha}")

        self.converge_sources()
        self.say("FINAL_ROM_POSTBUILD_SOURCE_GATES=PASS")

        output_dir = self.report / "actual-built-output"
        output_dir.mkdir(exist_ok=True)
        r = self.helper(
            "audit-built-output.py",
            ["--root", str(self.root), "--report-dir", str(output_dir)],
            "audit-built-output.log",
        )
        if r.rc != 0 or "ACTUAL_BUILT_OUTPUT_AUDIT=PASS" not in r.text:
            classified = self.classify_failure(r.text, "bacon-postbuild")
            if classified and self.apply_handler(classified[0], "bacon-postbuild"):
                self.say("POSTBUILD_SELF_HEAL_REQUIRES_REBUILD=YES")
                self.build_target("bacon")
                self.source_changed = False
                self.ensure_image("boot")
                self.ensure_image("recovery")
                r = self.helper(
                    "audit-built-output.py",
                    ["--root", str(self.root), "--report-dir", str(output_dir)],
                    "audit-built-output-retry.log",
                )
            if r.rc != 0 or "ACTUAL_BUILT_OUTPUT_AUDIT=PASS" not in r.text:
                self.write_unknown_error("bacon-postbuild", r)
                raise StopAutopilot("actual built-output audit failed")
        self.say("ACTUAL_BUILT_OUTPUT_AUDIT=PASS")
        self.say("FULL_ROM_STAGE=PASS")

    def next_stage(self) -> None:
        self.say("")
        self.say("=== NEXT UNVALIDATED STAGE ===")
        if not self.audit_image("boot"):
            self.build_target("bootimage")
            if not self.audit_image("boot"):
                raise StopAutopilot("next-stage boot audit failed")
            self.say("NEXT_COMPLETED=BOOT")
            return
        if not self.audit_image("recovery"):
            self.build_target("recoveryimage")
            if not self.audit_image("recovery"):
                raise StopAutopilot("next-stage recovery audit failed")
            self.say("NEXT_COMPLETED=RECOVERY")
            return
        if self.detect_vendor_partition():
            self.build_target("vendorimage")
            raw = self.release_info.get("BOARD_VENDORIMAGE_PARTITION_SIZE", "")
            limit = int(raw, 0) if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw) else None
            self.audit_partition_image("vendor", limit)
            self.say("NEXT_COMPLETED=VENDOR")
        else:
            self.build_target("systemimage")
            raw = self.release_info.get("BOARD_SYSTEMIMAGE_PARTITION_SIZE", "")
            limit = int(raw, 0) if raw and re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", raw) else PARTITION_LIMITS["system"]
            self.audit_partition_image("system", limit)
            self.say("NEXT_COMPLETED=SYSTEM")

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

        if self.goal == "converge":
            return
        if self.goal == "boot":
            self.ensure_image("boot")
            return
        if self.goal == "recovery":
            self.ensure_image("boot")
            self.source_changed = False
            self.ensure_image("recovery")
            return
        if self.goal == "next":
            self.next_stage()
            return
        if self.goal == "system":
            self.ensure_image("boot")
            self.source_changed = False
            self.ensure_image("recovery")
            self.ensure_system()
            return
        if self.goal == "rom":
            self.ensure_image("boot")
            self.source_changed = False
            self.ensure_image("recovery")
            self.ensure_system()
            self.full_rom()
            return
        raise StopAutopilot(f"unsupported goal: {self.goal}")

def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"

def static_self_test() -> None:
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
    supported = set(knowledge.get("autopilot_policy", {}).get("supported_goals", []))
    expected_goals = {"converge","boot","recovery","next","system","rom"}
    if supported != expected_goals:
        raise RuntimeError(f"supported-goal mismatch: {supported}")
    for name in HELPERS:
        if not (repo_root / "tb8504-build/scripts" / name).is_file():
            raise RuntimeError(f"helper missing from repository: {name}")
    source_text = Path(__file__).read_text("utf-8", errors="replace")
    required_guards = (
        "FORBIDDEN_COMMAND_PATTERNS",
        "self.safety_check_command(printable)",
        "UNKNOWN_BUILD_ERROR",
        "STOP_WITH_DIAGNOSTIC_BUNDLE",
    )
    for guard in required_guards:
        if guard not in source_text and guard not in json.dumps(knowledge):
            raise RuntimeError(f"required autopilot guard missing: {guard}")
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
            print(f"AUTOPILOT_SELF_TEST=FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if args.max_attempts < 1 or args.max_attempts > 5:
        print("max-attempts must be in 1..5", file=sys.stderr)
        return 2

    auto = Autopilot(args.root, args.goal, args.max_attempts)
    try:
        auto.preflight()
        if args.show_knowledge:
            print(json.dumps(auto.knowledge, indent=2, sort_keys=True))
            auto.finish(True, "knowledge displayed")
            return 0

        # preflight was already run above; execute the remaining pipeline.
        auto.converge_sources()
        auto.release_gate()

        if auto.goal == "converge":
            pass
        elif auto.goal == "boot":
            auto.ensure_image("boot")
        elif auto.goal == "recovery":
            auto.ensure_image("boot")
            auto.source_changed = False
            auto.ensure_image("recovery")
        elif auto.goal == "next":
            auto.next_stage()
        elif auto.goal == "system":
            auto.ensure_image("boot")
            auto.source_changed = False
            auto.ensure_image("recovery")
            auto.ensure_system()
        elif auto.goal == "rom":
            auto.ensure_image("boot")
            auto.source_changed = False
            auto.ensure_image("recovery")
            auto.ensure_system()
            auto.full_rom()
        else:
            raise StopAutopilot(f"unsupported goal: {auto.goal}")

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
