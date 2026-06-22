"""
Auto-updater for Catalytics Middleware
=======================================
- Checks the update server every 5 minutes
- Uses category (1-4) + version for update matching
- Safe update: NEVER touches .env, SQLite DB, logs, automation_state.json
- Windows only: uses a .bat launcher to swap the EXE while it is not running

Server API contract
-------------------
GET {UPDATE_SERVER_URL}/version/middleware/check-update?category=<n>&version=<x.y.z>

Response (200 OK):
{
    "update_available": true | false,
    "latest_version":  "1.2.3",
    "download_url":    "https://your-server/releases/cat1/v1.2.3/dashboard.exe",
    "checksum":        "sha256:abcdef..."   // optional but recommended
}
"""

import hashlib
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import json as _json
except ImportError:
    _json = None  # shouldn't happen, but guard against edge cases

try:
    import requests as _requests
except ImportError:
    _requests = None

logger = logging.getLogger("auto_updater")

_UPDATE_CHECK_INTERVAL = 300  # seconds (5 minutes)


def _setup_log_file(exe_dir: Path):
    """Add a dedicated rotating log file for the auto_updater logger."""
    log_dir = exe_dir / 'logs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    log_path = log_dir / 'auto_updater.log'
    for h in logger.handlers:
        if getattr(h, 'baseFilename', None) == str(log_path):
            return
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8'
        )
        fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(message)s'))
        fh.name = 'auto_updater_file'
        logger.addHandler(fh)
        logger.info("[auto_updater] Log file: %s", log_path)
    except Exception as exc:
        logger.warning("[auto_updater] Could not create log file %s: %s", log_path, exc)


_updater_thread = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_version_info() -> dict:
    """
    Read version.json.
    When frozen (PyInstaller EXE):
      - First checks an external version.json next to the EXE (written by the
        auto-updater after each successful update so the version is always current).
      - Falls back to the one bundled in sys._MEIPASS.
    When running as a plain script it lives next to this file.
    """
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        external = exe_dir / 'version.json'
        if external.exists():
            try:
                with open(external, 'r', encoding='utf-8') as fh:
                    data = _json.load(fh)
                if data.get('version') and data.get('category'):
                    return data
            except Exception:
                pass
        base = Path(getattr(sys, '_MEIPASS', exe_dir))
    else:
        base = Path(__file__).parent

    version_file = base / 'version.json'
    try:
        with open(version_file, 'r', encoding='utf-8') as fh:
            return _json.load(fh)
    except Exception as exc:
        logger.error("[auto_updater] Could not read version.json: %s", exc)
        return {}


def _write_version_override(exe_dir: Path, new_version: str):
    """
    Write/update version.json next to the EXE with the new version so the
    freshly-launched process reads the correct version and does not loop.
    Preserves the 'category' and any other fields from the existing file.
    """
    version_file = exe_dir / 'version.json'
    existing: dict = {}
    # Try the external file first
    if version_file.exists():
        try:
            with open(version_file, 'r', encoding='utf-8') as fh:
                existing = _json.load(fh)
        except Exception:
            pass
    # Fall back to _MEIPASS copy for fields like 'category'
    if 'category' not in existing and getattr(sys, 'frozen', False):
        meipass_file = Path(getattr(sys, '_MEIPASS', exe_dir)) / 'version.json'
        if meipass_file.exists():
            try:
                with open(meipass_file, 'r', encoding='utf-8') as fh:
                    existing.update({k: v for k, v in _json.load(fh).items() if k not in existing})
            except Exception:
                pass
    existing['version'] = new_version
    try:
        with open(version_file, 'w', encoding='utf-8') as fh:
            _json.dump(existing, fh, indent=2)
        logger.info("[auto_updater] version.json written → version=%s at %s", new_version, version_file)
    except Exception as exc:
        logger.warning("[auto_updater] Could not write version.json override: %s", exc)


