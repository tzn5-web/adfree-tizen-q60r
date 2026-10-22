"""Device operations. Security policy is loaded on EACH call; no inbound listener."""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_READ = 1024 * 1024
MAX_WRITE = 512 * 1024
MAX_OUTPUT = 24000
CAPABILITIES = (
    "files_read", "files_write", "shell", "screen",
    "ui", "android_read", "android_write", "azure_read", "azure_write"
)

class AccessDenied(PermissionError):
    pass

def policy_path() -> Path:
    folder = os.environ.get("LOCALAPPDATA", str(Path.home()))
    return Path(os.environ.get("CDC_POLICY", str(Path(folder) / "ChatGPTDeviceControl" / "policy.json")))

class Policy:
    def __init__(self, data: dict[str, Any] | None = None):
        if data is None:
            path = policy_path()
            if not path.is_file():
                raise AccessDenied("No local policy. Run install-windows.ps1 to authorize this PC.")
            data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise AccessDenied("Invalid policy schema")
        self.data = data

    def require(self, capability: str) -> None:
        if capability not in CAPABILITIES:
            raise AccessDenied("Unknown capability")
        if self.data.get("enabled") is not True or self.data.get("capabilities", {}).get(capability) is not True:
            raise AccessDenied("Capability disabled locally: " + capability)

    def roots(self) -> list[Path]:
        raw = self.data.get("roots", ["Desktop", "Documents"])
        if not isinstance(raw, list) or not raw:
            raise AccessDenied("No filesystem roots granted")
        return [resolve_path(str(r)) for r in raw]

def known_folder(name: str) -> Path:
    if platform.system() == "Windows" and name in ("Desktop", "Documents"):
        try:
            import winreg
            reg_name = "Personal" if name == "Documents" else name
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                value, _ = winreg.QueryValueEx(k, reg_name)
            return Path(os.path.expandvars(value)).expanduser().resolve()
        except (OSError, ValueError):
            pass
    return (Path.home() / name).resolve()

def resolve_path(location: str) -> Path:
    if not location or not str(location).strip() or "\x00" in location:
        raise ValueError("Non-empty path required")
    location = location.strip()
    for prefix in ("Desktop", "Documents", "Downloads"):
        if location == prefix:
            return known_folder(prefix)
        if location.startswith(prefix + "/") or location.startswith(prefix + "\\"):
            return (known_folder(prefix) / location[len(prefix) + 1:]).resolve()
    return Path(os.path.expandvars(location)).expanduser().resolve()

def within(path: Path, roots: list[Path]) -> bool:
    path = path.resolve()
    for root in roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False

def checked_file_path(location: str, capability: str) -> Path:
    policy = Policy()
    policy.require(capability)
    p = resolve_path(location)
    if not within(p, policy.roots()):
        raise AccessDenied("Path not in authorized roots")
    return p

def status() -> dict[str, Any]:
    p = policy_path()
    if p.exists():
        cfg = Policy()
        permissions = {k: cfg.data.get("capabilities", {}).get(k) is True for k in CAPABILITIES}
        enabled = cfg.data.get("enabled") is True
    else:
        permissions, enabled = {}, False
    return {"device": platform.node(), "os": platform.platform(),
            "user": os.environ.get("USERNAME", os.environ.get("USER", "unknown")),
            "policy_enabled": enabled, "permissions": permissions,
            "mcp_transport": "stdio through authenticated outbound OpenAI tunnel",
            "azure_audit": bool(os.environ.get("CDC_AZURE_BLOB_URL"))}

def list_directory(location: str = "Desktop") -> list[dict[str, Any]]:
    path = checked_file_path(location, "files_read")
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    entries = []
    for item in sorted(path.iterdir(), key=lambda p: p.name.casefold())[:200]:
        entries.append({"name": item.name, "directory": item.is_dir(),
                        "size": item.stat().st_size if item.is_file() else None})
    return entries

def read_text(location: str) -> str:
    p = checked_file_path(location, "files_read")
    if not p.is_file() or p.stat().st_size > MAX_READ:
        raise ValueError("Expected text file no larger than 1 MiB")
    return p.read_text(encoding="utf-8-sig")

def audit(action: str, target: str = "") -> None:
    """Azure Blob-only persistent journal; never save commands, screenshots or secrets."""
    url = os.environ.get("CDC_AZURE_BLOB_URL", "").strip()
    if not url:
        return
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    account = BlobServiceClient(account_url=url, credential=DefaultAzureCredential())
    bucket = os.environ.get("CDC_AZURE_AUDIT_CONTAINER", "device-control-audit")
    record = {"ts": datetime.now(timezone.utc).isoformat(), "device": platform.node(),
              "action": action, "target_type": target}
    key = "events/" + datetime.now(timezone.utc).strftime("%Y/%m/%d/") + uuid.uuid4().hex + ".json"
    account.get_blob_client(bucket, key).upload_blob(
        json.dumps(record, separators=(",", ":")).encode("utf-8"), overwrite=False)

