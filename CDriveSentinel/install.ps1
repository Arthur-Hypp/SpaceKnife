#Requires -Version 5.1
<#
CDriveSentinel installer (script mode).

    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -DataDir D:\CDriveSentinelData
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall          # keeps data
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall -PurgeData

What it does (same actions as the Inno Setup script in CDriveSentinel.iss):

  1. copies the program to  %ProgramFiles%\CDriveSentinel
  2. creates the data folder (default: the first non-C: volume) and grants
     "users modify" on data\runtime and data\logs -- the tray runs un-elevated
     and those are the only two folders it ever writes
  3. writes %ProgramData%\CDriveSentinel\home.txt so the collector, the tray and
     the CLI all resolve the same data folder
  4. writes the initial config through `CDriveSentinel.exe setup`
  5. registers TWO at-logon tasks: the elevated collector (install_task.ps1,
     started right away) and the normal-privilege daily-report popup
     (install_popup_task.ps1)
  6. registers the tray under HKLM Run so every account gets an icon

Uninstall removes both tasks, the Run entry and the program folder; data is
kept unless -PurgeData is passed (or the prompt is answered 'y').
Everything is idempotent: running it again upgrades in place and keeps the data.
ASCII-only on purpose (PowerShell 5.1 reads a BOM-less .ps1 as GBK, and a
console at code page 936 renders UTF-8 output as mojibake).
#>
[CmdletBinding()]
param(
    [string]$DataDir = "",
    [string]$SourceDir = "",
    [switch]$NoPrompt,
    [switch]$Uninstall,
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

$AppName    = "CDriveSentinel"
$InstallDir = Join-Path $env:ProgramFiles $AppName
$PtrDir     = Join-Path $env:ProgramData $AppName
$PtrFile    = Join-Path $PtrDir "home.txt"
$TaskName   = $AppName
$PopupTask  = "$AppName-OpenReport"
$RunKey     = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
$RunValue   = "CDriveSentinelTray"
$TrayExe    = Join-Path $InstallDir "SentinelTray.exe"
$CliExe     = Join-Path $InstallDir "CDriveSentinel.exe"
# The same script ships in two places: the repo (beside dist\) and inside the
# distributed folder itself (next to the exes). Resolve whichever applies.
$Source = $SourceDir
if (-not $Source) {
    $repoDist = Join-Path $PSScriptRoot ("dist\" + $AppName)
    if (Test-Path -LiteralPath $repoDist) { $Source = $repoDist }
    elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot "CDriveSentinel.exe")) { $Source = $PSScriptRoot }
    else { $Source = $repoDist }
}
$UsersSid   = "*S-1-5-32-545"      # BUILTIN\Users, locale independent

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "ERROR: administrator rights are required." -ForegroundColor Red
        Write-Host "       Right-click setup.cmd and choose 'Run as administrator'."
        exit 1
    }
}

function Get-PointerTarget {
    if (-not (Test-Path -LiteralPath $PtrFile)) { return "" }
    $line = (Get-Content -LiteralPath $PtrFile -TotalCount 1 -ErrorAction SilentlyContinue)
    if ($null -eq $line) { return "" }
    return $line.Trim()
}

