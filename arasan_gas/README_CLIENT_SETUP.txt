Arasan Gas Middleware - Client Package

Quick Start
1. Extract the zip to any folder (for example: C:\ArasanGas).
2. Edit .env with your real values (Tally URL, API key, companies).
3. Double-click arasan_gas_dashboard.exe.
4. Browser opens automatically at http://localhost:8787.

Startup Behavior (Default in this release)
- No console window (background app window only in browser).
- Automation loops auto-start on app launch.
- Browser auto-opens dashboard URL.
- EXE auto-registers Windows startup (configurable in .env).

Useful Files
- Start_Dashboard.bat: starts dashboard EXE.
- Install_AutoStart.bat: manual fallback to set Windows startup entry.
- Remove_AutoStart.bat: removes Windows auto-start entry.

Notes
- On first run, local SQLite DB is auto-created if missing.
- Logs are written to logs\ folder.
- Required services: Tally (localhost:9000) and Catalytics backend.
