; Inno Setup script for PyVNC Viewer. Build the app first (scripts\build_client_windows.bat),
; then compile:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer.iss

#define AppName "PyVNC Viewer"
#define AppVersion "1.0.0"
#define AppExe "PyVNC Viewer.exe"

[Setup]
AppId={{6F3C2B1E-7A44-4D2E-9B8E-5C1D0A9F4E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PyVNC
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=PyVNCViewer-Setup
SetupIconFile=..\assets\PyVNC.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
