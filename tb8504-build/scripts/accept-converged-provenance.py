#!/usr/bin/env python3
"""Promote audited TB8504 converge provenance into the autopilot knowledge base.

This tool does not inspect or touch a device. It consumes only an already
hash-verified cloud seed after the STAGE8N source/runtime gates have passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

GPS_PATH = "hardware/qcom-caf/msm8996/gps"
PRIMARY = ("device/lenovo/TB8504", "vendor/lenovo/TB8504")


def die(msg: str) -> None:
    raise SystemExit(f"PROVENANCE_ACCEPT_FAIL={msg}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json_member(z: zipfile.ZipFile, name: str) -> dict:
    try:
        obj = json.loads(z.read(name).decode("utf-8"))
    except Exception as exc:
        die(f"cannot read {name}: {exc}")
    if not isinstance(obj, dict):
        die(f"{name} is not a JSON object")
    return obj


def recompute_workspace(workspace: dict) -> str:
    projects = workspace.get("projects")
    manifests = str(workspace.get("local_manifests_sha256", ""))
    if not isinstance(projects, list) or not projects:
        die("workspace project inventory missing")
    if workspace.get("project_count") != len(projects):
        die("workspace project count mismatch")
    if workspace.get("unexpected_dirty_repos") != []:
        die("workspace has unexpected dirty repos")
    if not re.fullmatch(r"[0-9a-f]{64}", manifests):
        die("local manifests fingerprint invalid")

    seen: set[str] = set()
    h = hashlib.sha256()
    for row in sorted(projects, key=lambda x: str(x.get("path", ""))):
        if not isinstance(row, dict):
            die("workspace project row invalid")
        rel = str(row.get("path", ""))
        head = str(row.get("head", ""))
        if not rel or rel in seen:
            die(f"workspace project path invalid/duplicate: {rel!r}")
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            die(f"workspace project HEAD invalid: {rel}")
        seen.add(rel)
        h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        h.update(head.encode("ascii") + b"\0")
    h.update(b"LOCAL_MANIFESTS\0" + manifests.encode("ascii") + b"\0")
    return h.hexdigest()


def patch_member(rel: str) -> str:
    return "patches/" + rel.replace("/", "__") + ".patch"


def verify_untracked_seed(
    z: zipfile.ZipFile, repo: str, provenance: dict
) -> None:
    rows = provenance.get("untracked")
    recorded = str(provenance.get("untracked_manifest_sha256", ""))
    if not isinstance(rows, list):
        die(f"untracked provenance list missing: {repo}")
    if not re.fullmatch(r"[0-9a-f]{64}", recorded):
        die(f"untracked provenance fingerprint invalid: {repo}")

    names = set(z.namelist())
    prefix = f"untracked/{repo}/"
    expected: set[str] = set()
    seen: set[str] = set()
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda x: str(x.get("path", "")) if isinstance(x, dict) else ""):
        if not isinstance(row, dict):
            die(f"untracked provenance row invalid: {repo}")
        rel = str(row.get("path", ""))
        parts = Path(rel).parts
        if not rel or rel in seen or Path(rel).is_absolute() or ".." in parts:
            die(f"untracked provenance path invalid/duplicate: {repo}:{rel!r}")
        seen.add(rel)
        h.update(rel.encode("utf-8", "surrogateescape") + b"\0")

        kind = str(row.get("type", ""))
        if kind != "file":
            die(f"unsupported untracked provenance type: {repo}:{rel}:{kind}")
        size = row.get("size")
        digest = str(row.get("sha256", ""))
        if not isinstance(size, int) or size < 0:
            die(f"untracked file size invalid: {repo}:{rel}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            die(f"untracked file digest invalid: {repo}:{rel}")

        member = prefix + rel
        if member not in names:
            die(f"untracked file missing from seed: {repo}:{rel}")
        data = z.read(member)
        if len(data) != size or sha256_bytes(data) != digest:
            die(f"untracked file content mismatch: {repo}:{rel}")
        expected.add(member)
        h.update(
            b"F\0"
            + str(size).encode("ascii")
            + b"\0"
            + digest.encode("ascii")
        )

    actual = {
        name for name in names
        if name.startswith(prefix) and not name.endswith("/")
    }
    if actual != expected:
        die(
            f"untracked seed coverage mismatch {repo}: "
            f"missing={sorted(expected-actual)[:20]} "
            f"extra={sorted(actual-expected)[:20]}"
        )
    if h.hexdigest() != recorded:
        die(f"untracked manifest fingerprint mismatch: {repo}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True, type=Path)
    ap.add_argument("--knowledge", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--release-tag", required=True)
    ap.add_argument("--request-commit", required=True)
    ap.add_argument("--tooling-ref", required=True)
    args = ap.parse_args()

    if not re.fullmatch(r"[0-9a-f]{40}", args.request_commit):
        die("request commit invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", args.tooling_ref):
        die("tooling ref invalid")
    if not args.seed.is_file() or not args.knowledge.is_file():
        die("seed or knowledge file missing")

    try:
        knowledge = json.loads(args.knowledge.read_text("utf-8"))
    except Exception as exc:
        die(f"knowledge JSON invalid: {exc}")
    if not isinstance(knowledge, dict):
        die("knowledge root is not an object")

    seed_sha = hashlib.sha256(args.seed.read_bytes()).hexdigest()
    with zipfile.ZipFile(args.seed, "r") as z:
        bad = z.testzip()
        if bad:
            die(f"seed ZIP CRC failure: {bad}")

        export = z.read("meta/EXPORT.txt").decode("utf-8", "replace")
        tooling = [
            x.split("=", 1)[1].strip()
            for x in export.splitlines()
            if x.startswith("TOOLING_REF=")
        ]
        if tooling != [args.tooling_ref]:
            die(f"seed tooling ref mismatch: {tooling} != {args.tooling_ref}")

        workspace = load_json_member(z, "meta/workspace-source-state.json")
        gps = load_json_member(z, "meta/gps-source-state.json")
        primary = load_json_member(z, "meta/primary-source-state.json")

        recomputed = recompute_workspace(workspace)
        recorded = str(workspace.get("revision_fingerprint", ""))
        if recomputed != recorded:
            die("workspace fingerprint does not recompute")

        contracts = knowledge.get("source_integrity_contracts")
        if not isinstance(contracts, dict):
            die("source integrity contracts missing")
        dynamic = contracts.get("dynamic_primary_repos")
        if not isinstance(dynamic, dict) or set(dynamic) != set(PRIMARY):
            die("dynamic primary contract set mismatch")

        for rel in PRIMARY:
            row = primary.get(rel)
            spec = dynamic.get(rel)
            if not isinstance(row, dict) or not isinstance(spec, dict):
                die(f"primary provenance missing: {rel}")
            head = str(row.get("head", ""))
            patch_sha = str(row.get("patch_sha256", ""))
            untracked_sha = str(row.get("untracked_manifest_sha256", ""))
            if head != str(spec.get("head", "")):
                die(f"primary HEAD differs from approved baseline: {rel}")
            if not re.fullmatch(r"[0-9a-f]{64}", patch_sha):
                die(f"primary patch fingerprint invalid: {rel}")
            if not re.fullmatch(r"[0-9a-f]{64}", untracked_sha):
                die(f"primary untracked fingerprint invalid: {rel}")
            member = patch_member(rel)
            try:
                actual_patch_sha = sha256_bytes(z.read(member))
            except KeyError:
                die(f"primary patch member missing: {member}")
            if actual_patch_sha != patch_sha:
                die(f"primary patch fingerprint disagrees with seed: {rel}")
            verify_untracked_seed(z, rel, row)

        if gps.get("path") != GPS_PATH:
            die("GPS provenance path mismatch")
        mode = str(gps.get("mode", ""))
        if mode == "git":
            head = str(gps.get("head", ""))
            patch_sha = str(gps.get("patch_sha256", ""))
            untracked_sha = str(gps.get("untracked_manifest_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                die("GPS HEAD invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", patch_sha):
                die("GPS patch fingerprint invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", untracked_sha):
                die("GPS untracked fingerprint invalid")
            try:
                actual_patch_sha = sha256_bytes(z.read(patch_member(GPS_PATH)))
            except KeyError:
                die("GPS patch member missing")
            if actual_patch_sha != patch_sha:
                die("GPS patch fingerprint disagrees with seed")
            verify_untracked_seed(z, GPS_PATH, gps)
            gps_contract = {
                "mode": "git",
                "head": head,
                "patch_sha256": patch_sha,
                "untracked_manifest_sha256": untracked_sha,
            }
        elif mode == "tree":
            tree_sha = str(gps.get("tree_sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", tree_sha):
                die("GPS tree fingerprint invalid")
            gps_contract = {"mode": "tree", "tree_sha256": tree_sha}
        else:
            die(f"GPS mode cannot be promoted: {mode!r}")

    contracts["gps_repo"] = gps_contract
    for rel in PRIMARY:
        row = primary[rel]
        spec = dynamic[rel]
        spec["post_converge_patch_sha256"] = row["patch_sha256"]
        spec["post_converge_untracked_manifest_sha256"] = row[
            "untracked_manifest_sha256"
        ]
        spec["status"] = "PROVENANCE_ACCEPTED_AFTER_STAGE8N"

    workspace_contract = contracts.get("workspace_repo_contract")
    if not isinstance(workspace_contract, dict):
        die("workspace repo contract missing")
    workspace_contract["revision_fingerprint"] = recorded
    workspace_contract["expected_project_count"] = workspace["project_count"]
    workspace_contract["status"] = "PROVENANCE_ACCEPTED_AFTER_STAGE8N"

    contracts["accepted_provenance"] = {
        "seed_sha256": seed_sha,
        "release_tag": args.release_tag,
        "request_commit": args.request_commit,
        "tooling_ref": args.tooling_ref,
        "workspace_revision_fingerprint": recorded,
    }

    cloud = knowledge.setdefault("cloud_seed", {})
    if not isinstance(cloud, dict):
        die("cloud_seed knowledge entry is not an object")
    cloud.update(
        {
            "release_tag": args.release_tag,
            "zip_sha256": seed_sha,
            "coverage": "COMPLETE_FOR_BUILD_PROVENANCE",
            "coverage_blocker": None,
            "required_format_version": 2,
            "legacy_seed_only": False,
            "legacy_seed_reason": None,
            "accepted_request_commit": args.request_commit,
            "accepted_tooling_ref": args.tooling_ref,
        }
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(knowledge, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"PROVENANCE_ACCEPT_SEED_SHA256={seed_sha}")
    print(f"PROVENANCE_ACCEPT_WORKSPACE={recorded}")
    print(f"PROVENANCE_ACCEPT_PROJECT_COUNT={workspace['project_count']}")
    print(f"PROVENANCE_ACCEPT_GPS_MODE={mode}")
    print("PROVENANCE_ACCEPTANCE_CANDIDATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
