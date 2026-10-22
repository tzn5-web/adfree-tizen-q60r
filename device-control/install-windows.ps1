$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
Write-Host 'ChatGPT Device Control - Windows installer (isolated from Tizen and AI LOCAL)'
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    & py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { & py -3 -m venv .venv }
} elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    & python.exe -m venv .venv
} else { throw 'Python from python.org (3.11+) required. Microsoft Store is NOT required.' }
if ($LASTEXITCODE -ne 0) { throw 'Failed creating Python virtual environment' }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if ($LASTEXITCODE -ne 0) { throw 'Requires Python 3.11+' }
& $python -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Failed installing dependencies' }
& $python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Local regression tests failed' }

$root = Join-Path $env:LOCALAPPDATA 'ChatGPTDeviceControl'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$policyPath = Join-Path $root 'policy.json'
if (Test-Path $policyPath) {
    Write-Host "Existing local policy preserved: $policyPath"
} else {
    Write-Host 'Authorize ON THIS PC which actions a ChatGPT plugin may perform after pairing a tunnel.'
    Write-Host 'Shell access permits arbitrary actions AS YOUR WINDOWS USER. Use a dedicated standard account for maximum isolation.'
    $grant = Read-Host 'Type AUTORIZEZ to enable files, screen, UI, CMD and PowerShell (anything else keeps permissions disabled)'
    $full = ($grant -ceq 'AUTORIZEZ')
    $policy = @{
        schema = 1
        enabled = $full
        capabilities = @{
            files_read = $full
            files_write = $full
            shell = $full
            screen = $full
            ui = $full
            android_read = $false
            android_write = $false
        }
        roots = @('Desktop', 'Documents', 'Downloads')
        android_serial = ''
        adb_path = ''
    }
    $json = $policy | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($policyPath, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Policy written: $policyPath. Permissions granted: $full"
}
Write-Host 'Installation complete. No startup service installed and no network port opened.'
Write-Host 'Read README.md to create the OpenAI Secure MCP Tunnel. Run .\start-tunnel.ps1 when paired.'
