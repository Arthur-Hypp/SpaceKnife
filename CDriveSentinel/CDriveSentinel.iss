; CDriveSentinel installer (Inno Setup 6).
;
;   Build:  python make_installer.py       (or: iscc CDriveSentinel.iss)
;   Needs:  dist\CDriveSentinel\  -- run `python build.py` first
;
; Design decisions worth keeping:
;
; * The program goes to Program Files, the DATA goes wherever the user picks,
;   and the default is not the monitored volume. This tool exists to find out
;   what is eating C:; shipping it with 600 MB of its own data on C: would make
;   it part of the answer. The choice is stored as a one-line pointer in
;   %PROGRAMDATA%\CDriveSentinel\home.txt so the collector, the tray and the
;   CLI all resolve the same folder (see p1_config.read_home_pointer).
;
; * The runtime/ and logs/ folders get "users modify". Without that the tray
;   (deliberately un-elevated) cannot write the 立即扫描 / 暂停 request file and
;   the buttons silently do nothing.
;
; * Uninstall asks before deleting data and defaults to keeping it. A year of
;   history is not something to remove because someone re-ran Setup.

#define AppName        "CDriveSentinel"
#define AppVer         "0.3.0"
#define AppPublisher   "SpaceKnife"
#define AppExe         "CDriveSentinel.exe"
#define TrayExe        "SentinelTray.exe"
#define SrcDir         "dist\CDriveSentinel"

