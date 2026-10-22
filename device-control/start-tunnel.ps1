$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$client = Get-Command tunnel-client -ErrorAction SilentlyContinue
if (-not $client) { throw 'tunnel-client not installed. Download from official OpenAI Platform tunnel settings.' }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw 'Run install-windows.ps1 first' }
if ([string]::IsNullOrWhiteSpace($env:CONTROL_PLANE_API_KEY)) {
    throw 'Set CONTROL_PLANE_API_KEY in this PowerShell session (never add keys to GitHub).'
}
$tunnel = Read-Host 'OpenAI tunnel ID (tunnel_ followed by 32 characters)'
if ($tunnel -notmatch '^tunnel_[a-z0-9]{32}$') { throw 'Invalid tunnel ID' }
$profile = 'chatgpt-device-control'
$command = '"' + $py + '" -m device_control.server'
& tunnel-client init --sample sample_mcp_stdio_local --profile $profile --tunnel-id $tunnel --mcp-command $command
if ($LASTEXITCODE -ne 0) { throw 'Tunnel init failed' }
& tunnel-client doctor --profile $profile --explain
if ($LASTEXITCODE -ne 0) { throw 'Tunnel doctor failed' }
Write-Host 'Starting outbound-only tunnel, keep this session open. Ctrl+C to disconnect.'
& tunnel-client run --profile $profile
exit $LASTEXITCODE
