; Inno Setup script for PyVNC Server. Build the app first (scripts\build_server_windows.bat),
; then compile:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" scripts\installer_server.iss

#define AppName "PyVNC Server"
#define AppVersion "1.0.0"
#define AppExe "PyVNC Server.exe"

[Setup]
AppId={{8E4D1C7A-2B55-4F19-A6C3-91D0E7B84F02}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=PyVNC
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=PyVNCServer-Setup
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
