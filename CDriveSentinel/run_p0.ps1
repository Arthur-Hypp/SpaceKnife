# CDriveSentinel P0 baseline launcher (ASCII only, Windows PowerShell 5.1 safe)
# Run from an elevated terminal for complete results:
#   powershell -ExecutionPolicy Bypass -File .\run_p0.ps1
# Any extra arguments are forwarded to p0_baseline.py, e.g.:
#   powershell -ExecutionPolicy Bypass -File .\run_p0.ps1 --force-scan --scan-depth 3
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$script = Join-Path $root 'src\p0_baseline.py'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'WARNING: not elevated. Some read-only checks (fsutil / vssadmin / DISM) may fail.' -ForegroundColor Yellow
    Write-Host 'Recommendation: open Terminal as Administrator and run this script again.' -ForegroundColor Yellow
}

& $py $script @args
exit $LASTEXITCODE
