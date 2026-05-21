Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "Bot Manager"
OutFile "C:\Users\Lеша\Desktop\jkhk\dist\BotManager_Setup.exe"
InstallDir "$PROGRAMFILES64\Bot Manager"
InstallDirRegKey HKLM "Software\BotManager" "Install_Dir"
RequestExecutionLevel admin

!define MUI_ICON "C:\Users\Lеша\Desktop\jkhk\icon.ico"
!define MUI_UNICON "C:\Users\Lеша\Desktop\jkhk\icon.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${NSISDIR}\Contrib\Graphics\Wizard\win.bmp"
!define MUI_ABORTWARNING
!define MUI_LANGDLL_ALLLANGUAGES

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\BotManager.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить Bot Manager"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"

VIProductVersion "1.0.7.0"
VIAddVersionKey /LANG=0 "ProductName" "Bot Manager"
VIAddVersionKey /LANG=0 "FileVersion" "1.0.7"
VIAddVersionKey /LANG=0 "ProductVersion" "1.0.7"
VIAddVersionKey /LANG=0 "FileDescription" "Bot Manager Installer"

Section "Bot Manager" SecMain
  SectionIn RO
  ; Завершаем запущенный процесс если есть
  nsExec::Exec 'taskkill /f /im BotManager.exe'
  Sleep 1000
  SetOutPath "$INSTDIR"
  File "C:\Users\Lеша\Desktop\jkhk\dist\BotManager\BotManager.exe"
  SetOutPath "$INSTDIR\_internal"
  File /r "C:\Users\Lеша\Desktop\jkhk\dist\BotManager\_internal\*.*"

  WriteRegStr HKLM "Software\BotManager" "Install_Dir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "DisplayName" "Bot Manager"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "DisplayIcon" "$INSTDIR\BotManager.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "Publisher" "Lesha"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "DisplayVersion" "1.0.7"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\Bot Manager"
  CreateShortcut "$SMPROGRAMS\Bot Manager\Bot Manager.lnk" "$INSTDIR\BotManager.exe" "" "$INSTDIR\_internal\icon.ico"
  CreateShortcut "$DESKTOP\Bot Manager.lnk" "$INSTDIR\BotManager.exe" "" "$INSTDIR\_internal\icon.ico"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\Bot Manager\Bot Manager.lnk"
  RMDir "$SMPROGRAMS\Bot Manager"
  Delete "$DESKTOP\Bot Manager.lnk"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager"
  DeleteRegKey HKLM "Software\BotManager"
SectionEnd
