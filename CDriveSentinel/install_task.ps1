# CDriveSentinel P1 scheduled task installer (ASCII-only)
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
# Remove:
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall
param([switch]$Uninstall)

$taskName = "CDriveSentinel"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$taskName' removed."
    exit 0
}

$principalCheck = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: run this installer from an elevated (Administrator) PowerShell." -ForegroundColor Red
    exit 1
}

$pythonConsole = (Get-Command python.exe -ErrorAction Stop).Source
$python = Join-Path (Split-Path -Parent $pythonConsole) "pythonw.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "ERROR: pythonw.exe not found next to $pythonConsole" -ForegroundColor Red
    exit 1
}
$script = Join-Path $root "src\main.py"
$argLine = '"' + $script + '" daemon'

$action = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Task '$taskName' registered."
Write-Host "  Python : $python (windowless)"
Write-Host "  Script : $argLine"
Write-Host "  Start  : at logon, highest run level; no console window"
Write-Host "The daemon only READS C:; all output goes to $root\data"
Write-Host "To start it now: Start-ScheduledTask -TaskName $taskName"