def write_text(location: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    p = checked_file_path(location, "files_write")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ValueError("Write exceeds 512 KiB")
    if not p.parent.is_dir():
        raise FileNotFoundError("Parent directory does not exist")
    audit("file_write", "text_file")
    with p.open("wb" if overwrite else "xb") as stream:
        stream.write(data)
    return {"path": str(p), "bytes": len(data), "overwritten": overwrite}

def run_process(args: list[str], timeout: int = 30, binary: bool = False) -> dict[str, Any] | bytes:
    """Bounded output using auto-deleted temporary streams; kill Windows descendants on timeout."""
    if not isinstance(timeout, int) or not 1 <= timeout <= 120:
        raise ValueError("Timeout must be 1–120 seconds")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if platform.system() == "Windows":
                subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, check=False)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            raise TimeoutError("Command timed out after " + str(timeout) + " seconds")
        out.seek(0, 2)
        out_size = out.tell()
        err.seek(0, 2)
        err_size = err.tell()
        out.seek(0)
        err.seek(0)
        if binary:
            if code != 0:
                raise RuntimeError(err.read(1000).decode(errors="replace"))
            if out_size > 8 * 1024 * 1024:
                raise ValueError("Binary output too large")
            return out.read()
        return {"exit_code": code,
                "stdout": out.read(MAX_OUTPUT).decode("utf-8", errors="replace"),
                "stderr": err.read(MAX_OUTPUT).decode("utf-8", errors="replace"),
                "truncated": out_size > MAX_OUTPUT or err_size > MAX_OUTPUT}

def shell_command(command: str, engine: str = "powershell", timeout: int = 30) -> dict[str, Any]:
    Policy().require("shell")
    if not command or len(command) > 16000 or "\x00" in command:
        raise ValueError("Invalid shell command")
    if engine not in ("powershell", "cmd"):
        raise ValueError("engine must be powershell or cmd")
    if platform.system() != "Windows":
        raise OSError("Windows command requested on non-Windows host")
    args = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if engine == "powershell" else ["cmd.exe", "/D", "/S", "/C", command])
    audit("shell", engine)
    return run_process(args, timeout=timeout)

def screenshot_png() -> bytes:
    Policy().require("screen")
    if platform.system() != "Windows":
        raise OSError("Requires an unlocked Windows desktop session")
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    img.thumbnail((1920, 1200))
    stream = io.BytesIO()
    img.save(stream, format="PNG")
    return stream.getvalue()

def ui_action(action: str, x: int = 0, y: int = 0, text: str = "", amount: int = 0) -> str:
    Policy().require("ui")
    if platform.system() != "Windows":
        raise OSError("Windows interactive session required")
    audit("ui", action)
    import pyautogui
    pyautogui.FAILSAFE = True
    if action == "click":
        pyautogui.click(x=x, y=y)
    elif action == "double_click":
        pyautogui.doubleClick(x=x, y=y)
    elif action == "scroll":
        pyautogui.scroll(max(-20, min(20, amount)))
    elif action == "type":
        if len(text) > 3000:
            raise ValueError("Text too long")
        pyautogui.write(text, interval=0.003)
    elif action == "hotkey":
        parts = [p.strip().lower() for p in text.split("+")]
        if not 1 <= len(parts) <= 4 or not all(re.fullmatch(r"[a-z0-9_]+", p) for p in parts):
            raise ValueError("Invalid hotkey")
        pyautogui.hotkey(*parts)
    else:
        raise ValueError("Unknown UI action")
    return "UI action completed: " + action

def adb_prefix() -> list[str]:
    p = Policy()
    serial = p.data.get("android_serial", "")
    if not serial or not re.fullmatch(r"[A-Za-z0-9._:-]{4,100}", serial):
        raise AccessDenied("Pair a specific Android device in local policy first")
    adb = p.data.get("adb_path") or shutil.which("adb")
    if not adb:
        raise FileNotFoundError("Install Android platform-tools; set adb_path in policy")
    return [str(adb), "-s", serial]

def android_status() -> dict[str, Any]:
    Policy().require("android_read")
    result = run_process(adb_prefix() + ["get-state"], timeout=10)
    return result

def android_screenshot_png() -> bytes:
    Policy().require("android_read")
    result = run_process(adb_prefix() + ["exec-out", "screencap", "-p"], timeout=20, binary=True)
    if not isinstance(result, bytes) or not result.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Android did not return PNG")
    return result

def android_action(kind: str, value: str = "", x: int = 0, y: int = 0) -> dict[str, Any]:
    Policy().require("android_write")
    if kind == "tap":
        args = ["input", "tap", str(int(x)), str(int(y))]
    elif kind == "text":
        if len(value) > 1000 or not re.fullmatch(r"[A-Za-z0-9 .,!?@:_+-]*", value):
            raise ValueError("ADB text allows only simple characters")
        args = ["input", "text", value.replace(" ", "%s")]
    elif kind == "key":
        if not re.fullmatch(r"[A-Z0-9_]{1,36}", value.upper()):
            raise ValueError("Invalid key name")
        args = ["input", "keyevent", value.upper()]
    else:
        raise ValueError("Unsupported Android action")
    audit("android_write", kind)
    return run_process(adb_prefix() + ["shell"] + args, timeout=15)

def android_shell(command: str, timeout: int = 30) -> dict[str, Any]:
    Policy().require("android_write")
    if not command or len(command) > 4096 or "\x00" in command:
        raise ValueError("Invalid Android shell command")
    audit("android_shell", "adb")
    return run_process(adb_prefix() + ["shell", "sh", "-c", command], timeout=timeout)