[Setup]
AppId={{7C3E1A42-9B5D-4E6F-8A21-3D5C7B9012EF}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\CDriveSentinel
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=CDriveSentinel-{#AppVer}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#TrayExe}
AllowNoIcons=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
; Chinese Simplified ships with Inno 6 as an unofficial language file; using the
; default English messages keeps the build working on a bare Inno install.

[Tasks]
Name: "trayautostart"; Description: "登录时自动启动托盘图标"; GroupDescription: "启动项："; Flags: checkedonce

[Files]
Source: "{#SrcDir}\{#AppExe}";      DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\{#TrayExe}";     DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\_internal\*";    DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SrcDir}\config\*";       DestDir: "{app}\config"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\README.md";      DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\LICENSE";        DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\使用说明.txt";    DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; Both task installers ship from the build output, not the source tree, so the
; compiled Setup.exe is self-contained.
Source: "{#SrcDir}\install_task.ps1";        DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\install_popup_task.ps1";  DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; Bracket the data folder with the permissions the tray needs. Everything else
; (inventory, events, reports) is written by the elevated collector only.
Name: "{code:GetDataDir}\config"
Name: "{code:GetDataDir}\data";             Permissions: users-readexec
Name: "{code:GetDataDir}\data\logs";        Permissions: users-modify
Name: "{code:GetDataDir}\data\runtime";     Permissions: users-modify
Name: "{code:GetDataDir}\data\snapshots";   Permissions: users-readexec
Name: "{code:GetDataDir}\reports\daily";    Permissions: users-readexec

[Registry]
; Per-machine so every account gets a tray. The tray already refuses to start a
; second instance, so this cannot pile up icons.
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "CDriveSentinelTray"; ValueData: """{app}\{#TrayExe}"""; \
    Flags: uninsdeletevalue; Tasks: trayautostart

[Run]
Filename: "{app}\{#TrayExe}"; Description: "打开托盘面板"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Two tasks: the elevated collector and the normal-privilege report popup.
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File ""{app}\install_task.ps1"" -Uninstall"; \
    Flags: runhidden waituntilterminated; RunOnceId: "RemoveCollectorTask"
Filename: "powershell.exe"; \
    Parameters: "-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File ""{app}\install_popup_task.ps1"" -Uninstall"; \
    Flags: runhidden waituntilterminated; RunOnceId: "RemovePopupTask"

[Code]
var
  DataDirPage: TInputDirWizardPage;

function DefaultDataDir(): String;
var
  I: Integer;
  Drv: String;
begin
  { Prefer any volume other than C: -- that is the whole point. }
  for I := Ord('D') to Ord('Z') do
  begin
    Drv := Chr(I) + ':\';
    if DirExists(Drv) then
    begin
      Result := AddBackslash(Drv) + 'CDriveSentinelData';
      Exit;
    end;
  end;
  Result := ExpandConstant('{commonappdata}\CDriveSentinel');
end;

function PointerFolder(): String;
begin
  Result := ExpandConstant('{commonappdata}\CDriveSentinel');
end;

function ExistingPointer(): String;
var
  Lines: TArrayOfString;
  P: String;
begin
  Result := '';
  P := AddBackslash(PointerFolder()) + 'home.txt';
  if FileExists(P) and LoadStringsFromFile(P, Lines) and (GetArrayLength(Lines) > 0) then
    Result := Trim(Lines[0]);
end;

{ Bracket a value: [Dirs] and [Run] both ask for the same folder. }
function GetDataDir(Param: String): String;
begin
  if DataDirPage <> nil then
    Result := RemoveBackslashUnlessRoot(DataDirPage.Values[0])
  else
    Result := RemoveBackslashUnlessRoot(DefaultDataDir());
end;

procedure InitializeWizard();
var
  Prev: String;
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    '数据保存位置',
    '程序装在 C 盘，数据建议放在其他盘。',
    '这个工具用来查清 C 盘的空间被什么占掉了，它自己的数据（通常 300–600 MB）' + #13#10 +
    '不应再占 C 盘。选一个目录存放配置与历史记录。',
    False, '');
  DataDirPage.Add('数据文件夹：');

  Prev := ExistingPointer();
  if Prev <> '' then
    DataDirPage.Values[0] := Prev
  else
    DataDirPage.Values[0] := DefaultDataDir();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if (DataDirPage <> nil) and (CurPageID = DataDirPage.ID) then
  begin
    Dir := RemoveBackslashUnlessRoot(DataDirPage.Values[0]);
    if Dir = '' then
    begin
      MsgBox('请选择一个数据文件夹。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Pos(':', Dir) <> 2 then
    begin
      MsgBox('请使用带盘符的完整路径，例如 D:\CDriveSentinelData。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, PtrDir, PtrFile: String;
  Rc: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  DataDir := GetDataDir('');
  PtrDir := PointerFolder();
  PtrFile := AddBackslash(PtrDir) + 'home.txt';

  { 1. The pointer first: everything below resolves the data folder through it. }
  if CompareText(DataDir, RemoveBackslashUnlessRoot(PtrDir)) <> 0 then
  begin
    ForceDirectories(PtrDir);
    SaveStringToFile(PtrFile, DataDir + #13#10, False);
  end
  else
    DeleteFile(PtrFile);   { data sits in the default place, no pointer needed }

  ForceDirectories(AddBackslash(DataDir) + 'data\runtime');
  ForceDirectories(AddBackslash(DataDir) + 'data\logs');
  ForceDirectories(AddBackslash(DataDir) + 'reports\daily');

  { 2. Write the initial config through the CLI, so the profile table, the
       atomic write and the scope rules exist in exactly one place. }
  if not FileExists(AddBackslash(DataDir) + 'data\sentinel.db') then
  begin
    if not Exec(ExpandConstant('{app}\{#AppExe}'),
                'setup --profile standard --area full --yes',
                ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc) then
      MsgBox('初始配置写入失败，可以稍后运行 setup 命令补上。', mbInformation, MB_OK)
    else if Rc <> 0 then
      MsgBox('初始配置写入返回代码 ' + IntToStr(Rc) + '，请查看程序目录下的运行日志。',
             mbInformation, MB_OK);
  end;

  { 3. TWO scheduled tasks, both at logon:
        - the elevated collector, highest privileges, started right away;
        - the daily-report popup, NORMAL privileges (a browser launched from an
          elevated process is refused by Chrome/Edge), fires at logon only. }
  if not Exec('powershell.exe',
              '-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File "'
              + ExpandConstant('{app}\install_task.ps1') + '" -Start',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc) then
    MsgBox('后台采集任务注册失败。可以稍后在程序目录以管理员身份运行 install_task.ps1。',
           mbInformation, MB_OK);

  if not Exec('powershell.exe',
              '-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File "'
              + ExpandConstant('{app}\install_popup_task.ps1') + '"',
              ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc) then
    MsgBox('日报弹窗任务注册失败。可以稍后在程序目录以管理员身份运行 install_popup_task.ps1。',
           mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, PtrFile: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  DataDir := ExistingPointer();
  if DataDir = '' then
    DataDir := RemoveBackslashUnlessRoot(PointerFolder());
  PtrFile := AddBackslash(PointerFolder()) + 'home.txt';

  if DirExists(DataDir) then
    if MsgBox('是否同时删除历史数据？' + #13#10#13#10 + DataDir + #13#10#13#10 +
              '选“否”会保留它，下次安装可以直接接着用。',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(DataDir, True, True, True);
      DeleteFile(PtrFile);
    end;
end;