function Get-DefaultDataDir {
    # Prefer a volume that is not the one being monitored. C: is the point of
    # the whole exercise -- 300-600 MB of our own data there makes the tool part
    # of the problem it is supposed to explain.
    $drives = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -ne "C" -and $_.Free -gt 0 } |
              Sort-Object Name
    foreach ($d in $drives) {
        return (Join-Path ($d.Name + ":\") "CDriveSentinelData")
    }
    return ""
}

function Grant-UsersModify([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    & icacls $Path /grant ("{0}:(OI)(CI)M" -f $UsersSid) /T /Q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  WARNING: could not grant write access on {0}" -f $Path) -ForegroundColor Yellow
    }
}

function Stop-Running {
    foreach ($name in @("SentinelTray", "CDriveSentinelDaemon", "CDriveSentinel")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, "OrdinalIgnoreCase") } |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $PopupTask -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------- uninstall
if ($Uninstall) {
    Assert-Admin
    Write-Host "== Removing $AppName"
    $target = Get-PointerTarget

    Write-Host "[1/5] stopping processes and both scheduled tasks"
    Stop-Running
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $PopupTask -Confirm:$false -ErrorAction SilentlyContinue

    Write-Host "[2/5] removing the tray autostart entry"
    Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue

    Write-Host "[3/5] removing the program folder"
    if ((Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath (Join-Path $InstallDir "CDriveSentinel.exe"))) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
    }

    $deleteData = [bool]$PurgeData
    if (-not $PurgeData -and -not $NoPrompt -and $target -and (Test-Path -LiteralPath $target)) {
        $answer = Read-Host ("Delete the recorded data in {0} as well? [y/N]" -f $target)
        $deleteData = ($answer -eq "y" -or $answer -eq "Y")
    }
    if ($deleteData -and $target -and (Test-Path -LiteralPath $target)) {
        Write-Host "[4/5] deleting data: $target"
        Remove-Item -LiteralPath $target -Recurse -Force
    } elseif ($target) {
        Write-Host "[4/5] keeping data: $target"
    } else {
        Write-Host "[4/5] no recorded data folder"
    }

    Write-Host "[5/5] removing the pointer"
    Remove-Item -LiteralPath $PtrFile -Force -ErrorAction SilentlyContinue
    if ((Test-Path -LiteralPath $PtrDir) -and
        -not (Get-ChildItem -LiteralPath $PtrDir -Recurse -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $PtrDir -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Done."
    exit 0
}

# ----------------------------------------------------------------------- install
Assert-Admin

if (-not (Test-Path -LiteralPath $Source)) {
    Write-Host ("ERROR: build output not found: {0}" -f $Source) -ForegroundColor Red
    Write-Host "       Run 'python build.py' first."
    exit 2
}
foreach ($exe in @("CDriveSentinel.exe", "CDriveSentinelDaemon.exe", "SentinelTray.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $exe))) {
        Write-Host ("ERROR: {0} is missing from the build output." -f $exe) -ForegroundColor Red
        exit 2
    }
}

Write-Host ("== Installing {0}" -f $AppName)

# 1. program files ------------------------------------------------------------
Write-Host ("[1/6] copying program to {0}" -f $InstallDir)
Stop-Running
if ((Test-Path -LiteralPath $InstallDir) -and
    (Test-Path -LiteralPath (Join-Path $InstallDir "CDriveSentinel.exe"))) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
}
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item -Path (Join-Path $Source "*") -Destination $InstallDir -Recurse -Force

# 2. data folder --------------------------------------------------------------
$existing = Get-PointerTarget
$target = $DataDir
if (-not $target) { $target = $existing }
if (-not $target) { $target = Get-DefaultDataDir }

$usingDefault = $false
if (-not $target) {
    # No second volume: the data stays in ProgramData. Say so, because that is
    # the one case where this tool does occupy C:.
    $target = $PtrDir
    $usingDefault = $true
    Write-Host "[2/6] no non-C: volume found; data will live in ProgramData" -ForegroundColor Yellow
    Write-Host "      (its subtree is excluded from scanning, but the space is real)"
} else {
    Write-Host ("[2/6] data folder: {0}" -f $target)
}

foreach ($sub in @("config", "data", "data\logs", "data\runtime", "data\snapshots", "reports\daily")) {
    New-Item -ItemType Directory -Path (Join-Path $target $sub) -Force | Out-Null
}

# 3. permissions --------------------------------------------------------------
Write-Host "[3/6] granting the tray write access to runtime\ and logs\"
Grant-UsersModify (Join-Path $target "data\runtime")
Grant-UsersModify (Join-Path $target "data\logs")

# 4. pointer ------------------------------------------------------------------
New-Item -ItemType Directory -Path $PtrDir -Force | Out-Null
if ($usingDefault) {
    Remove-Item -LiteralPath $PtrFile -Force -ErrorAction SilentlyContinue
    Write-Host "[4/6] data folder is the default location; no pointer written"
} else {
    Set-Content -LiteralPath $PtrFile -Value $target -Encoding ASCII
    Write-Host ("[4/6] pointer: {0} -> {1}" -f $PtrFile, $target)
}

# 5. initial config -----------------------------------------------------------
$dbPath = Join-Path $target "data\sentinel.db"
if (Test-Path -LiteralPath $dbPath) {
    Write-Host "[5/6] existing database found; leaving its configuration alone"
} else {
    Write-Host "[5/6] writing the initial configuration"
    $rc = 0
    $proc = Start-Process -FilePath $CliExe -ArgumentList @("setup", "--profile", "standard", "--area", "full", "--yes") -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow
    $rc = $proc.ExitCode
    if ($rc -ne 0) {
        Write-Host ("  WARNING: setup returned {0}; run it again manually if the panel looks empty" -f $rc) -ForegroundColor Yellow
    }
}

# 6. both tasks + tray autostart -----------------------------------------------
Write-Host "[6/6] registering both scheduled tasks and the tray autostart"
# The elevated collector. -Start launches the first scan right away.
$taskScript = Join-Path $InstallDir "install_task.ps1"
if (Test-Path -LiteralPath $taskScript) {
    $p = Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-ExecutionPolicy", "Bypass", "-NoProfile", "-WindowStyle", "Hidden",
                        "-File", ('"{0}"' -f $taskScript), "-Start") `
        -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) {
        Write-Host ("  WARNING: task registration returned {0}" -f $p.ExitCode) -ForegroundColor Yellow
    }
} else {
    Write-Host "  WARNING: install_task.ps1 not found; register the task manually" -ForegroundColor Yellow
}
# The daily-report popup runs at NORMAL privilege (a browser launched from an
# elevated process is refused by Chrome/Edge). No -Start: it fires at logon.
$popupScript = Join-Path $InstallDir "install_popup_task.ps1"
if (Test-Path -LiteralPath $popupScript) {
    $p = Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-ExecutionPolicy", "Bypass", "-NoProfile", "-WindowStyle", "Hidden",
                        "-File", ('"{0}"' -f $popupScript)) `
        -WorkingDirectory $InstallDir -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) {
        Write-Host ("  WARNING: popup task registration returned {0}" -f $p.ExitCode) -ForegroundColor Yellow
    }
} else {
    Write-Host "  WARNING: install_popup_task.ps1 not found; no daily report popup" -ForegroundColor Yellow
}

if (-not (Test-Path -LiteralPath $RunKey)) { New-Item -Path $RunKey -Force | Out-Null }
Set-ItemProperty -Path $RunKey -Name $RunValue -Value ('"{0}"' -f $TrayExe)

Write-Host ""
Write-Host "Installed."
Write-Host ("  program : {0}" -f $InstallDir)
Write-Host ("  data    : {0}" -f $target)
Write-Host ("  verify  : & '{0}' paths" -f $CliExe)
Write-Host ""
if (-not $NoPrompt) {
    $answer = Read-Host "Open the tray panel now? [Y/n]"
    if ($answer -ne "n" -and $answer -ne "N") {
        Start-Process -FilePath $TrayExe
    }
}
exit 0
