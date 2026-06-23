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

_UPDATE_CHECK_INTERVAL = 60  # seconds (1 minute)


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
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)-8s %(message)s'))
        fh.name = 'auto_updater_file'
        logger.setLevel(logging.DEBUG)
        logger.addHandler(fh)
        logger.info("[auto_updater] Log file initialised: %s", log_path)
        logger.info("[auto_updater] Python: %s  PID: %d", sys.version.split()[0], os.getpid())
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
      3. CATALYTICS_API_BASE env var, normalized back to the server root

    Examples:
      http://host:8000           -> http://host:8000
      http://host:8000/          -> http://host:8000
      http://host:8000/api       -> http://host:8000
      http://host:8000/import    -> http://host:8000
      http://host:8000/api/import -> http://host:8000
    """
    explicit = (server_url or "").strip()
    if explicit:
        return explicit.rstrip("/")

    env_url = os.environ.get('UPDATE_SERVER_URL', '').strip()
    if env_url:
        return env_url.rstrip("/")

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
        logger.warning("[auto_updater] 'requests' package not available — cannot check for updates")
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
        params = {'category': category, 'version': version}
        logger.debug("[auto_updater] Checking update at %s  params=%s", url, params)
        try:
            resp = _requests.get(url, params=params, timeout=15)
            logger.debug("[auto_updater] Response HTTP %s from %s", resp.status_code, url)
            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    "[auto_updater] Server response: update_available=%s  latest=%s  url=%s",
                    data.get('update_available'), data.get('latest_version'), data.get('download_url'),
                )
                return data
            else:
                logger.info(
                    "[auto_updater] Update check HTTP %s from %s — body: %s",
                    resp.status_code, url, resp.text[:200],
                )
        except Exception as exc:
            logger.warning("[auto_updater] Update check failed for %s: %s", url, exc)

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
        logger.error("[auto_updater] 'requests' package not available — download impossible")
        return False
    try:
        logger.info("[auto_updater] Download starting: %s", url)
        logger.info("[auto_updater] Download destination: %s", dest)
        with _requests.get(url, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            total_bytes = int(resp.headers.get('Content-Length', 0))
            if total_bytes:
                logger.info("[auto_updater] Expected file size: %.2f MB", total_bytes / 1024 / 1024)
            else:
                logger.info("[auto_updater] Content-Length not provided by server")
            downloaded = 0
            last_log_mb = 0
            with open(dest, 'wb') as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        mb = downloaded / 1024 / 1024
                        if int(mb) > last_log_mb:
                            last_log_mb = int(mb)
                            if total_bytes:
                                pct = downloaded * 100 // total_bytes
                                logger.info("[auto_updater] Downloaded %.1f MB / %.1f MB (%d%%)",
                                            mb, total_bytes / 1024 / 1024, pct)
                            else:
                                logger.info("[auto_updater] Downloaded %.1f MB", mb)
        final_size = dest.stat().st_size if dest.exists() else 0
        logger.info("[auto_updater] Download complete → %s  (%.2f MB)", dest, final_size / 1024 / 1024)
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
    Write a self-deleting PowerShell script that:
      1. Waits for this process (by PID) to exit
      2. Kills any other instances of the EXE by name
      3. Moves pending_exe → current_exe with retry (PowerShell Move-Item is more
         reliable than cmd move for files that are briefly locked by antivirus)
      4. Launches the new EXE from the correct working directory
      5. Deletes itself

    Data files (.env, *.sqlite, logs/, automation_state.json) are never touched.
    """
    pid = os.getpid()
    ps_path = current_exe.parent / '_mw_updater.ps1'
    log_file = str(current_exe.parent / 'logs' / 'auto_updater.log').replace('\\', '\\\\')

    def ps_log(msg):
        """Return a PS line that appends msg to the log file with timestamp."""
        return (
            f'[System.IO.File]::AppendAllText("{log_file}", '
            f'("[" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "] {msg}`r`n"))'
        )

    lines = [
        f'# Catalytics Middleware auto-updater — upgrading to {new_version}',
        f'$target_pid = {pid}',
        f'$pending    = \'{pending_exe}\'',
        f'$current    = \'{current_exe}\'',
        f'$work_dir   = \'{current_exe.parent}\'',
        f'$proc_name  = \'{current_exe.stem}\'',
        f'$ps_self    = $MyInvocation.MyCommand.Path',
        f'$log_file   = "{log_file}"',
        '',
        '# Ensure logs dir exists',
        'New-Item -ItemType Directory -Force -Path (Split-Path $log_file) | Out-Null',
        '',
        ps_log(f'PS_UPDATER START — upgrading to {new_version}  PID={pid}  pending=$pending'),
        ps_log('PS_UPDATER current exe: $current'),
        ps_log('PS_UPDATER work dir:    $work_dir'),
        '',
        '# 0. Self-elevate to Administrator if not already — required for Move-Item and Start-Process',
        '$isAdmin = ([Security.Principal.WindowsPrincipal]',
        '            [Security.Principal.WindowsIdentity]::GetCurrent()',
        '           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)',
        ps_log('PS_UPDATER isAdmin=$isAdmin'),
        'if (-not $isAdmin) {',
        f'    {ps_log("PS_UPDATER not admin — re-launching with RunAs (UAC prompt may appear)")}',
        '    Start-Process powershell -ArgumentList ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps_self`"") -Verb RunAs',
        '    exit',
        '}',
        ps_log('PS_UPDATER running as Administrator — proceeding'),
        '',
        '# 1. Wait for the specific old PID to exit',
        ps_log(f'PS_UPDATER STEP 1 — waiting for old process PID {pid} to exit'),
        '$wait_secs = 0',
        'while (Get-Process -Id $target_pid -ErrorAction SilentlyContinue) {',
        '    Start-Sleep -Seconds 1',
        '    $wait_secs++',
        '    if ($wait_secs % 5 -eq 0) {',
        f'        {ps_log("PS_UPDATER still waiting for PID {pid} to exit ($wait_secs s elapsed)")}',
        '    }',
        '}',
        ps_log('PS_UPDATER old process has exited'),
        '',
        '# 2. Kill ALL running instances by name (handles autostart relaunches)',
        ps_log('PS_UPDATER STEP 2 — killing all instances of $proc_name'),
        '$killed = Get-Process -Name $proc_name -ErrorAction SilentlyContinue',
        'if ($killed) {',
        f'    {ps_log("PS_UPDATER killing $($killed.Count) instance(s) of $proc_name")}',
        '    $killed | Stop-Process -Force -ErrorAction SilentlyContinue',
        '} else {',
        f'    {ps_log("PS_UPDATER no extra instances of $proc_name running")}',
        '}',
        'Start-Sleep -Seconds 2',
        '',
        '# 3. Check pending file exists before move',
        ps_log('PS_UPDATER STEP 3 — verifying pending file exists'),
        'if (-not (Test-Path -LiteralPath $pending)) {',
        f'    {ps_log("PS_UPDATER ERROR — pending file not found: $pending")}',
        '    exit 1',
        '}',
        '$pending_size = (Get-Item -LiteralPath $pending).Length',
        ps_log('PS_UPDATER pending file size: $pending_size bytes'),
        '',
        '# 4. Move _old -> current with retry (up to 20 attempts)',
        ps_log('PS_UPDATER STEP 4 — moving pending -> current (up to 20 attempts)'),
        '$moved = $false',
        'for ($i = 0; $i -lt 20; $i++) {',
        '    try {',
        '        Move-Item -Force -LiteralPath $pending -Destination $current -ErrorAction Stop',
        '        $moved = $true',
        f'        {ps_log("PS_UPDATER move succeeded on attempt $($i+1)")}',
        '        break',
        '    } catch {',
        f'        {ps_log("PS_UPDATER move attempt $($i+1) failed: $($_.Exception.Message)")}',
        '        Start-Sleep -Seconds 1',
        '    }',
        '}',
        '',
        '# 5. Launch new version as Administrator so it starts correctly',
        'if ($moved) {',
        ps_log('PS_UPDATER STEP 5 — move succeeded, waiting 2s then launching new EXE'),
        '    Start-Sleep -Seconds 2',
        '    if (Test-Path -LiteralPath $current) {',
        f'        {ps_log("PS_UPDATER launching: $current")}',
        '        Start-Process -FilePath $current -WorkingDirectory $work_dir -Verb RunAs',
        f'        {ps_log("PS_UPDATER new EXE launched successfully")}',
        '    } else {',
        f'        {ps_log("PS_UPDATER ERROR — current EXE missing after move: $current")}',
        '    }',
        '} else {',
        ps_log('PS_UPDATER ERROR — move failed after 20 attempts — old EXE NOT replaced'),
        '}',
        '',
        '# 6. Clean up this script',
        ps_log('PS_UPDATER STEP 6 — cleaning up updater script'),
        'Start-Sleep -Seconds 1',
        'Remove-Item -LiteralPath $ps_self -Force -ErrorAction SilentlyContinue',
        ps_log('PS_UPDATER DONE'),
    ]

    ps_path.write_text('\n'.join(lines), encoding='utf-8')
    logger.info("[auto_updater] PS updater script written: %s", ps_path)
    logger.info("[auto_updater] Spawning PowerShell updater (detached, hidden) then exiting...")

    subprocess.Popen(
        [
            'powershell',
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-WindowStyle', 'Hidden',
            '-File', str(ps_path),
        ],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )

    # Brief pause so PowerShell has time to start before we disappear
    logger.info("[auto_updater] PowerShell spawned — this process will now exit (PID=%d)", os.getpid())
    time.sleep(1)
    os._exit(0)


