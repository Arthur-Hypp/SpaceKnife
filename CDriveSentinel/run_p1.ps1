# CDriveSentinel P1 launcher (ASCII-only on purpose: avoids PowerShell encoding issues)
# Examples:
#   powershell -ExecutionPolicy Bypass -File .\run_p1.ps1 init-db
#   powershell -ExecutionPolicy Bypass -File .\run_p1.ps1 snapshot
#   powershell -ExecutionPolicy Bypass -File .\run_p1.ps1 report --day 2026-11-16 --top 20
#   powershell -ExecutionPolicy Bypass -File .\run_p1.ps1 daemon --once

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host "CDriveSentinel P1  root: $root"
if (-not $isAdmin) {
    Write-Host "WARNING: not running as Administrator. Read-only scanning still works," -ForegroundColor Yellow
    Write-Host "         but admin-only checks (VSS, USN deep reads) will be unavailable." -ForegroundColor Yellow
} else {
    Write-Host "Administrator: yes"
}

$cli = if ($args.Count -gt 0) { $args } else { @("status") }
& python "$root\src\main.py" @cli
exit $LASTEXITCODE
