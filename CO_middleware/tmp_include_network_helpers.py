from pathlib import Path
root = Path(r'C:\Github\catalytics-india-middleware\CO_middleware')
path = root / 'build_release.bat'
text = path.read_text(encoding='utf-8')
old = '''copy /y "README_CLIENT_SETUP.txt" "%RELEASE_DIR%\" >nul
if exist "RELEASE_NOTES.md" copy /y "RELEASE_NOTES.md" "%RELEASE_DIR%\" >nul
copy /y "Start_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Install_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\" >nul
'''
new = '''copy /y "README_CLIENT_SETUP.txt" "%RELEASE_DIR%\" >nul
if exist "RELEASE_NOTES.md" copy /y "RELEASE_NOTES.md" "%RELEASE_DIR%\" >nul
copy /y "Start_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Install_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "allow_firewall.bat" "%RELEASE_DIR%\" >nul
copy /y "check_network_access.py" "%RELEASE_DIR%\" >nul
'''
if old not in text:
    raise SystemExit('copy block not found in build_release.bat')
text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8')
print('Updated build_release.bat to include firewall/network helpers')
