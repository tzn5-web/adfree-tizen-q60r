"""Long-running local jobs: bounded in-memory output and explicit cancellation.

Jobs survive chat tool-call completion, but not agent process restart.
No local persistent job database: Azure may be configured separately for audit metadata.
"""
from __future__ import annotations

import platform
import subprocess
import threading
import time
import uuid
from typing import Any

from device_control.core import Policy, AccessDenied, audit

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
MAX_JOBS = 3
MAX_OUTPUT = 48000

def command_args(command: str, engine: str) -> list[str]:
    if not command or len(command) > 16000 or "\x00" in command:
        raise ValueError("Invalid command")
    if engine not in ("powershell", "cmd"):
        raise ValueError("Only powershell or cmd")
    return (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            if engine == "powershell" else ["cmd.exe", "/D", "/S", "/C", command])

def _terminate(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    if platform.system() == "Windows":
        # Kill the Windows process tree; PowerShell scripts may spawn child processes.
        subprocess.run(["taskkill.exe", "/PID", str(p.pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    if p.poll() is None:
        p.kill()

def start_job(command: str, engine: str = "powershell", timeout: int = 1800) -> dict[str, str]:
    Policy().require("shell")
    if platform.system() != "Windows":
        raise OSError("Windows session required")
    if not isinstance(timeout, int) or not (1 <= timeout <= 7200):
        raise ValueError("timeout must be between 1 and 7200 seconds")
    args = command_args(command, engine)
    audit("job_start", engine)
    with _lock:
        active = sum(j["status"] in ("queued", "running") for j in _jobs.values())
        if active >= MAX_JOBS:
            raise RuntimeError("Maximum concurrent jobs reached")
        now = time.monotonic()
        for key, job in list(_jobs.items()):
            if job["status"] not in ("queued", "running") and now - job["created"] > 3600:
                del _jobs[key]
        ident = uuid.uuid4().hex
        _jobs[ident] = {"id": ident, "status": "queued", "exit_code": None,
                        "stdout": "", "bytes_seen": 0, "error": "",
                        "created": now, "cancel": False, "process": None}

    def work():
        try:
            with _lock:
                item = _jobs[ident]
                if item["cancel"]:
                    item["status"] = "cancelled"
                    return
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                item["process"] = proc
                item["status"] = "running"
            def read_output():
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    decoded = chunk.decode("utf-8", errors="replace")
                    with _lock:
                        item["bytes_seen"] += len(chunk)
                        item["stdout"] = (item["stdout"] + decoded)[-MAX_OUTPUT:]
                proc.stdout.close()
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                return_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate(proc)
                return_code = proc.wait(timeout=10)
                with _lock:
                    item["error"] = "Job timed out"
            reader.join(timeout=3)
            with _lock:
                item["exit_code"] = return_code
                item["status"] = "cancelled" if item["cancel"] else ("timeout" if item["error"] else "finished")
        except Exception as exc:
            with _lock:
                item["error"] = str(exc)[:1000]
                item["status"] = "failed"

    thread = threading.Thread(target=work, daemon=True, name="cdc-" + ident[:8])
    thread.start()
    return {"job_id": ident, "status": "queued"}

def job_status(job_id: str) -> dict[str, Any]:
    Policy().require("shell")
    with _lock:
        if job_id not in _jobs:
            raise KeyError("Unknown/expired job ID")
        job = _jobs[job_id]
        return {k: job[k] for k in ("id", "status", "exit_code", "stdout", "bytes_seen", "error")}

def cancel_job(job_id: str) -> dict[str, str]:
    Policy().require("shell")
    with _lock:
        if job_id not in _jobs:
            raise KeyError("Unknown/expired job ID")
        item = _jobs[job_id]
        item["cancel"] = True
        p = item["process"]
        if item["status"] == "queued":
            item["status"] = "cancelled"
        elif item["status"] != "running":
            return {"job_id": job_id, "status": item["status"]}
    if p is not None:
        _terminate(p)
    return {"job_id": job_id, "status": "cancellation_requested"}