def _derive_update_server_url(server_url: str | None = None) -> str:
    """
    Resolve the update-server base URL.

    Priority:
      1. Explicit server_url argument
      2. UPDATE_SERVER_URL env var
      3. CATALYTICS_API_BASE_URL env var, normalized back to the server root
      4. CATALYTICS_API_BASE env var, normalized back to the server root
    """
    explicit = (server_url or "").strip()
    if explicit:
        return explicit.rstrip("/")

    env_url = os.environ.get('UPDATE_SERVER_URL', '').strip()
    if env_url:
        return env_url.rstrip("/")

    api_base = os.environ.get('CATALYTICS_API_BASE_URL', '').strip().rstrip("/")
    if not api_base:
        api_base = os.environ.get('CATALYTICS_API_BASE', '').strip().rstrip("/")
    if not api_base:
        return ""

    for suffix in ('/api/import', '/import/api', '/import', '/api'):
        if api_base.endswith(suffix):
            return api_base[:-len(suffix)].rstrip("/")
    return api_base


def _check_for_update(server_url: str, category: int, version: str):
    """
    Call the update server.
    Returns the parsed JSON dict on success, None on any failure.
    """
    if _requests is None:
        logger.warning("[auto_updater] 'requests' package not available")
        return None
    base = server_url.rstrip('/')
    tried = []
    for url in (
        base + '/version/middleware/check-update',
        base + '/api/version/middleware/check-update',
    ):
        if url in tried:
            continue
        tried.append(url)
        try:
            resp = _requests.get(url, params={'category': category, 'version': version}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            logger.info("[auto_updater] Update check HTTP %s from %s", resp.status_code, url)
        except Exception as exc:
            logger.info("[auto_updater] Update check failed for %s: %s", url, exc)

    logger.warning("[auto_updater] Update check failed for all candidate URLs: %s", ', '.join(tried))
    return None


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _download_file(url: str, dest: Path) -> bool:
    """Stream-download url → dest.  Returns True on success."""
    if _requests is None:
        return False
    try:
        logger.info("[auto_updater] Downloading update from %s", url)
        with _requests.get(url, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            with open(dest, 'wb') as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        logger.info("[auto_updater] Download complete → %s", dest)
        return True
    except Exception as exc:
        logger.error("[auto_updater] Download failed: %s", exc)
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _apply_update_windows(pending_exe: Path, current_exe: Path, new_version: str):
    """
    Write a self-deleting .bat script that:
      1. Waits for this process (by PID) to exit
      2. Moves pending_exe → current_exe  (only the EXE is replaced)
      3. Starts the new EXE
      4. Deletes itself

    Data files (.env, *.sqlite, logs/, automation_state.json) are never touched.
    """
    pid = os.getpid()
    bat_path = current_exe.parent / '_mw_updater.bat'

    lines = [
        '@echo off',
        f':: Catalytics Middleware auto-updater  -  upgrading to {new_version}',
        '',
        ':: ---- Wait for the old process to exit ----',
        ':wait_loop',
        f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL',
        'if not errorlevel 1 (',
        '    timeout /t 1 /nobreak >nul',
        '    goto wait_loop',
        ')',
        '',
        ':: ---- Extra wait so Windows fully releases the EXE file lock ----',
        'timeout /t 3 /nobreak >nul',
        '',
        ':: ---- Replace old EXE (retry up to 15x if still locked) ----',
        'set RETRIES=0',
        ':move_loop',
        f'move /Y "{pending_exe}" "{current_exe}" >nul 2>&1',
        'if not errorlevel 1 goto move_ok',
        'set /a RETRIES+=1',
        'if %RETRIES% GEQ 15 (',
        '    echo [ERROR] Failed to replace EXE after 15 attempts - check permissions',
        '    del "%~f0"',
        '    exit /b 1',
        ')',
        'timeout /t 1 /nobreak >nul',
        'goto move_loop',
        ':move_ok',
        '',
        ':: ---- Wait for file system to settle ----',
        'timeout /t 2 /nobreak >nul',
        '',
        ':: ---- Start the new version ----',
        f'cd /d "{current_exe.parent}"',
        f'powershell -WindowStyle Hidden -Command "Start-Process \'{current_exe}\' -WorkingDirectory \'{current_exe.parent}\'"',
        '',
        ':: ---- Clean up this script ----',
        'del "%~f0"',
    ]

    bat_path.write_text('\r\n'.join(lines), encoding='utf-8')
    logger.info(
        "[auto_updater] Updater script written to %s — current process will exit now", bat_path
    )

    subprocess.Popen(
        ['cmd.exe', '/c', str(bat_path)],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )

    # Brief pause so the bat process has time to start before we disappear
    time.sleep(1)
    os._exit(0)


def _perform_update(update_info: dict, current_exe: Path):
    """Orchestrate download → verify → apply."""
    download_url = update_info.get('download_url', '').strip()
    new_version = update_info.get('latest_version', 'unknown')
    expected_checksum = update_info.get('checksum', '')  # e.g. "sha256:abcdef..."

    if not download_url:
        logger.error("[auto_updater] update_info has no download_url — skipping")
        return

    pending_exe = current_exe.parent / (current_exe.stem + '_pending.exe')

    if not _download_file(download_url, pending_exe):
        return

    # Validate that the downloaded file is a valid Windows PE executable
    try:
        with open(pending_exe, 'rb') as _f:
            _magic = _f.read(2)
        if _magic != b'MZ':
            logger.error(
                "[auto_updater] Downloaded file is not a valid Windows EXE (bad magic bytes: %r) — aborting update",
                _magic,
            )
            pending_exe.unlink(missing_ok=True)
            return
    except Exception as exc:
        logger.error("[auto_updater] Could not validate downloaded EXE: %s — aborting", exc)
        pending_exe.unlink(missing_ok=True)
        return

    # Optional checksum verification
    if expected_checksum:
        algo, _, expected_hash = expected_checksum.partition(':')
        if algo.lower() == 'sha256':
            actual = _compute_sha256(pending_exe)
            if actual != expected_hash:
                logger.error(
                    "[auto_updater] Checksum mismatch! expected=%s got=%s — aborting update",
                    expected_hash, actual,
                )
                pending_exe.unlink(missing_ok=True)
                return
            logger.info("[auto_updater] Checksum verified OK")

    # Write the new version to version.json next to the EXE BEFORE launching
    # the bat script.  This ensures the freshly-started EXE reads the correct
    # version and does not trigger another update cycle, even if the developer
    # forgot to update version.json inside the build.
    _write_version_override(current_exe.parent, new_version)

    logger.info("[auto_updater] Applying update → version %s", new_version)
    _apply_update_windows(pending_exe, current_exe, new_version)


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _update_loop(server_url: str, category: int, version: str):
    logger.info(
        "[auto_updater] Started  category=%s  version=%s  server=%s",
        category, version, server_url,
    )

    while not _stop_event.is_set():
        try:
            result = _check_for_update(server_url, category, version)

            if result and result.get('update_available'):
                new_ver = result.get('latest_version', '?')
                logger.info("[auto_updater] Update available: %s → %s", version, new_ver)

                if getattr(sys, 'frozen', False):
                    _perform_update(result, Path(sys.executable))
                    return  # _perform_update calls os._exit; this is just a safety return
                else:
                    logger.info(
                        "[auto_updater] Running as script (not frozen EXE) — "
                        "EXE replacement skipped.  New version available: %s",
                        new_ver,
                    )
            else:
                logger.debug("[auto_updater] No update (current: %s)", version)

        except Exception as exc:
            logger.error("[auto_updater] Unexpected error: %s", exc)

        _stop_event.wait(_UPDATE_CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(server_url: str = None):
    """
    Start the auto-updater background thread.

    server_url  — base URL of the update server.
                  If omitted, falls back to the UPDATE_SERVER_URL environment variable.
                  If neither is set, auto-update is silently disabled.
    """
    global _updater_thread

    if _updater_thread and _updater_thread.is_alive():
        return  # Already running

    effective_url = _derive_update_server_url(server_url)

    # Set up dedicated log file early so all subsequent messages land there too
    if getattr(sys, 'frozen', False):
        _setup_log_file(Path(sys.executable).parent)
    else:
        _setup_log_file(Path(__file__).parent)

    if not effective_url:
        logger.info(
            "[auto_updater] No update server configured "
            "(UPDATE_SERVER_URL / CATALYTICS_API_BASE_URL / CATALYTICS_API_BASE missing) "
            "— auto-update disabled"
        )
        return

    info = _get_version_info()
    category = info.get('category')
    version = str(info.get('version', '0.0.0'))

    if not category:
        logger.warning("[auto_updater] version.json missing 'category' field — auto-update disabled")
        return

    _stop_event.clear()
    _updater_thread = threading.Thread(
        target=_update_loop,
        args=(effective_url, int(category), version),
        name='auto_updater',
        daemon=True,
    )
    _updater_thread.start()


def stop():
    """Signal the auto-updater thread to stop at the next check interval."""
    _stop_event.set()
