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
import re
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


def _apply_update_windows(pending_exe: Path, current_exe: Path, canonical_exe: Path, new_version: str):
    """
    Write a self-deleting PowerShell script that:
      1. Waits for this process (by PID) to exit
      2. Kills any other instances of the EXE by name
      3. Renames running EXE → _old, then pending → canonical name
      4. Launches the new EXE from the correct working directory via Task Scheduler
      5. Deletes itself

    Data files (.env, *.sqlite, logs/, automation_state.json) are never touched.
    """
    pid = os.getpid()
    ps_path = current_exe.parent / '_mw_updater.ps1'

    def ps_log(msg):
        """Return a PS line that appends msg to the PS log file. Uses Add-Content so
        failures are silently ignored — the script never crashes due to a log write."""
        return f'Add-Content -Path $log_file -Value ("[" + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "] {msg}") -ErrorAction SilentlyContinue'

    old_exe = canonical_exe.parent / (canonical_exe.stem + '_old' + canonical_exe.suffix)

    lines = [
        f'# Catalytics Middleware auto-updater — upgrading to {new_version}',
        f'$target_pid  = {pid}',
        f'$pending     = \'{pending_exe}\'',
        f'$running_exe = \'{current_exe}\'',
        f'$current     = \'{canonical_exe}\'',
        f'$old_exe     = \'{old_exe}\'',
        f'$work_dir    = \'{canonical_exe.parent}\'',
        f'$proc_name   = \'{canonical_exe.stem}\'',
        f'$ps_self     = $MyInvocation.MyCommand.Path',
        # Log goes to work_dir root — no subdirectory dependency
        '$log_file    = $work_dir + "\\_mw_ps.log"',
        '',
        '# Ensure logs dir exists for auto_updater.log',
        'New-Item -ItemType Directory -Force -Path ($work_dir + "\\logs") | Out-Null',
        '',
        ps_log(f'PS_UPDATER START — upgrading to {new_version}  PID={pid}'),
        ps_log('PS_UPDATER running_exe : $running_exe'),
        ps_log('PS_UPDATER canonical   : $current'),
        ps_log('PS_UPDATER pending     : $pending'),
        ps_log('PS_UPDATER old_exe     : $old_exe'),
        '',
        '# 0. Admin check',
        '$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)',
        ps_log('PS_UPDATER isAdmin=$isAdmin'),
        'if (-not $isAdmin) {',
        f'    {ps_log("PS_UPDATER not admin — re-launching elevated")}',
        '    Start-Process powershell -ArgumentList ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps_self`"") -Verb RunAs',
        '    exit',
        '}',
        ps_log('PS_UPDATER running as Administrator — proceeding'),
        '',
        '# 1. Verify temp (downloaded) file exists',
        ps_log('PS_UPDATER STEP 1 — verifying downloaded temp file'),
        'if (-not (Test-Path -LiteralPath $pending)) {',
        f'    {ps_log("PS_UPDATER ERROR — temp file not found: $pending — aborting")}',
        '    exit 1',
        '}',
        '$pending_size = (Get-Item -LiteralPath $pending).Length',
        ps_log('PS_UPDATER temp file OK  size=$pending_size bytes'),
        '',
        '# 2. Remove stale backup if exists',
        ps_log('PS_UPDATER STEP 2 — removing stale backup if exists'),
        'if (Test-Path -LiteralPath $old_exe) {',
        '    Remove-Item -LiteralPath $old_exe -Force -ErrorAction SilentlyContinue',
        f'    {ps_log("PS_UPDATER stale backup removed: $old_exe")}',
        '}',
        '',
        '# 3. Rename running EXE → _old  (Windows allows renaming a running EXE)',
        '# Old process is STILL RUNNING here — new EXE will be launched before it stops',
        ps_log('PS_UPDATER STEP 3 — renaming running EXE to backup (up to 20 attempts)'),
        '$backed_up = $false',
        'for ($i = 0; $i -lt 20; $i++) {',
        '    try {',
        '        Move-Item -LiteralPath $running_exe -Destination $old_exe -ErrorAction Stop',
        '        $backed_up = $true',
        f'        {ps_log("PS_UPDATER backup OK on attempt $($i+1) : $old_exe")}',
        '        break',
        '    } catch {',
        f'        {ps_log("PS_UPDATER backup attempt $($i+1) FAILED: $($_.Exception.Message)")}',
        '        Start-Sleep -Seconds 1',
        '    }',
        '}',
        'if (-not $backed_up) {',
        f'    {ps_log("PS_UPDATER ERROR — could not rename running EXE after 20 attempts — aborting")}',
        '    exit 1',
        '}',
        '',
        '# 4. Rename temp → canonical name (new EXE now in place on disk)',
        ps_log('PS_UPDATER STEP 4 — renaming temp → canonical EXE name'),
        'try {',
        '    Move-Item -LiteralPath $pending -Destination $current -ErrorAction Stop',
        f'    {ps_log("PS_UPDATER new EXE in place: $current")}',
        '} catch {',
        f'    {ps_log("PS_UPDATER ERROR — rename temp failed: $($_.Exception.Message) — restoring backup")}',
        '    Move-Item -LiteralPath $old_exe -Destination $running_exe -ErrorAction SilentlyContinue',
        f'    {ps_log("PS_UPDATER backup restored — update failed")}',
        '    exit 1',
        '}',
        '',
        '# 5. Launch NEW EXE FIRST via schtasks.exe, THEN let old process exit',
        '# Old EXE (Python) is still running — it calls os._exit after ~1s automatically',
        '# Launching new EXE first ensures no dark window if launch succeeds',
        ps_log('PS_UPDATER STEP 5 — launching new EXE via schtasks.exe (old still running)'),
        '$task_name  = "_MW_" + $proc_name',
        '$run_time   = (Get-Date).AddMinutes(1).ToString("HH:mm")',
        '$tr_arg     = \'"\' + $current + \'"\'',
        ps_log('PS_UPDATER schtasks /Create — task=$task_name  exe=$current  time=$run_time'),
        '$sched_out  = (& schtasks.exe /Create /TN $task_name /TR $tr_arg /SC ONCE /ST $run_time /F /RL HIGHEST 2>&1) -join " "',
        f'{ps_log("PS_UPDATER schtasks /Create  exit=$LASTEXITCODE  out=$sched_out")}',
        'if ($LASTEXITCODE -eq 0) {',
        f'    {ps_log("PS_UPDATER schtasks /Create OK — running task now")}',
        '    $run_out = (& schtasks.exe /Run /TN $task_name 2>&1) -join " "',
        f'    {ps_log("PS_UPDATER schtasks /Run  exit=$LASTEXITCODE  out=$run_out")}',
        '} else {',
        f'    {ps_log("PS_UPDATER schtasks /Create FAILED — falling back to Start-Process")}',
        '    Start-Process -FilePath $current -WorkingDirectory $work_dir',
        f'    {ps_log("PS_UPDATER Start-Process fallback called")}',
        '}',
        '',
        '# 6. Wait for old PID to exit (Python calls os._exit(0) after ~1s)',
        ps_log('PS_UPDATER STEP 6 — waiting for old process (PID=$target_pid) to exit'),
        '$wait_secs = 0',
        'while (Get-Process -Id $target_pid -ErrorAction SilentlyContinue) {',
        '    Start-Sleep -Seconds 1',
        '    $wait_secs++',
        '    if ($wait_secs -ge 30) {',
        f'        {ps_log("PS_UPDATER old process still alive at 30s — force killing")}',
        '        Stop-Process -Id $target_pid -Force -ErrorAction SilentlyContinue',
        '        break',
        '    }',
        '}',
        ps_log('PS_UPDATER old process exited — new EXE now owns the port'),
        '',
        '# 7. Cleanup: delete schtasks task, old EXE file, this script',
        ps_log('PS_UPDATER STEP 7 — cleanup (waiting 15s)'),
        'Start-Sleep -Seconds 15',
        '& schtasks.exe /Delete /TN $task_name /F 2>&1 | Out-Null',
        ps_log('PS_UPDATER schtasks task deleted'),
        'Remove-Item -LiteralPath $old_exe -Force -ErrorAction SilentlyContinue',
        ps_log('PS_UPDATER old EXE file removed'),
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

    # Guard: server must not serve a temp/backup file as an update
    url_filename = Path(download_url.rstrip('/').split('/')[-1]).stem.lower()
    if any(x in url_filename for x in ('_pending', '_old', '_new', '_temp')):
        logger.error(
            "[auto_updater] download_url points to a temp/backup file — "
            "wrong file uploaded on server: %s  Skipping.", download_url
        )
        return

    # Compute the canonical (clean) EXE name — strip any _vX.Y.Z_temp / _old / _temp suffixes
    # that may have snowballed if the client was running a messy-named EXE.
    # e.g. "co_dashboard_v1.0.3_temp" → "co_dashboard"
    clean_stem = re.sub(r'(_v[\d.]+(_temp|_old|_new)?|_temp|_old|_new)$', '', current_exe.stem, flags=re.IGNORECASE)
    canonical_exe = current_exe.parent / (clean_stem + current_exe.suffix)
    logger.info("[auto_updater] Running EXE   : %s", current_exe)
    logger.info("[auto_updater] Canonical EXE : %s", canonical_exe)
    # Always download to a fixed temp name based on the canonical stem
    pending_exe = current_exe.parent / (clean_stem + '_temp.exe')
    logger.info("[auto_updater] Download destination (temp): %s", pending_exe)

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
    _apply_update_windows(pending_exe, current_exe, canonical_exe, new_version)


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
