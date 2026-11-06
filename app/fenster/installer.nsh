; Extra steps for the Windows installer (electron-builder NSIS, one click, per user, no admin rights).
; The build runs on Linux and Mac without rcedit, so "Open Libary.exe" keeps Electron's icon. The shortcuts therefore
; take the app icon from the copy the installer puts next to the app (uninstallerIcon.ico, also used in Apps & features).
!macro customInstall
  ${if} ${FileExists} "$newStartMenuLink"
    CreateShortCut "$newStartMenuLink" "$appExe" "" "$INSTDIR\uninstallerIcon.ico" 0 "" "" "${APP_DESCRIPTION}"
    WinShell::SetLnkAUMI "$newStartMenuLink" "${APP_ID}"
  ${endIf}
  ${if} ${FileExists} "$newDesktopLink"
    CreateShortCut "$newDesktopLink" "$appExe" "" "$INSTDIR\uninstallerIcon.ico" 0 "" "" "${APP_DESCRIPTION}"
    WinShell::SetLnkAUMI "$newDesktopLink" "${APP_ID}"
  ${endIf}
  ClearErrors
!macroend
