# ChatGPT Device Control v1.0

Independent subproject hosted in the tzn5-web/adfree-tizen-q60r repository by explicit owner request. Does not modify or import any Tizen/YouTube files. **The repository is public: NEVER commit credentials, screenshots, personal files, policy.json, or command output.**

## Exactly what is implemented

Windows 10/11 local MCP agent (Python 3.11+):
- Device status; authorized folder listing; UTF-8 file reads; create/overwrite text files.
- PowerShell and CMD, 1–120 second synchronous timeout, bounded tool output.
- Long-running Windows PowerShell/CMD jobs, up to 2 hours, with start/status/cancel MCP tools, bounded in-memory output and policy-revocation monitoring. Jobs stop when owner disables local shell access; job state is not durable after agent exit.
- One-off Windows screenshots; click, double-click, scroll, type and hotkeys.
- Android via installed ADB with an explicitly paired serial: status, screenshots, tap, text, key events and shell.
- Policy loaded from the local Windows user profile on every operation: disabled until opt-in at installation. No Windows service, UAC bypass, inbound port, or scheduled task.
- Optional Azure Blob audit of **metadata only**: timestamps, action types and device name. Screenshots and arbitrary shell output are not archived automatically.
- Azure-only project workspace (separate Blob container): create/list projects and save/read/list text artifacts under a per-project prefix.
- Unit tests and GitHub Actions independent of Tizen build jobs.

What is NOT implemented: streaming video, multiuser session brokering, offline control when the PC is off, elevation through UAC, Android control without USB/wireless debugging, or guaranteed write-tool availability in every ChatGPT plan.

## One-time Windows setup (no Microsoft Store)

1. Download this subdirectory or the source release; copy it somewhere such as C:\Tools\ChatGPTDeviceControl. Do NOT put it in D:\AI LOCAL.
2. Install official Python 3.11+ from python.org, ensuring it is available as py.exe or python.exe.
3. Run setup.cmd. It builds an isolated venv, installs dependencies, runs tests, then asks for the exact word AUTORIZEZ to enable Windows capabilities. If declined, the agent stays disabled.
4. The default authorized file roots are Desktop, Documents and Downloads. The policy file is at %LOCALAPPDATA%\ChatGPTDeviceControl\policy.json. Editing it disables/enables any capability immediately; set enabled to false for the emergency kill switch.
5. For Android, install Google Android SDK platform-tools, authorize ADB on the phone, edit the policy to set adb_path and android_serial to the exact output of adb devices, and explicitly set android_read/android_write permissions. Root is NOT required for ordinary ADB UI control.
6. For remote access, obtain OpenAI Secure MCP Tunnel access and download the signed official tunnel-client from the OpenAI Platform tunnel settings, as described at https://developers.openai.com/api/docs/guides/secure-mcp-tunnels . Add its executable to PATH.
7. Acquire a valid OpenAI tunnel ID and runtime key for your Platform organization. start-tunnel.ps1 asks for the key using a hidden local prompt if not already present in CONTROL_PLANE_API_KEY. The key must never enter GitHub or the chat. Run start-tunnel.ps1; this initializes the tunnel profile, runs doctor, and starts the outbound-only link.
8. In ChatGPT -> Plugins -> Add custom MCP server -> Tunnel, select the authorized tunnel. Review the discovered tools and policies. Availability of mutating tools depends on your ChatGPT plan/workspace permissions.

For a demonstration, ask the connected plugin for device_status, then use write_text_file with location Desktop/x.txt and content Hello. Do not mistake source code being available on GitHub for a live connection.

## Roles: GitHub / Azure / devices

GitHub: source, PR reviews, CI test logs, release artifacts. GitHub Actions is NOT a permanent remote-control runner.

Azure: after explicit authorization to create an account, a private Standard_LRS Blob account in existing group rg-chatgpt-azure (template at infra/azure.bicep) can keep a write-once audit of action metadata and project artifacts in separate private containers. The runtime uses Azure Entra identity with Azure RBAC and the environment variable CDC_AZURE_BLOB_URL (https://YOURACCOUNT.blob.core.windows.net). Set CDC_AZURE_AUDIT_CONTAINER=device-control-audit or use the default. The local Azure CLI credential or a narrowly scoped identity needs Storage Blob Data Contributor RBAC. The Azure project MCP tools require azure_read / azure_write flags in the local policy. Do not use storage account keys. There is no requirement to run a VM.

PC/Android: executes commands on the user's own session. The local policy file, binaries and short-lived credentials are necessary local runtime state, not a separate local AI project. No connection to D:\AI LOCAL, no use of any existing personal AI agent.

## Cost constraint

The user's Azure credit ceiling is a **TOTAL 200 USD**, not per month. No compute, Azure resources or pay-per-use AI models are created by this repository or its GitHub workflow. Before deployment, review existing resources and costs; deploy only one small Standard_LRS storage account if authorized, and configure budget alerts. Alerts alone do not automatically stop billing.

## Security model

Only one trust bridge: authenticated outbound tunnel -> stdio MCP server on the logged-in PC -> policy enforcement per tool. Never expose this server as an anonymous public HTTP endpoint. Prefer a dedicated standard Windows user. The policy explicitly permits powerful shell actions, so connect only trusted clients, keep the tunnel key private, and stop the tunnel when not in use.

The original Tizen application CI, player, ad filter, package.json, and television artifacts remain untouched.
