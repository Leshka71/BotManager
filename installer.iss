#define MyAppName "Bot Manager"
#define MyAppVersion "1.4.1"
#define MyAppPublisher "Lesha"
#define MyAppExeName "BotManager.exe"
#define MyAppDir SourcePath + "dist\BotManager"
#define MyIcon SourcePath + "icon.ico"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#SourcePath}dist
OutputBaseFilename=BotManager_Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
DefaultDirName={commonpf64}\{#MyAppName}
DisableProgramGroupPage=yes
CloseApplications=yes
ShowLanguageDialog=no
SetupIconFile={#MyIcon}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#MyAppDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyAppDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\icon.ico"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\_internal\icon.ico"; Tasks: desktopicon

[Run]
; Без skipifsilent: автообновление из самой программы запускает установщик с
; /VERYSILENT — без него BotManager после тихого обновления не перезапускался
; бы сам, а просто оставался закрытым до следующего ручного запуска.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall
