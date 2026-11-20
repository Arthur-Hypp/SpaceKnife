# CDriveSentinel scheduled task installer (ASCII-only)
# Registers the daemon to run at logon with highest privileges.
#
# Auto-detects two layouts:
#   - PACKAGED: if CDriveSentinel.exe sits next to this script -> run the exe.
#   - SOURCE:   otherwise run src\main.py via windowless pythonw.exe.
#
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

$exe = Join-Path $root "CDriveSentinel.exe"
if (Test-Path -LiteralPath $exe) {
    # Packaged layout: the folder containing the exe is also where config/ and
    # data/ live (see p1_config.app_dir()). Hide the console window.
    $action = New-ScheduledTaskAction -Execute $exe -Argument "daemon" -WorkingDirectory $root
    $launchDesc = "EXE   : $exe"
    $outputDesc = "$root\data (or auto-relocated off the monitored volume)"
} else {
    # Source layout: windowless pythonw.exe, as before.
    $pythonConsole = (Get-Command python.exe -ErrorAction Stop).Source
    $python = Join-Path (Split-Path -Parent $pythonConsole) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host "ERROR: pythonw.exe not found next to $pythonConsole" -ForegroundColor Red
        exit 1
    }
    $script = Join-Path $root "src\main.py"
    $argLine = '"' + $script + '" daemon'
    $action = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $root
    $launchDesc = "Python: $python (windowless)"
    $outputDesc = "$root\data"
}

$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Task '$taskName' registered."
Write-Host "  $launchDesc"
Write-Host "  Start  : at logon, highest run level"
Write-Host "The daemon only READS C:; all output goes to $outputDesc"
Write-Host "To start it now: Start-ScheduledTask -TaskName $taskName"
