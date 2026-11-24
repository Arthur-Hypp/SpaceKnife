# CDriveSentinel daily-report popup task installer (ASCII-only)
# Registers a SEPARATE logon task that opens YESTERDAY's HTML report in the
# default browser, once per day. Kept separate from the elevated daemon on
# purpose: a browser launched from an elevated process is refused by
# Chrome/Edge. The TASK runs at normal privilege; REGISTERING it needs admin.
#
# Auto-detects two layouts:
#   - PACKAGED: if CDriveSentinel.exe sits next to this script -> run the exe.
#   - SOURCE:   otherwise run src\main.py open-report via windowless pythonw.exe.
#
# Run from an ELEVATED PowerShell (Run as Administrator):
#   powershell -ExecutionPolicy Bypass -File .\install_popup_task.ps1
# Remove (also needs admin):
#   powershell -ExecutionPolicy Bypass -File .\install_popup_task.ps1 -Uninstall
param([switch]$Uninstall)

$taskName = "CDriveSentinel-OpenReport"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Register-ScheduledTask writes to the system task store -> admin required.
$principalCheck = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: run this script from an ELEVATED PowerShell (Run as Administrator)." -ForegroundColor Red
    Write-Host "  Registering a scheduled task needs admin; the task itself still runs as normal user." -ForegroundColor Red
    exit 1
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$taskName' removed."
    exit 0
}

$exe = Join-Path $root "CDriveSentinel.exe"
if (Test-Path -LiteralPath $exe) {
    $action = New-ScheduledTaskAction -Execute $exe -Argument "open-report" -WorkingDirectory $root
    $launchDesc = "EXE   : $exe open-report"
} else {
    # Locate Python 3.13 explicitly. The PATH 'python.exe' on this machine is
    # an old 3.7 (EOL) that cannot open the WAL database; the project is
    # developed and selftested on 3.13. Prefer py-launcher 3.13, then the
    # known per-user install path; refuse if neither is present.
    $pyExe = $null
    try {
        $p = & py -3.13 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $pyExe = ($p | Select-Object -First 1).Trim() }
    } catch { }
    if ((-not $pyExe) -or (-not (Test-Path -LiteralPath $pyExe))) {
        $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
        if (Test-Path -LiteralPath $candidate) { $pyExe = $candidate }
    }
    if ((-not $pyExe) -or (-not (Test-Path -LiteralPath $pyExe))) {
        Write-Host "ERROR: Python 3.13 not found. Install it or edit this script's pyExe path." -ForegroundColor Red
        exit 1
    }
    $python = Join-Path (Split-Path -Parent $pyExe) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host "ERROR: pythonw.exe not found next to $pyExe" -ForegroundColor Red
        exit 1
    }
    $script = Join-Path $root "src\main.py"
    if (-not (Test-Path -LiteralPath $script)) {
        Write-Host "ERROR: $script not found (no packed exe either)" -ForegroundColor Red
        exit 1
    }
    $argLine = '"' + $script + '" open-report'
    $action = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $root
    $launchDesc = "Python: $python (windowless) open-report"
}

# Trigger at logon, with a short delay so the desktop is fully ready.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT15S"

# NORMAL privilege (no RunLevel Highest) + Interactive: the browser must land
# in the user's own desktop session, not an elevated/Session-0 context.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) `
    -MultipleInstances IgnoreNew -Hidden

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host "ERROR: Register-ScheduledTask FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# Verify the task actually exists before claiming success.
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: registration reported success but task '$taskName' not found." -ForegroundColor Red
    exit 1
}

Write-Host "Task '$taskName' registered and verified."
Write-Host "  $launchDesc"
Write-Host "  Start  : at logon (+15s desktop delay), NORMAL privilege, interactive"
Write-Host "  Result : opens yesterday's C: drive daily report once per day"
Write-Host "To test now: Start-ScheduledTask -TaskName $taskName"
