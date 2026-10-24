; Inno Setup script: turns dist\CallCaptions into dist\CallCaptions-Setup-<version>.exe.
; Installs per user (no admin prompt). Built by build.ps1.

#define AppName "Call Captions"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{FFE85341-BFAB-4DB1-BC26-952C95B0AAB0}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=CallCaptions-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\CallCaptions.exe
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Capturing just Discord's audio needs Windows 10 version 2004 or newer.
MinVersion=10.0.19041
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start {#AppName} when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\..\dist\CallCaptions\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\CallCaptions.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\CallCaptions.exe"; Tasks: desktopicon

[Registry]
; Same value the app's own "Start with Windows" menu item toggles.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CallCaptions"; ValueData: """{app}\CallCaptions.exe"""; Tasks: startup

[Run]
Filename: "{app}\CallCaptions.exe"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Settings, log, downloaded speech models and GPU libraries.
Type: filesandordirs; Name: "{localappdata}\CallCaptions"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'CallCaptions');
end;
