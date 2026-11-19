#define AppName "OfflinePDF Translate"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppPublisher "OfflinePDF Translate"
#define SourceRoot AddBackslash(SourcePath) + ".."

[Setup]
AppId={{AA1E57AF-2184-4CA3-ACB2-13017E78CC9D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\OfflinePDF Translate
DefaultGroupName={#AppName}
UsePreviousAppDir=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
AppMutex=OfflinePDFTranslateAppMutex
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
OutputDir={#SourceRoot}\dist
#ifdef SplitInstaller
OutputBaseFilename=OfflinePDF-Translate-Setup-Split
#else
OutputBaseFilename=OfflinePDF-Translate-Setup
#endif
SetupIconFile={#SourceRoot}\assets\icon.ico
UninstallDisplayIcon={app}\assets\icon.ico
Compression=lzma2/normal
SolidCompression=no
#ifdef SplitInstaller
DiskSpanning=yes
DiskSliceSize=1800000000
#endif
WizardStyle=modern
DisableProgramGroupPage=yes
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=auto

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl,{#SourceRoot}\scripts\ChineseSimplified.isl"
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
english.CreateDesktopIcon=Create a &desktop shortcut
english.ShortcutsGroup=Additional shortcuts:
english.LaunchApp=Launch {#AppName}

chinesesimplified.CreateDesktopIcon=创建桌面快捷方式(&D)
chinesesimplified.ShortcutsGroup=附加快捷方式:
chinesesimplified.LaunchApp=启动 {#AppName}

japanese.CreateDesktopIcon=デスクトップにショートカットを作成する(&D)
japanese.ShortcutsGroup=追加のショートカット:
japanese.LaunchApp={#AppName} を起動する

korean.CreateDesktopIcon=바탕 화면에 바로 가기 만들기(&D)
korean.ShortcutsGroup=추가 바로 가기:
korean.LaunchApp={#AppName} 실행

russian.CreateDesktopIcon=Создать значок на &рабочем столе
russian.ShortcutsGroup=Дополнительные значки:
russian.LaunchApp=Запустить {#AppName}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:ShortcutsGroup}"; Flags: checkedonce

[Files]
Source: "{#SourceRoot}\core\*"; DestDir: "{app}\core"; Excludes: "__pycache__\*,*.pyc"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SourceRoot}\models\*"; DestDir: "{app}\models"; Flags: recursesubdirs createallsubdirs ignoreversion nocompression
Source: "{#SourceRoot}\assets\*"; DestDir: "{app}\assets"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SourceRoot}\runtime\python\*"; DestDir: "{app}\runtime\python"; Excludes: "__pycache__\*,*.pyc,*.pyo,Lib\site-packages\bin\ruff*"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SourceRoot}\runtime\llama.cpp\llama-server.exe"; DestDir: "{app}\runtime\llama.cpp"; Flags: ignoreversion
Source: "{#SourceRoot}\runtime\llama.cpp\*.dll"; DestDir: "{app}\runtime\llama.cpp"; Excludes: "ggml-rpc.dll,llama-batched-bench-impl.dll,llama-bench-impl.dll,llama-cli-impl.dll,llama-completion-impl.dll,llama-fit-params-impl.dll,llama-perplexity-impl.dll,llama-quantize-impl.dll"; Flags: ignoreversion
Source: "{#SourceRoot}\runtime\llama.cpp\LICENSE*"; DestDir: "{app}\runtime\llama.cpp"; Flags: ignoreversion
Source: "{#SourceRoot}\gui.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\cli.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceRoot}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: "-B ""{app}\gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: "-B ""{app}\gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\python\pythonw.exe"; Parameters: "-B ""{app}\gui.py"""; WorkingDir: "{app}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Clean managed program directories before extracting the new version so removed/renamed files,
; old runtime DLLs/packages, or superseded GGUF models never linger across upgrades.
; User data lives outside {app} (%LOCALAPPDATA%\OfflinePDF Translate) and is preserved.
Type: filesandordirs; Name: "{app}\core"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\assets"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\启动.bat"
; Remove legacy shortcuts from previous product names on upgrade.
Type: files; Name: "{autoprograms}\PDF2zh Local.lnk"
Type: files; Name: "{autodesktop}\PDF2zh Local.lnk"
Type: files; Name: "{autoprograms}\LitMTrans Local.lnk"
Type: files; Name: "{autodesktop}\LitMTrans Local.lnk"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\core"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\assets"
Type: filesandordirs; Name: "{app}\models"

[Code]
procedure StopRunningAppProcesses();
var
  AppDir: String;
  PowerShellExe: String;
  Cmd: String;
  ResultCode: Integer;
begin
  AppDir := ExpandConstant('{app}');
  if not DirExists(AppDir) then
    Exit;
  PowerShellExe := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  if not FileExists(PowerShellExe) then
    Exit;
  StringChangeEx(AppDir, '''', '''''', True);
  Cmd := '-NoProfile -NonInteractive -WindowStyle Hidden -Command "' +
    '$prefix = ''' + AppDir + '\''; ' +
    'Get-CimInstance Win32_Process -Filter \"Name=''llama-server.exe'' OR Name=''pythonw.exe'' OR Name=''python.exe''\" ' +
    '| Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) } ' +
    '| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  Exec(PowerShellExe, Cmd, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningAppProcesses();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningAppProcesses();
end;
