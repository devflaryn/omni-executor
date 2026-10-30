; Omni Executor -- the Windows installer (Inno Setup 6).
;
; WHY INNO SETUP, AND WHY IT CARRIES THE APP. The setup file used to be a
; one-file PyInstaller "stub": a self-extracting Python program that, once
; running, downloaded an executable from a bare IP address, unpacked it,
; wrote an uninstall key and made shortcuts. Every one of those is a behaviour
; antivirus heuristics associate with a trojan DOWNLOADER, and VirusTotal
; engines duly reported it as one. Nothing about the app changed; the shape of
; the installer did. So this is the ordinary shape instead: a standard,
; widely-recognised installer engine, with the build INSIDE it (nothing is
; fetched at install time), a full version resource and publisher details.
;
; Per-user, so it never asks for administrator rights -- same folder as the
; stub used (%LOCALAPPDATA%\Programs\OmniExecutor), so an existing install is
; upgraded in place and the app's own file-by-file updater (updates.py) keeps
; working on it.
;
; Built by build-windows.ps1 -Installer:
;   ISCC /DAppVersion=1.0.0 /DSourceDir=<dist>\omni-exec /DOutputDir=<dist> OmniExecutor.iss

#ifndef AppVersion
  #error AppVersion must be passed: /DAppVersion=x.y.z
#endif
#ifndef SourceDir
  #error SourceDir must be passed: /DSourceDir=<the assembled omni-exec folder>
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

#define AppName "Omni Executor"
#define AppExe "omni-exec.exe"
#define BackendExe "omni-exec-py.exe"

[Setup]
; Never change AppId: it is how Windows and Inno recognise an upgrade.
AppId={{6E7C2A11-5B3D-4C2E-9F0A-0E5C1D8B7A42}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Omni Executor
AppPublisherURL=https://omniexec.net
AppSupportURL=https://omniexec.net
AppCopyright=Copyright (c) 2026 Omni Executor
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=Omni Executor
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\OmniExecutor
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=OmniExecutorSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
; The Restart Manager closes a running copy (window AND backend) so its files
; can be replaced; PrepareToInstall below is the backstop.
CloseApplications=yes
RestartApplications=no
SetupIconFile=..\src-tauri\icons\icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; The previous build's PyInstaller tree. Replaced whole, so a module the new
; build no longer has cannot be loaded instead of nothing.
Type: filesandordirs; Name: "{app}\_internal"
; What the app's own updater parks while swapping files (bootstrap.replace_tree).
Type: filesandordirs; Name: "{app}\.omni-replaced-*"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\.omni-replaced-*"

[Code]
procedure StopApp();
var
  Code: Integer;
begin
  // Both halves: `omni-exec` (the window) does not match `omni-exec-py` (the
  // backend), and the backend is the one holding _internal's DLLs open.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#BackendExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopApp();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    // The old Python stub's own "Apps & features" entry. This installer owns
    // the program now, under its AppId; leaving the stub's entry would list
    // the app twice, and its uninstaller no longer exists.
    RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\OmniExecutor');
end;

function InitializeUninstall(): Boolean;
begin
  StopApp();
  Result := True;
end;
