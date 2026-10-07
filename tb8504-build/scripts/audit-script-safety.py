#!/usr/bin/env python3
"""Static safety audit for TB8504 local/cloud helper scripts.

The audit distinguishes executable command contexts from comments and strings
that merely document forbidden operations.
"""
from __future__ import annotations

import ast
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

FORBIDDEN_CMD = re.compile(r"(^|[;&|()]\s*)(adb|fastboot)(\s|$)", re.I)
FORBIDDEN_CLEAN = re.compile(r"\b(?:mka|make)\s+(?:clean|installclean|clobber)\b", re.I)
FORBIDDEN_DEV_WRITE = re.compile(
    r"\bdd\b[^\n]*\bof=/dev/|"
    r"\b(?:cp|mv|tee)\b[^\n]*(?:>|/dev/(?:block|snd))",
    re.I,
)

def fail(msg: str) -> None:
    print(f"SCRIPT_SAFETY_FAIL={msg}")
    raise SystemExit(2)

def strip_shell_comment(line: str) -> str:
    # Good enough for command-level safety scanning. Full shell parsing is not
    # needed because dangerous commands in this project must appear as tokens.
    out=[]
    quote=None
    esc=False
    for ch in line:
        if esc:
            out.append(ch)
            esc=False
            continue
        if ch == "\\":
            out.append(ch)
            esc=True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote=None
            continue
        if ch in ("'", '"'):
            quote=ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)

def check_command_text(origin: str, text: str) -> None:
    for rx,name in (
        (FORBIDDEN_CMD,"device command"),
        (FORBIDDEN_CLEAN,"destructive build clean"),
        (FORBIDDEN_DEV_WRITE,"device-node write"),
    ):
        if rx.search(text):
            fail(f"{name} in {origin}: {text.strip()}")

def audit_shell(path: Path) -> None:
    for no,raw in enumerate(path.read_text("utf-8",errors="replace").splitlines(),1):
        line=strip_shell_comment(raw).strip()
        if not line:
            continue
        # Ignore pure diagnostic strings. The command itself is echo/printf.
        if re.match(r"^(echo|printf)\b", line):
            continue
        check_command_text(f"{path.name}:{no}", line)

def literal_command(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value,str):
        return node.value
    if isinstance(node,(ast.List,ast.Tuple)):
        vals=[]
        for elt in node.elts:
            if not isinstance(elt,ast.Constant) or not isinstance(elt.value,str):
                return None
            vals.append(elt.value)
        return " ".join(vals)
    return None

def audit_python(path: Path) -> None:
    text=path.read_text("utf-8",errors="replace")
    try:
        tree=ast.parse(text,filename=str(path))
    except SyntaxError as exc:
        fail(f"python parse failed {path}: {exc}")
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):
            continue
        func=node.func
        name=""
        if isinstance(func,ast.Attribute) and isinstance(func.value,ast.Name):
            name=f"{func.value.id}.{func.attr}"
        elif isinstance(func,ast.Name):
            name=func.id
        if name not in {
            "subprocess.run","subprocess.Popen","subprocess.check_call",
            "subprocess.check_output","os.system",
        }:
            continue
        if not node.args:
            continue
        cmd=literal_command(node.args[0])
        if cmd:
            check_command_text(f"{path.name}:{getattr(node,'lineno','?')}",cmd)

    # The autopilot uses dynamic command variables, so require its runtime
    # guard to be wired into both execution paths.
    if path.name == "tb8504-autopilot.py":
        required=(
            "def safety_check_command",
            "self.safety_check_command(printable)",
            "FORBIDDEN_COMMAND_PATTERNS",
        )
        for token in required:
            if token not in text:
                fail(f"autopilot runtime guard missing: {token}")

def main() -> int:
    sh_files=sorted(ROOT.glob("*.sh"))
    py_files=sorted(ROOT.glob("*.py"))
    if not sh_files or not py_files:
        fail("unexpected empty script set")
    for p in sh_files:
        audit_shell(p)
    for p in py_files:
        audit_python(p)
    print(f"SAFETY_SHELL_FILES={len(sh_files)}")
    print(f"SAFETY_PYTHON_FILES={len(py_files)}")
    print("TB8504_SCRIPT_SAFETY=PASS")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
