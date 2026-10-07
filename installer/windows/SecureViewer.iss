; Inno Setup script for the pdf-drm Secure PDF Viewer.
; Turns the PyInstaller output (dist\SecureViewer\) into a proper Windows
; installer with a Start Menu entry, optional desktop icon, and an
; uninstaller registered in "Add or remove programs".
;
; Requires Inno Setup 6 (https://jrsoftware.org/isdl.php) and a build
; already produced by scripts\build_windows.ps1 (i.e. dist\SecureViewer\
; must exist before compiling this).
;
; Compile with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\windows\SecureViewer.iss
; (scripts\build_windows.ps1 does this automatically if ISCC.exe is found.)

#define MyAppName "Secure PDF Viewer"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "pdf-drm"
#define MyAppExeName "SecureViewer.exe"

[Setup]
AppId={{0643C303-05B0-4C49-8693-74FAC2B1273E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=https://github.com/
DefaultDirName={autopf}\SecureViewer
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Let the user pick per-machine (needs admin) or per-user (no UAC prompt) --
; useful since this is often installed on machines where the end user isn't
; a local admin.
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=SecureViewer-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\..\viewer\assets\icon.ico
; PySide6 wheels are 64-bit only, so only offer this installer on 64-bit
; Windows. (Use "x64compatible" instead of "x64" if your Inno Setup version
; is 6.3+ and prints a deprecation warning for this line.)
ArchitecturesInstallIn64BitMode=x64
; This installer isn't code-signed -- see the warning printed by
; scripts\build_windows.ps1 / README.md section 6.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\SecureViewer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Not removing %USERPROFILE%\.pdf_drm_viewer on uninstall -- it holds the
; user's saved server URL/name/email and self-service registration state
; (see CONFIG_PATH in viewer/main.py); wiping it on every reinstall would
; force re-registering with the license server for no reason.
