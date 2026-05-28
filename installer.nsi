Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "Bot Manager"
OutFile "C:\Temp\BotManager_Setup.exe"
InstallDir "$PROGRAMFILES64\Bot Manager"
InstallDirRegKey HKLM "Software\BotManager" "Install_Dir"
RequestExecutionLevel admin

!define MUI_ICON "C:\Temp\bm_icon.ico"
!define MUI_UNICON "C:\Temp\bm_icon.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "${NSISDIR}\Contrib\Graphics\Wizard\win.bmp"
!define MUI_ABORTWARNING
!define MUI_LANGDLL_ALLLANGUAGES

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\BotManager.exe"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"

VIProductVersion "1.2.1.0"
VIAddVersionKey /LANG=0 "ProductName" "Bot Manager"
VIAddVersionKey /LANG=0 "FileVersion" "1.2.1"
VIAddVersionKey /LANG=0 "ProductVersion" "1.2.1"
VIAddVersionKey /LANG=0 "FileDescription" "Bot Manager Installer"

Section "Bot Manager" SecMain
  SectionIn RO

  ; Завершаем процесс
  nsExec::Exec 'taskkill /f /im BotManager.exe'
  Sleep 1000

  ; ── Сохраняем конфиг из ЛЮБОГО старого места ──────────────────────────────
  ; 1. Из старой папки C:\Temp\BotManager_dist
  ${If} ${FileExists} "C:\Temp\BotManager_dist\manager_config.json"
    CopyFiles "C:\Temp\BotManager_dist\manager_config.json" "$TEMP\bm_config_backup.json"
  ${EndIf}
  ; 2. Из предыдущего места установки (реестр)
  ReadRegStr $0 HKLM "Software\BotManager" "Install_Dir"
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0\manager_config.json"
    CopyFiles "$0\manager_config.json" "$TEMP\bm_config_backup.json"
  ${EndIf}

  ; ── Удаляем старые установки ───────────────────────────────────────────────
  ; Старая папка C:\Temp\BotManager_dist
  RMDir /r "C:\Temp\BotManager_dist"
  ; Папка из реестра (предыдущий InstallDir)
  ${If} $0 != ""
  ${AndIf} $0 != "$INSTDIR"
    RMDir /r "$0"
  ${EndIf}
  ; Чистим ВСЕ старые ключи реестра (любые варианты названий)
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Bot Manager"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager_1.0.8"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager_1.0.7"

  ; ── Устанавливаем файлы ────────────────────────────────────────────────────
  SetOutPath "$INSTDIR"
  File "C:\Temp\BotManager_build\BotManager.exe"
  SetOutPath "$INSTDIR\_internal"
  File /r "C:\Temp\BotManager_build\_internal\*.*"

  ; ── Восстанавливаем конфиг ─────────────────────────────────────────────────
  ${If} ${FileExists} "$TEMP\bm_config_backup.json"
    CopyFiles "$TEMP\bm_config_backup.json" "$INSTDIR\manager_config.json"
    Delete "$TEMP\bm_config_backup.json"
  ${EndIf}

  ; ── Реестр ─────────────────────────────────────────────────────────────────
  WriteRegStr HKLM "Software\BotManager" "Install_Dir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "DisplayName" "Bot Manager"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "DisplayIcon" "$INSTDIR\BotManager.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "Publisher" "Lesha"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager" "DisplayVersion" "1.2.1"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; ── Ярлыки ─────────────────────────────────────────────────────────────────
  CreateDirectory "$SMPROGRAMS\Bot Manager"
  CreateShortcut "$SMPROGRAMS\Bot Manager\Bot Manager.lnk" "$INSTDIR\BotManager.exe" "" "$INSTDIR\_internal\icon.ico"
  CreateShortcut "$DESKTOP\Bot Manager.lnk" "$INSTDIR\BotManager.exe" "" "$INSTDIR\_internal\icon.ico"
SectionEnd

Section "Uninstall"
  nsExec::Exec 'taskkill /f /im BotManager.exe'
  Sleep 500
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\Bot Manager\Bot Manager.lnk"
  RMDir "$SMPROGRAMS\Bot Manager"
  Delete "$DESKTOP\Bot Manager.lnk"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BotManager"
  DeleteRegKey HKLM "Software\BotManager"
SectionEnd
