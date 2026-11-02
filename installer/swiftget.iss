; SwiftGet Windows installer (Inno Setup 6)
; Build:  ISCC.exe /DMyAppVersion=1.0.0 installer\swiftget.iss
; Expects the PyInstaller one-folder build in ..\dist\SwiftGet\

#define MyAppName "SwiftGet"
#define MyAppExe "SwiftGet.exe"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{8C1F3A52-6B7E-4D2A-9E61-2F4B7C9D1A35}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} Download Manager {#MyAppVersion}
AppPublisher=SwiftGet
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=..\dist-installer
OutputBaseFilename=SwiftGet-Setup
SetupIconFile=..\assets\swiftget.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName} Download Manager
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
#ifdef SIGN
; the "swiftsign" tool is passed on the command line: ISCC /DSIGN "/Sswiftsign=cmd /c sign.cmd $f"
SignTool=swiftsign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "&Start SwiftGet when Windows starts (recommended - catches browser downloads)"; GroupDescription: "Startup:"

[Files]
Source: "..\dist\SwiftGet\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\browser-extension\*"; DestDir: "{app}\browser-extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\SwiftGet-firefox.xpi"; DestDir: "{app}\firefox"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autoprograms}\{#MyAppName} browser extension folder"; Filename: "{app}\browser-extension"
Name: "{autoprograms}\{#MyAppName} Firefox add-on folder"; Filename: "{app}\firefox"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SwiftGet"; ValueData: """{app}\{#MyAppExe}"" --tray"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch SwiftGet now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#MyAppExe}"; Flags: runhidden; RunOnceId: "StopSwiftGet"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The app can also add itself to start-up from its Settings; always clean that up.
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'SwiftGet');
end;