def _perform_update(update_info: dict, current_exe: Path, current_version: str = ''):
    """Orchestrate download → verify → apply."""
    download_url = update_info.get('download_url', '').strip()
    new_version = update_info.get('latest_version', 'unknown')
    expected_checksum = update_info.get('checksum', '')  # e.g. "sha256:abcdef..."

    logger.info("[auto_updater] === UPDATE STARTED ===")
    logger.info("[auto_updater] Current version : %s", current_version)
    logger.info("[auto_updater] New version      : %s", new_version)
    logger.info("[auto_updater] Download URL     : %s", download_url)
    logger.info("[auto_updater] Current EXE      : %s", current_exe)
    logger.info("[auto_updater] Checksum         : %s", expected_checksum or "not provided")

    if not download_url:
        logger.error("[auto_updater] update_info has no download_url — skipping")
        return

    # Guard: server must not serve the _pending or _old file as an update
    url_filename = Path(download_url.rstrip('/').split('/')[-1]).stem.lower()
    if '_pending' in url_filename or '_old' in url_filename:
        logger.error(
            "[auto_updater] download_url points to a temp file (_pending/_old) — "
            "wrong file uploaded on server: %s  Skipping.", download_url
        )
        return

    # Name the download file with current version + _old so it's easy to identify
    ver_tag = f'_v{current_version}' if current_version else ''
    pending_exe = current_exe.parent / f'{current_exe.stem}{ver_tag}_old.exe'
    logger.info("[auto_updater] Download destination: %s", pending_exe)

    # Remove stale _old file if it already exists
    if pending_exe.exists():
        logger.info("[auto_updater] Removing stale file: %s", pending_exe)
        try:
            pending_exe.unlink()
        except Exception as exc:
            logger.warning("[auto_updater] Could not remove stale file: %s", exc)

    logger.info("[auto_updater] STEP 1/4 — Downloading new EXE")
    if not _download_file(download_url, pending_exe):
        logger.error("[auto_updater] Download failed — update aborted")
        return

    # Validate that the downloaded file is a valid Windows PE executable
    logger.info("[auto_updater] STEP 2/4 — Validating downloaded EXE (MZ magic bytes)")
    try:
        with open(pending_exe, 'rb') as _f:
            _magic = _f.read(2)
        if _magic != b'MZ':
            logger.error(
                "[auto_updater] Downloaded file is NOT a valid Windows EXE "
                "(bad magic bytes: %r) — aborting update", _magic,
            )
            pending_exe.unlink(missing_ok=True)
            return
        logger.info("[auto_updater] EXE validation OK (MZ magic bytes confirmed)")
    except Exception as exc:
        logger.error("[auto_updater] Could not validate downloaded EXE: %s — aborting", exc)
        pending_exe.unlink(missing_ok=True)
        return

    # Optional checksum verification
    if expected_checksum:
        logger.info("[auto_updater] STEP 2b — Verifying checksum: %s", expected_checksum)
        algo, _, expected_hash = expected_checksum.partition(':')
        if algo.lower() == 'sha256':
            logger.info("[auto_updater] Computing SHA256 of downloaded file...")
            actual = _compute_sha256(pending_exe)
            if actual != expected_hash:
                logger.error(
                    "[auto_updater] Checksum MISMATCH! expected=%s  got=%s — aborting update",
                    expected_hash, actual,
                )
                pending_exe.unlink(missing_ok=True)
                return
            logger.info("[auto_updater] Checksum verified OK  sha256=%s", actual)
    else:
        logger.info("[auto_updater] No checksum provided — skipping verification")

    # Write the new version to version.json next to the EXE BEFORE launching
    # the PS script.  This ensures the freshly-started EXE reads the correct
    # version and does not trigger another update cycle.
    logger.info("[auto_updater] STEP 3/4 — Writing version.json override → %s", new_version)
    _write_version_override(current_exe.parent, new_version)

    logger.info("[auto_updater] STEP 4/4 — Launching PowerShell updater script")
    _apply_update_windows(pending_exe, current_exe, new_version)


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _update_loop(server_url: str, category: int, version: str):
    logger.info("[auto_updater] ============================================================")
    logger.info("[auto_updater] Auto-updater thread started")
    logger.info("[auto_updater]   category : %s", category)
    logger.info("[auto_updater]   version  : %s", version)
    logger.info("[auto_updater]   server   : %s", server_url)
    logger.info("[auto_updater]   interval : %ds", _UPDATE_CHECK_INTERVAL)
    logger.info("[auto_updater]   frozen   : %s", getattr(sys, 'frozen', False))
    logger.info("[auto_updater]   exe path : %s", sys.executable)
    logger.info("[auto_updater] ============================================================")

    check_count = 0
    while not _stop_event.is_set():
        check_count += 1
        logger.info("[auto_updater] Check #%d — contacting update server...", check_count)
        try:
            result = _check_for_update(server_url, category, version)

            if result is None:
                logger.warning("[auto_updater] Check #%d — no response from server (will retry in %ds)",
                               check_count, _UPDATE_CHECK_INTERVAL)
            elif result.get('update_available'):
                new_ver = result.get('latest_version', '?')
                logger.info("[auto_updater] Check #%d — UPDATE AVAILABLE: %s → %s",
                            check_count, version, new_ver)

                if getattr(sys, 'frozen', False):
                    _perform_update(result, Path(sys.executable), version)
                    return  # _perform_update calls os._exit; this is just a safety return
                else:
                    logger.info(
                        "[auto_updater] Running as plain script (not frozen EXE) — "
                        "EXE replacement skipped.  New version available: %s", new_ver,
                    )
            else:
                logger.info("[auto_updater] Check #%d — up to date (version=%s)", check_count, version)

        except Exception as exc:
            logger.error("[auto_updater] Check #%d — unexpected error: %s", check_count, exc, exc_info=True)

        logger.info("[auto_updater] Next check in %ds", _UPDATE_CHECK_INTERVAL)
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
            "(UPDATE_SERVER_URL / CATALYTICS_API_BASE missing) — auto-update disabled"
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
