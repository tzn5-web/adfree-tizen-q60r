"""MCP stdio tools. Run on the user's own device via an outbound-only tunnel."""
from __future__ import annotations
import json
from mcp.server.mcpserver import MCPServer, Image
from device_control import core, jobs, azure_store

mcp = MCPServer("ChatGPT Device Control")

@mcp.tool()
def device_status() -> str:
    """Identify PC and granted capabilities (does not change state)."""
    return json.dumps(core.status(), ensure_ascii=False)

@mcp.tool()
def list_directory(location: str = "Desktop") -> str:
    """List up to 200 files in an authorized Windows directory."""
    return json.dumps(core.list_directory(location), ensure_ascii=False)

@mcp.tool()
def read_text_file(location: str) -> str:
    """Read UTF-8 text from an authorized local directory."""
    return core.read_text(location)

@mcp.tool()
def write_text_file(location: str, content: str = "", overwrite: bool = False) -> str:
    """Create or edit a text file under authorized roots, e.g. Desktop/x.txt."""
    return json.dumps(core.write_text(location, content, overwrite), ensure_ascii=False)

@mcp.tool()
def windows_shell(command: str, engine: str = "powershell", timeout: int = 30) -> str:
    """Execute PowerShell or CMD locally if the OWNER configured shell permission."""
    return json.dumps(core.shell_command(command, engine, timeout), ensure_ascii=False)

@mcp.tool()
def screen_capture() -> Image:
    """Take one current unlocked-screen capture, not a continuous live video."""
    return Image(data=core.screenshot_png(), format="png")

@mcp.tool()
def windows_ui(action: str, x: int = 0, y: int = 0, text: str = "", amount: int = 0) -> str:
    """Act on UI: click, double_click, scroll, type, hotkey (e.g. ctrl+s)."""
    return core.ui_action(action, x, y, text, amount)

@mcp.tool()
def android_device_status() -> str:
    """Inspect paired Android device through adb."""
    return json.dumps(core.android_status(), ensure_ascii=False)

@mcp.tool()
def android_screen_capture() -> Image:
    """Take a single Android screenshot through adb."""
    return Image(data=core.android_screenshot_png(), format="png")

@mcp.tool()
def android_input(kind: str, value: str = "", x: int = 0, y: int = 0) -> str:
    """Control paired Android device with tap, text, or key event."""
    return json.dumps(core.android_action(kind, value, x, y), ensure_ascii=False)

@mcp.tool()
def android_run_shell(command: str, timeout: int = 30) -> str:
    """Run adb shell command on locally authorized paired Android device."""
    return json.dumps(core.android_shell(command, timeout), ensure_ascii=False)

@mcp.tool()
def start_windows_job(command: str, engine: str = "powershell", timeout: int = 1800) -> str:
    """Start a long local Windows script/compile asynchronously (up to 2h). Returns job_id."""
    return json.dumps(jobs.start_job(command, engine, timeout), ensure_ascii=False)

@mcp.tool()
def windows_job_status(job_id: str) -> str:
    """Check progress and bounded output of a previously started local shell job."""
    return json.dumps(jobs.job_status(job_id), ensure_ascii=False)

@mcp.tool()
def cancel_windows_job(job_id: str) -> str:
    """Terminate a running local shell job and its child process tree."""
    return json.dumps(jobs.cancel_job(job_id), ensure_ascii=False)

@mcp.tool()
def azure_create_project(project_id: str, description: str = "") -> str:
    """Create Azure-only persistent project namespace; no local project storage."""
    core.Policy().require("azure_write")
    return json.dumps(azure_store.create_project(project_id, description), ensure_ascii=False)

@mcp.tool()
def azure_list_projects() -> str:
    """List persistent project workspaces in Azure Blob."""
    core.Policy().require("azure_read")
    return json.dumps(azure_store.list_projects(), ensure_ascii=False)

@mcp.tool()
def azure_save_text(project_id: str, name: str, value: str, overwrite: bool = False) -> str:
    """Save project text artifacts in Azure, not local disk."""
    core.Policy().require("azure_write")
    return json.dumps(azure_store.save_text(project_id, name, value, overwrite), ensure_ascii=False)

@mcp.tool()
def azure_read_text(project_id: str, name: str) -> str:
    """Read one Azure project text artifact (up to 1 MiB)."""
    core.Policy().require("azure_read")
    return azure_store.read_text(project_id, name)

@mcp.tool()
def azure_list_artifacts(project_id: str) -> str:
    """List saved Azure artifacts under one project namespace."""
    core.Policy().require("azure_read")
    return json.dumps(azure_store.list_artifacts(project_id), ensure_ascii=False)

if __name__ == "__main__":
    mcp.run(transport="stdio")
