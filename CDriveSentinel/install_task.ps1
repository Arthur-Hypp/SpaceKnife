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
param([switch]$Uninstall, [switch]$Start)

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
    # Where the data actually lives is decided at run time: an install-time
    # pointer (home.txt) or ProgramData -- never "next to the exe" any more.
    $outputDesc = "the folder that CDriveSentinel.exe paths reports"
} else {
    # Source layout: windowless pythonw.exe.
    #
    # MUST be 64-bit. A 32-bit interpreter has ~2 GB of address space, and the
    # merge step (1.2M staged rows + 1.2M inventory rows) hits it: the process
    # dies mid-merge with no traceback and no "snapshot FAILED" line, leaving
    # the staging table behind and the baseline one snapshot stale. Observed
    # twice on 2026-11-25 while the task was pointing at Python 3.7 32-bit.
    function Get-Bits([string]$exe) {
        try {
            $v = & $exe -c "import struct;print(struct.calcsize('P')*8)" 2>$null
            return "$v".Trim()
        } catch { return "" }
    }

    $pythonConsole = $null
    $found = @()
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $candidate = & $pyLauncher.Source -3 -c "import sys;print(sys.executable)" 2>$null
        if ($candidate) { $found += "$candidate".Trim() }
    }
    foreach ($cmd in (Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        $found += $cmd.Source
    }
    foreach ($candidate in ($found | Select-Object -Unique)) {
        if ((Get-Bits $candidate) -eq "64") { $pythonConsole = $candidate; break }
    }
    if (-not $pythonConsole) {
        Write-Host "ERROR: no 64-bit python.exe found." -ForegroundColor Red
        Write-Host "       A 32-bit interpreter cannot merge a 1M-row inventory (it runs out" -ForegroundColor Red
        Write-Host "       of address space and dies mid-merge). Install 64-bit Python, or use" -ForegroundColor Red
        Write-Host "       the packaged installer, which ships a 64-bit exe." -ForegroundColor Red
        exit 1
    }

    $python = Join-Path (Split-Path -Parent $pythonConsole) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host "ERROR: pythonw.exe not found next to $pythonConsole" -ForegroundColor Red
        exit 1
    }
    $script = Join-Path $root "src\main.py"
    $argLine = '"' + $script + '" daemon'
    $action = New-ScheduledTaskAction -Execute $python -Argument $argLine -WorkingDirectory $root
    $launchDesc = "Python: $python (windowless, 64-bit)"
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

if ($Start) {
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Write-Host "Started now."
} else {
    Write-Host "To start it now: Start-ScheduledTask -TaskName $taskName"
}
