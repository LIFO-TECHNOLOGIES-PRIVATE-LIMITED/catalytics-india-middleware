from pathlib import Path
root = Path(r'C:\Github\catalytics-india-middleware\CO_middleware')

build_release = root / 'build_release.bat'
text = build_release.read_text(encoding='utf-8')
old = 'copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\\" >nul\n'
new = 'copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\\" >nul\ncopy /y "allow_firewall.bat" "%RELEASE_DIR%\\" >nul\ncopy /y "check_network_access.py" "%RELEASE_DIR%\\" >nul\n'
if old not in text:
    raise SystemExit('Remove_AutoStart copy line not found')
text = text.replace(old, new, 1)
old_steps = 'echo   5. Use Install_AutoStart.bat if Windows auto-start is required\n'
new_steps = 'echo   5. If remote machines must access the dashboard, set WEB_UI_HOST=0.0.0.0 and run allow_firewall.bat as Administrator\n' \
           'echo   6. Use Install_AutoStart.bat if Windows auto-start is required\n'
if old_steps not in text:
    raise SystemExit('Client deployment steps line not found')
text = text.replace(old_steps, new_steps, 1)
build_release.write_text(text, encoding='utf-8')

readme = root / 'README_CLIENT_SETUP.txt'
text = readme.read_text(encoding='utf-8')
anchor = '4. Dashboard opens automatically in your browser at http://localhost:8787\n\n'
addition = 'NETWORK ACCESS FROM OTHER MACHINES\n--------------------------------\nIf another machine or server must open the dashboard URL:\n1. Set WEB_UI_HOST=0.0.0.0 in .env\n2. Run allow_firewall.bat as Administrator\n3. Access it using http://YOUR_IP:8787\n\nImportant: outgoing access from the EXE to CATALYTICS_API_BASE_URL does not normally need a firewall rule. If that fails, the problem is usually URL/DNS/SSL/proxy/network policy, not EXE permission.\n\n'
if addition not in text:
    if anchor not in text:
        raise SystemExit('README anchor not found')
    text = text.replace(anchor, anchor + addition, 1)
readme.write_text(text, encoding='utf-8')

print('Updated build_release.bat and README_CLIENT_SETUP.txt for network access guidance')
