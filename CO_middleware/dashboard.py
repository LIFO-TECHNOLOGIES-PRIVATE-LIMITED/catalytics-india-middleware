"""
CO Middleware - Web Dashboard
Provides real-time monitoring and control interface for the middleware
Single company version (adapted from arasan_gas)
"""

import os
import sys
import json
import logging
import re
import webbrowser
import threading
import subprocess
import requests
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from collections import deque

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import sqlite3
import config as cfg
import db
from config import config, BASE_DIR
from automation_manager import get_manager
from log_capture import dashboard_logger

# Determine template folder â€” inside _MEIPASS when frozen, else default
if getattr(sys, 'frozen', False):
    _template_folder = os.path.join(sys._MEIPASS, 'templates')
else:
    _template_folder = os.path.join(os.path.dirname(__file__), 'templates')

app = Flask(__name__, template_folder=_template_folder)
CORS(app)
logger = logging.getLogger(__name__)

# Activity and error tracking
activity_log = deque(maxlen=50)
error_log = deque(maxlen=10)
activity_lock = threading.Lock()
ENV_KEY_RE = re.compile(r"^[A-Z0-9_]+$")


def check_tally_connection():
    """Check if Tally server is accessible"""
    try:
        tally_url = cfg.get_env("TALLY_URL", "http://localhost:9000/")
        response = requests.get(tally_url, timeout=2)
        return response.status_code == 200
    except:
        return False


def check_catalytics_connection():
    """Check if Catalytics backend is accessible"""
    try:
        api_base = cfg.get_env("CATALYTICS_API_BASE_URL", "").strip().rstrip("/")
        if not api_base:
            return False
        response = requests.get(api_base, timeout=2)
        return response.status_code < 500
    except:
        return False




def _same_db_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except Exception:
        return left == right


def initialize_database_file(db_path: str, *, include_master_tables: bool = False) -> None:
    """Create the full required schema for a configured SQLite database file."""
    if not db_path:
        return

    conn = db.connect(db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()

    # Keep the Database bootstrap for backward-compatible migrations/index creation.
    master_db = db.Database(db_path)
    master_db.close()


def initialize_configured_databases() -> None:
    """Initialize the configured DC/master databases for the current env."""
    tally_db_path = cfg.get_env("TALLY_DB_PATH")
    master_db_path = config.SQLITE_DB_PATH

    if tally_db_path:
        initialize_database_file(
            tally_db_path,
            include_master_tables=True,
        )

    if master_db_path and not _same_db_path(master_db_path, tally_db_path):
        initialize_database_file(master_db_path, include_master_tables=True)


RUNTIME_LOG_FILES = (
    'app.log',
    'dashboard_operations.log',
    'customer_fetch.log',
    'product_fetch.log',
    'customer_sync.log',
    'product_sync.log',
    'dc_fetch.log',
    'dc_fetch_errors.log',
    'dc_sync.log',
    'dc_sync_errors.log',
)


def ensure_runtime_support_files() -> None:
    """Create log files/directories used by the EXE on first launch."""
    log_dir = BASE_DIR / 'logs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    configured_log = cfg.get_env('LOG_FILE', 'logs/app.log') or 'logs/app.log'
    configured_path = Path(configured_log)
    if not configured_path.is_absolute():
        configured_path = BASE_DIR / configured_log

    log_paths = {configured_path}
    log_paths.update(log_dir / name for name in RUNTIME_LOG_FILES)

    for log_path in log_paths:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
        except Exception:
            pass


def get_recent_file_logs(log_file, lines=20):
    """Get recent lines from a log file"""
    try:
        log_path = Path(BASE_DIR) / 'logs' / log_file
        if not log_path.exists():
            return []
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()
            return [line.strip() for line in all_lines[-lines:]]
    except Exception:
        return []


def get_log_excerpt_by_keyword(log_file, keyword, lines=80):
    """Return up to `lines` log lines containing keyword; fallback to tail if none."""
    try:
        from pathlib import Path as _Path
        log_path = _Path(str(BASE_DIR)) / 'logs' / log_file
        if not log_path.exists():
            return []
        data = log_path.read_text(encoding='utf-8', errors='ignore').splitlines()
        keyword_lower = (keyword or '').lower()
        matched = [ln for ln in data if keyword_lower in ln.lower()] if keyword_lower else []
        source = matched if matched else data
        return source[-lines:]
    except Exception:
        return []


def get_env_path() -> str:
    return cfg.resolve_env_path(ROOT_DIR)


def read_env_entries() -> dict:
    env_path = get_env_path()
    entries = {}
    if not os.path.exists(env_path):
        return entries
    with open(env_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            entries[key] = value.strip().strip("'").strip('"')
    return entries


def write_env_entries(entries: dict) -> None:
    env_path = get_env_path()
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    temp_path = env_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        for key in sorted(entries.keys()):
            value = str(entries[key]).replace("\r", " ").replace("\n", " ").strip()
            handle.write(f"{key}={value}\n")
    os.replace(temp_path, env_path)
    cfg.load_env_file(env_path)


def restart_dashboard_process_async(restart_delay_seconds: float = 0.8) -> None:
    """Restart dashboard in-place so it stays attached to the same terminal."""
    def _restart_self():
        time.sleep(restart_delay_seconds)
        try:
            if getattr(sys, 'frozen', False):
                os.execv(sys.executable, [sys.executable])
            else:
                script_path = os.path.abspath(__file__)
                os.execv(sys.executable, [sys.executable, script_path])
        except Exception:
            logger.exception("Failed to restart dashboard process in-place")
            os._exit(1)

    threading.Thread(target=_restart_self, daemon=True).start()


def stop_dashboard_process_async(exit_delay_seconds: float = 0.5) -> None:
    """Stop the current dashboard process after sending API response."""
    def _exit_self():
        time.sleep(exit_delay_seconds)
        os._exit(0)

    threading.Thread(target=_exit_self, daemon=True).start()


def _dashboard_control_hosts() -> list[str]:
    host = (config.WEB_UI_HOST or '').strip()
    hosts: list[str] = []

    def _add(value: str) -> None:
        value = (value or '').strip()
        if value and value not in hosts:
            hosts.append(value)

    if host in ('', '0.0.0.0', '::'):
        _add('127.0.0.1')
        _add('localhost')
    else:
        _add(host)
        if host.lower() == 'localhost':
            _add('127.0.0.1')

    return hosts


def maybe_toggle_existing_instance_on_launch() -> None:
    """If another dashboard instance is already running, request it to close and exit."""
    default_toggle = 'true' if getattr(sys, 'frozen', False) else 'false'
    if not _env_flag('TOGGLE_EXISTING_INSTANCE_ON_LAUNCH', default_toggle):
        return

    for host in _dashboard_control_hosts():
        base_url = f'http://{host}:{config.WEB_UI_PORT}'
        try:
            response = requests.post(
                f'{base_url}/api/internal/shutdown',
                json={'source': 'launcher'},
                timeout=1.2,
            )
        except requests.RequestException:
            continue
        except Exception:
            continue

        if response.status_code != 200:
            continue

        try:
            payload = response.json()
        except Exception:
            payload = {}

        if payload.get('success'):
            logger.info('Existing dashboard instance found at %s; shutdown requested', base_url)
            time.sleep(0.2)
            os._exit(0)


@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html',
                         entity_name=config.ENTITY_NAME,
                         entity_id=config.ENTITY_ID)


@app.route('/api/status')
def api_status():
    """Get current system status"""
    tally_online = check_tally_connection()
    catalytics_online = check_catalytics_connection()

    manager = get_manager()
    automation_status = manager.get_status()
    middleware_online = (automation_status.get('status') == 'running')

    # --- Master data stats (SQLITE_DB_PATH: customers, products) ---
    cust_total = cust_synced = cust_unsynced = 0
    cust_last_sync = None
    prod_total = prod_synced = prod_unsynced = 0
    prod_last_sync = None
    cust_by_company = {}
    prod_by_company = {}
    dup_total = 0
    dup_breakdown = {}

    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM customers')
        cust_total = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM customers WHERE is_synced = 1')
        cust_synced = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM customers WHERE is_synced = 0')
        cust_unsynced = cursor.fetchone()[0]
        cursor.execute('SELECT MAX(last_sync_at) FROM customers WHERE is_synced = 1')
        cust_last_sync = cursor.fetchone()[0]
        cursor.execute('SELECT tally_company, COUNT(*) FROM customers GROUP BY tally_company')
        cust_by_company = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute('SELECT COUNT(*) FROM products')
        prod_total = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM products WHERE is_synced = 1')
        prod_synced = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM products WHERE is_synced = 0')
        prod_unsynced = cursor.fetchone()[0]
        cursor.execute('SELECT MAX(last_sync_at) FROM products WHERE is_synced = 1')
        prod_last_sync = cursor.fetchone()[0]
        cursor.execute('SELECT tally_company, COUNT(*) FROM products GROUP BY tally_company')
        prod_by_company = {row[0]: row[1] for row in cursor.fetchall()}

        try:
            cursor.execute('SELECT COUNT(*) FROM duplicate_log')
            dup_total = cursor.fetchone()[0]
            cursor.execute('SELECT entity_type, COUNT(*) FROM duplicate_log GROUP BY entity_type')
            dup_breakdown = {row[0]: row[1] for row in cursor.fetchall()}
        except Exception:
            pass

        conn.close()
    except Exception as e:
        logger.warning(f"Could not read master DB stats: {e}")

    # --- DC/Invoice stats (TALLY_DB_PATH: delivery_notes) ---
    dc_total = dc_synced = dc_unsynced = 0
    dc_last_sync = None
    dc_by_company = {}
    tally_db_path = cfg.get_env("TALLY_DB_PATH")
    if tally_db_path:
        try:
            conn2 = db.connect(tally_db_path)
            dc_total = conn2.execute('SELECT COUNT(*) FROM delivery_notes').fetchone()[0]
            dc_synced = conn2.execute(
                'SELECT COUNT(*) FROM delivery_notes dn LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id WHERE COALESCE(ss.is_synced, 0) = 1'
            ).fetchone()[0]
            dc_unsynced = conn2.execute(
                'SELECT COUNT(*) FROM delivery_notes dn LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id WHERE COALESCE(ss.is_synced, 0) = 0'
            ).fetchone()[0]
            dc_last_sync = conn2.execute(
                'SELECT MAX(ss.synced_at) FROM sync_status ss WHERE ss.is_synced = 1'
            ).fetchone()[0]
            company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
            dc_by_company = {company_name: dc_total}
            conn2.close()
        except Exception as e:
            logger.warning(f"Could not read DC DB stats: {e}")

    active_companies = config.get_active_companies()

    return jsonify({
        'timestamp': datetime.now().isoformat(),
        'connections': {
            'tally': tally_online,
            'catalytics': catalytics_online,
            'middleware': middleware_online
        },
        'statistics': {
            'customers': {
                'total': cust_total,
                'synced': cust_synced,
                'unsynced': cust_unsynced,
                'last_sync': cust_last_sync,
                'by_company': cust_by_company
            },
            'products': {
                'total': prod_total,
                'synced': prod_synced,
                'unsynced': prod_unsynced,
                'last_sync': prod_last_sync,
                'by_company': prod_by_company
            },
            'invoices': {
                'total': dc_total,
                'synced': dc_synced,
                'unsynced': dc_unsynced,
                'last_sync': dc_last_sync,
                'by_company': dc_by_company
            },
            'duplicates': {
                'total': dup_total,
                'breakdown': dup_breakdown
            }
        },
        'config': {
            'entity_name': config.ENTITY_NAME,
            'entity_id': config.ENTITY_ID,
            'active_companies': active_companies,
            'tally_company_map': {
                key: config.TALLY_COMPANIES[key]
                for key in config.TALLY_COMPANY_ACTIVE
                if config.TALLY_COMPANIES.get(key)
            },
            'sync_batch_size': cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            'invoice_fetch_start_date': (
                cfg.get_env("TALLY_FROM_DATE")
                or 'Dynamic: yesterday to tomorrow'
            ),
            'product_type_map': config.get_product_type_map()
        },
        'automation': automation_status
    })


@app.route('/api/automation/start', methods=['POST'])
def api_automation_start():
    """Start automation"""
    try:
        manager = get_manager()
        success = manager.start()
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Start Automation',
                'status': 'success' if success else 'warning',
                'detail': 'Automation started' if success else 'Already running'
            })
        
        return jsonify({'success': success, 'message': 'Automation started' if success else 'Already running'})
    except Exception as e:
        logger.exception("Failed to start automation")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/stop', methods=['POST'])
def api_automation_stop():
    """Stop automation"""
    try:
        manager = get_manager()
        success = manager.stop()
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Stop Automation',
                'status': 'success' if success else 'warning',
                'detail': 'Automation stopped' if success else 'Not running'
            })
        
        return jsonify({'success': success, 'message': 'Automation stopped' if success else 'Not running'})
    except Exception as e:
        logger.exception("Failed to stop automation")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/restart', methods=['POST'])
def api_automation_restart():
    """Restart automation"""
    try:
        manager = get_manager()
        success = manager.restart()
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Restart Automation',
                'status': 'success',
                'detail': 'Automation restarted'
            })
        
        return jsonify({'success': success, 'message': 'Automation restarted'})
    except Exception as e:
        logger.exception("Failed to restart automation")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/service/control', methods=['POST'])
def api_service_control():
    """Start/Stop external services using env-configured shell commands."""
    try:
        payload = request.get_json(silent=True) or {}
        service = str(payload.get('service', '')).strip().lower()
        action = str(payload.get('action', '')).strip().lower()

        if service not in ('tally', 'catalytics', 'middleware'):
            return jsonify({'success': False, 'error': 'Invalid service'}), 400
        if action not in ('start', 'stop'):
            return jsonify({'success': False, 'error': 'Invalid action'}), 400

        if service == 'middleware':
            manager = get_manager()
            if action == 'start':
                dashboard_logger.clear()
                dashboard_logger.write_log("=== MANUAL: MIDDLEWARE START REQUESTED ===")
                dashboard_logger.write_log("=== RESTARTING MIDDLEWARE AUTOMATION LOOP ===")
                success = manager.restart()
                return jsonify({
                    'success': bool(success),
                    'message': 'Middleware automation restarted. For code changes, restart from terminal once.'
                })

            manager.stop()
            dashboard_logger.write_log("=== MANUAL: MIDDLEWARE STOP REQUESTED ===")
            dashboard_logger.write_log("=== STOPPING DASHBOARD PROCESS ===")
            stop_dashboard_process_async(exit_delay_seconds=0.6)
            return jsonify({'success': True, 'message': 'Middleware stopped. Dashboard process is shutting down.'})

        cmd_key = f"{service.upper()}_{action.upper()}_CMD"
        cmd = cfg.get_env(cmd_key, "").strip()
        if not cmd:
            return jsonify({
                'success': False,
                'error': f'Command not configured: {cmd_key}. Add it in .env'
            }), 400

        if action == 'start':
            popen_kwargs = {
                'shell': True,
                'stdout': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL,
            }
            if sys.platform != 'win32':
                popen_kwargs['start_new_session'] = True
            subprocess.Popen(cmd, **popen_kwargs)
            return jsonify({'success': True, 'message': f'{service.title()} start command executed'})

        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or '').strip()[:400]
            return jsonify({'success': False, 'error': err or 'Stop command failed'}), 500
        return jsonify({'success': True, 'message': f'{service.title()} stop command executed'})
    except Exception as e:
        logger.exception("Failed to control service")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/env-config', methods=['GET'])
def api_get_env_config():
    """Read all .env values for UI configuration panel."""
    try:
        return jsonify({
            'success': True,
            'path': get_env_path(),
            'values': read_env_entries()
        })
    except Exception as e:
        logger.exception("Failed to read env config")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/env-config', methods=['POST'])
def api_set_env_config():
    """Save .env values from UI configuration panel."""
    try:
        payload = request.get_json(silent=True) or {}
        values = payload.get('values', {})
        if not isinstance(values, dict):
            return jsonify({'success': False, 'error': 'values must be an object'}), 400

        cleaned = {}
        for raw_key, raw_value in values.items():
            key = str(raw_key).strip()
            if not key:
                continue
            if not ENV_KEY_RE.match(key):
                return jsonify({'success': False, 'error': f'Invalid env key: {key}'}), 400
            cleaned[key] = "" if raw_value is None else str(raw_value)

        write_env_entries(cleaned)
        return jsonify({'success': True, 'message': 'Environment updated', 'count': len(cleaned)})
    except Exception as e:
        logger.exception("Failed to save env config")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/intervals', methods=['POST'])
@app.route('/api/automation/set-intervals', methods=['POST'])
def api_automation_intervals():
    """Update automation intervals"""
    try:
        data = request.get_json() or {}
        manager = get_manager()
        success = manager.set_intervals(data)
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Update Intervals',
                'status': 'success',
                'detail': f'Intervals updated: {data}'
            })
        
        return jsonify({'success': success, 'message': 'Intervals updated'})
    except Exception as e:
        logger.exception("Failed to update intervals")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/internal/shutdown', methods=['POST'])
def api_internal_shutdown():
    """Allow a new local launcher instance to close the currently running dashboard."""
    remote_addr = (request.remote_addr or '').strip()
    if remote_addr and remote_addr not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return jsonify({'success': False, 'error': 'Local requests only'}), 403

    try:
        manager = get_manager()
        manager.stop()
    except Exception as exc:
        logger.warning(f'Could not stop automation during internal shutdown: {exc}')

    dashboard_logger.write_log('=== LAUNCHER: EXISTING DASHBOARD INSTANCE SHUTDOWN REQUESTED ===')
    stop_dashboard_process_async(exit_delay_seconds=0.35)
    return jsonify({'success': True, 'message': 'Dashboard shutdown requested'})


@app.route('/api/logs')
def api_logs():
    """Get recent log entries"""
    log_type = request.args.get('type', 'main')
    lines = int(request.args.get('lines', 50))

    log_files = {
        'main': ['app.log'],
        'fetch_master': ['product_fetch.log', 'customer_fetch.log'],
        'fetch_invoices': ['dc_fetch.log'],
        'sync': ['dc_sync.log', 'customer_sync.log', 'product_sync.log'],
    }

    files = log_files.get(log_type, [])
    if isinstance(files, str):
        files = [files]

    logs = []
    for fname in files:
        part = get_recent_file_logs(fname, lines)
        if not part:
            continue
        if len(files) > 1:
            logs.append(f"--- {fname} ---")
        logs.extend(part)

    if not logs:
        logs = dashboard_logger.get_logs(lines)

    if len(logs) > lines:
        logs = logs[-lines:]

    return jsonify({
        'log_type': log_type,
        'log_file': files,
        'lines': logs
    })


@app.route('/api/activity')
def api_activity():
    """Get recent activity log"""
    with activity_lock:
        return jsonify({'activities': list(activity_log)[:20]})


@app.route('/api/errors')
def api_errors():
    """Get recent errors - placeholder for compatibility"""
    return jsonify({'errors': []})


@app.route('/api/progress')
def api_progress():
    """Get progress info - placeholder for compatibility"""
    return jsonify({'active': False, 'percentage': 0, 'current': 0, 'total': 0, 'step': ''})


@app.route('/api/automation/status')
def api_automation_status():
    """Get automation status"""
    try:
        manager = get_manager()
        status = manager.get_status()
        return jsonify(status)
    except Exception as e:
        logger.exception("Failed to get automation status")
        return jsonify({'running': False, 'error': str(e)}), 500


@app.route('/api/logs/recent')
def api_logs_recent():
    '''Get recent logs for the terminal panel'''
    try:
        lines = request.args.get('lines', 100, type=int)
        logs = dashboard_logger.get_recent_logs(lines)
        return jsonify({
            'success': True,
            'logs': logs,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    '''Clear logs'''
    try:
        dashboard_logger.clear_logs()
        return jsonify({'success': True, 'message': 'Logs cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trigger/fetch_invoices', methods=['POST'])
def trigger_fetch_invoices():
    """Manually trigger invoice/DC fetch"""
    try:
        from fetch_invoices import build_config, run_once
        from types import SimpleNamespace
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Invoices',
                'status': 'started'
            })
        
        dashboard_logger.write_log("=== MANUAL: FETCH INVOICES STARTED ===")
        
        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=cfg.get_env("TALLY_DB_PATH"),
            tally_url=cfg.get_env("TALLY_URL"),
            company=cfg.get_env("TALLY_COMPANY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            from_date=cfg.get_env("TALLY_FROM_DATE"),
            to_date=cfg.get_env("TALLY_TO_DATE"),
            days_back=cfg.get_env_int("TALLY_DAYS_BACK"),
            fetch_stock=cfg.get_env_bool("TALLY_FETCH_STOCK", False),
            dry_run=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=cfg.get_env_bool("LOG_JSON", False),
            log_file=None
        )
        
        fetch_config = build_config(args)
        stats = run_once(fetch_config)
        
        dashboard_logger.write_log(f"=== MANUAL: FETCH INVOICES COMPLETED - {stats} ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Invoices',
                'status': 'success',
                'detail': f"Created: {stats.get('created', 0)}, Updated: {stats.get('updated', 0)}"
            })
        
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception("Failed to fetch invoices")
        dashboard_logger.write_log(f"=== MANUAL: FETCH INVOICES FAILED - {str(e)} ===")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trigger/fetch_master', methods=['POST'])
def trigger_fetch_master():
    """Manually trigger master data fetch (customers + products)"""
    try:
        from fetch_customers import fetch_customers_from_all_companies
        from fetch_products import fetch_products_from_all_companies

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Master',
                'status': 'started'
            })

        dashboard_logger.write_log("=== MANUAL: FETCH MASTER DATA STARTED ===")

        prod_stats = {'new_saved': 0, 'updated': 0}
        cust_stats = {'new_saved': 0, 'updated': 0}

        if cfg.get_env_bool("AUTO_FETCH_PRODUCTS", False):
            prod_stats = fetch_products_from_all_companies()
        else:
            dashboard_logger.write_log("Product fetch skipped (AUTO_FETCH_PRODUCTS=false)")

        if cfg.get_env_bool("AUTO_FETCH_CUSTOMERS", False):
            cust_stats = fetch_customers_from_all_companies()
        else:
            dashboard_logger.write_log("Customer fetch skipped (AUTO_FETCH_CUSTOMERS=false)")

        dashboard_logger.write_log("=== MANUAL: FETCH MASTER DATA COMPLETED ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Master',
                'status': 'success',
                'detail': f"Customers: new={cust_stats.get('new_saved', 0)}, updated={cust_stats.get('updated', 0)} | Products: new={prod_stats.get('new_saved', 0)}, updated={prod_stats.get('updated', 0)}"
            })

        return jsonify({'success': True, 'customers': cust_stats, 'products': prod_stats})
    except Exception as e:
        logger.exception("Failed to fetch master data")
        dashboard_logger.write_log(f"=== MANUAL: FETCH MASTER DATA FAILED - {str(e)} ===")
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/trigger/fetch_customers', methods=['POST'])
def trigger_fetch_customers():
    """Manually trigger customer fetch from Tally"""
    if not cfg.get_env_bool("AUTO_FETCH_CUSTOMERS", False):
        return jsonify({'success': False, 'error': 'Customer fetch is disabled (AUTO_FETCH_CUSTOMERS=false)'}), 400
    try:
        from fetch_customers import fetch_customers_from_all_companies

        dashboard_logger.write_log("=== MANUAL: FETCH CUSTOMERS STARTED ===")

        stats = fetch_customers_from_all_companies()

        dashboard_logger.write_log(
            f"=== MANUAL: FETCH CUSTOMERS COMPLETED - new={stats.get('new_saved', 0)}, updated={stats.get('updated', 0)} ==="
        )

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Customers',
                'status': 'success',
                'detail': f"New: {stats.get('new_saved', 0)}, Updated: {stats.get('updated', 0)}"
            })

        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception("Failed to fetch customers")
        dashboard_logger.write_log(f"=== MANUAL: FETCH CUSTOMERS FAILED - {str(e)} ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Customers',
                'status': 'error',
                'detail': str(e)
            })

        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trigger/fetch_products', methods=['POST'])
def trigger_fetch_products():
    """Manually trigger product fetch from Tally"""
    if not cfg.get_env_bool("AUTO_FETCH_PRODUCTS", False):
        return jsonify({'success': False, 'error': 'Product fetch is disabled (AUTO_FETCH_PRODUCTS=false)'}), 400
    try:
        from fetch_products import fetch_products_from_all_companies

        dashboard_logger.write_log("=== MANUAL: FETCH PRODUCTS STARTED ===")

        stats = fetch_products_from_all_companies()

        dashboard_logger.write_log(
            f"=== MANUAL: FETCH PRODUCTS COMPLETED - new={stats.get('new_saved', 0)}, updated={stats.get('updated', 0)} ==="
        )

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Products',
                'status': 'success',
                'detail': f"New: {stats.get('new_saved', 0)}, Updated: {stats.get('updated', 0)}"
            })

        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception("Failed to fetch products")
        dashboard_logger.write_log(f"=== MANUAL: FETCH PRODUCTS FAILED - {str(e)} ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Products',
                'status': 'error',
                'detail': str(e)
            })

        return jsonify({'success': False, 'error': str(e)}), 500



@app.route('/api/trigger/sync', methods=['POST'])
def trigger_sync():
    """Manually trigger sync to Catalytics"""
    try:
        from sync_catalytics import build_config as build_dc_config, run_once as sync_dc
        from sync_customers import build_config as build_cust_config, run_once as sync_cust
        from sync_products import build_config as build_prod_config, run_once as sync_prod
        from types import SimpleNamespace
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Sync',
                'status': 'started'
            })
        
        dashboard_logger.write_log("=== MANUAL: SYNC TO CATALYTICS STARTED ===")
        dashboard_logger.write_log(f"API Base URL: {cfg.get_env('CATALYTICS_API_BASE_URL')}")
        dashboard_logger.write_log(f"Entity ID: {cfg.get_env_int('CATALYTICS_ENTITY_ID')}")
        dashboard_logger.write_log(f"API Key: {'*' * 20 + cfg.get_env('CATALYTICS_API_KEY')[-10:] if cfg.get_env('CATALYTICS_API_KEY') else 'NOT SET'}")
        
        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=cfg.get_env("TALLY_DB_PATH"),
            api_base_url=cfg.get_env("CATALYTICS_API_BASE_URL"),
            api_key=cfg.get_env("CATALYTICS_API_KEY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            company=cfg.get_env("TALLY_COMPANY"),
            batch_size=cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            limit=cfg.get_env_int("SYNC_LIMIT", 200),
            max_attempts=cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5),
            allow_tally_fetch=cfg.get_env_bool("SYNC_ALLOW_TALLY_FETCH", False),
            dry_run=cfg.get_env_bool("SYNC_DRY_RUN", False),
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=cfg.get_env_bool("LOG_JSON", False),
            log_file=None
        )
        
        # Sync products first, then customers, then DCs
        master_args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=None,
            api_base_url=cfg.get_env("CATALYTICS_API_BASE_URL"),
            api_key=cfg.get_env("CATALYTICS_API_KEY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            company=cfg.get_env("TALLY_COMPANY"),
            batch_size=cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            limit=cfg.get_env_int("SYNC_LIMIT", 200),
            max_attempts=cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5),
            dry_run=cfg.get_env_bool("SYNC_DRY_RUN", False),
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=False,
            log_file=None
        )

        prod_config = build_prod_config(master_args)
        prod_stats = sync_prod(prod_config)

        cust_config = build_cust_config(master_args)
        cust_stats = sync_cust(cust_config)

        dc_config = build_dc_config(args)
        dc_stats = sync_dc(dc_config)
        
        dashboard_logger.write_log(f"=== MANUAL: SYNC TO CATALYTICS COMPLETED ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Sync',
                'status': 'success',
                'detail': f"DCs: {dc_stats.get('ok', 0)}/{dc_stats.get('sent', 0)}, Customers: {cust_stats.get('ok', 0)}, Products: {prod_stats.get('ok', 0)}"
            })
        
        return jsonify({'success': True, 'dcs': dc_stats, 'customers': cust_stats, 'products': prod_stats})
    except Exception as e:
        logger.exception("Failed to sync")
        dashboard_logger.write_log(f"=== MANUAL: SYNC FAILED - {str(e)} ===")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trigger/sync_customers', methods=['POST'])
def trigger_sync_customers():
    """Manually trigger customer sync to Catalytics"""
    try:
        from sync_customers import build_config, run_once
        from types import SimpleNamespace

        dashboard_logger.write_log("=== MANUAL: SYNC CUSTOMERS STARTED ===")

        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=None,
            api_base_url=cfg.get_env("CATALYTICS_API_BASE_URL"),
            api_key=cfg.get_env("CATALYTICS_API_KEY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            company=cfg.get_env("TALLY_COMPANY"),
            batch_size=cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            limit=cfg.get_env_int("SYNC_LIMIT", 200),
            max_attempts=cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5),
            dry_run=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=False,
            log_file=None
        )
        
        config_obj = build_config(args)
        stats = run_once(config_obj)
        
        dashboard_logger.write_log(f"=== MANUAL: SYNC CUSTOMERS COMPLETED - {stats.get('ok', 0)} synced ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Sync Customers',
                'status': 'success',
                'detail': f"{stats.get('ok', 0)} synced"
            })
        
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception("Failed to sync customers")
        dashboard_logger.write_log(f"=== MANUAL: SYNC CUSTOMERS FAILED - {str(e)} ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Sync Customers',
                'status': 'error',
                'detail': str(e)
            })
        
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trigger/sync_products', methods=['POST'])
def trigger_sync_products():
    """Manually trigger product sync to Catalytics"""
    try:
        from sync_products import build_config, run_once
        from types import SimpleNamespace

        dashboard_logger.write_log("=== MANUAL: SYNC PRODUCTS STARTED ===")

        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=None,
            api_base_url=cfg.get_env("CATALYTICS_API_BASE_URL"),
            api_key=cfg.get_env("CATALYTICS_API_KEY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            company=cfg.get_env("TALLY_COMPANY"),
            batch_size=cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            limit=cfg.get_env_int("SYNC_LIMIT", 200),
            max_attempts=cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5),
            dry_run=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=False,
            log_file=None
        )
        
        config_obj = build_config(args)
        stats = run_once(config_obj)
        
        dashboard_logger.write_log(f"=== MANUAL: SYNC PRODUCTS COMPLETED - {stats.get('ok', 0)} synced ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Sync Products',
                'status': 'success',
                'detail': f"{stats.get('ok', 0)} synced"
            })
        
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception("Failed to sync products")
        dashboard_logger.write_log(f"=== MANUAL: SYNC PRODUCTS FAILED - {str(e)} ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Sync Products',
                'status': 'error',
                'detail': str(e)
            })
        
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/trigger/sync_invoices', methods=['POST'])
def trigger_sync_invoices():
    """Manually trigger invoice/DC sync to Catalytics"""
    try:
        from sync_catalytics import build_config, run_once
        from types import SimpleNamespace
        
        dashboard_logger.write_log("=== MANUAL: SYNC INVOICES STARTED ===")
        
        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=cfg.get_env("TALLY_DB_PATH"),
            api_base_url=cfg.get_env("CATALYTICS_API_BASE_URL"),
            api_key=cfg.get_env("CATALYTICS_API_KEY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            company=cfg.get_env("TALLY_COMPANY"),
            batch_size=cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            limit=cfg.get_env_int("SYNC_LIMIT", 200),
            max_attempts=cfg.get_env_int("SYNC_MAX_ATTEMPTS", 5),
            allow_tally_fetch=cfg.get_env_bool("SYNC_ALLOW_TALLY_FETCH", False),
            dry_run=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=False,
            log_file=None
        )
        
        config_obj = build_config(args)
        stats = run_once(config_obj)
        
        dashboard_logger.write_log(f"=== MANUAL: SYNC INVOICES COMPLETED - {stats.get('ok', 0)} synced ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Sync Invoices',
                'status': 'success',
                'detail': f"{stats.get('ok', 0)} synced"
            })
        
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        logger.exception("Failed to sync invoices")
        dashboard_logger.write_log(f"=== MANUAL: SYNC INVOICES FAILED - {str(e)} ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Sync Invoices',
                'status': 'error',
                'detail': str(e)
            })
        
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/data/customers')
def api_data_customers():
    """Get all customers with sync status"""
    try:
        import sqlite3 as _sqlite3
        db_path = config.SQLITE_DB_PATH
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row

        rows = conn.execute('''
            SELECT id, name, tally_company, gstin, phone, email, city, state,
                   is_synced, catalytics_id, first_fetched_at, last_sync_at,
                   sync_attempts, last_sync_error
            FROM customers
            ORDER BY first_fetched_at DESC
        ''').fetchall()

        customers = []
        for row in rows:
            customers.append({
                'id': row[0],
                'name': row[1],
                'tally_company': row[2],
                'gstin': row[3] or '-',
                'phone': row[4] or '-',
                'email': row[5] or '-',
                'city': row[6] or '-',
                'state': row[7] or '-',
                'is_synced': bool(row[8]),
                'catalytics_id': row[9],
                'first_fetched_at': row[10],
                'last_sync_at': row[11],
                'sync_attempts': row[12] or 0,
                'last_sync_error': row[13]
            })

        conn.close()
        return jsonify({'customers': customers, 'total': len(customers)})
    except Exception as e:
        logger.exception("Failed to fetch customers data")
        return jsonify({'customers': [], 'total': 0, 'error': str(e)}), 500


@app.route('/api/data/products')
def api_data_products():
    """Get all products with sync status"""
    try:
        import sqlite3 as _sqlite3
        db_path = config.SQLITE_DB_PATH
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row

        rows = conn.execute('''
            SELECT id, name, tally_company, hsn_code, unit, rate, description,
                   is_synced, catalytics_id, first_fetched_at, last_sync_at,
                   sync_attempts, last_sync_error
            FROM products
            ORDER BY first_fetched_at DESC
        ''').fetchall()

        products = []
        for row in rows:
            products.append({
                'id': row[0],
                'name': row[1],
                'tally_company': row[2],
                'hsn_code': row[3] or '-',
                'unit': row[4] or '-',
                'rate': float(row[5] or 0),
                'description': row[6] or '-',
                'is_synced': bool(row[7]),
                'catalytics_id': row[8],
                'first_fetched_at': row[9],
                'last_sync_at': row[10],
                'sync_attempts': row[11] or 0,
                'last_sync_error': row[12]
            })

        conn.close()
        return jsonify({'products': products, 'total': len(products)})
    except Exception as e:
        logger.exception("Failed to fetch products data")
        return jsonify({'products': [], 'total': 0, 'error': str(e)}), 500


@app.route('/api/data/invoices')
def api_data_invoices():
    """Get all invoices/DCs with sync status"""
    try:
        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)
        company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
        
        rows = conn.execute('''
            SELECT dn.id, dn.dc_no, dn.voucher_date, dn.party_ledger_name, dn.reference,
                   dn.updated_at,
                   ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at, ss.last_error
            FROM delivery_notes dn
            LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
            ORDER BY dn.updated_at DESC
        ''').fetchall()
        
        invoices = []
        for row in rows:
            invoices.append({
                'id': row[0],
                'voucher_no': row[1],
                'tally_company': company_name,
                'voucher_date': row[2],
                'customer_name': row[3],
                'total_amount': 0,
                'dc_no': row[1] or '-',
                'is_synced': bool(row[6]) if row[6] is not None else False,
                'catalytics_dc_id': None,
                'first_fetched_at': row[5],
                'last_sync_at': row[9],
                'sync_attempts': row[7] or 0,
                'last_sync_error': row[10],
                'order_status': None
            })
        
        conn.close()
        return jsonify({'invoices': invoices, 'total': len(invoices)})
    except Exception as e:
        logger.exception("Failed to fetch invoices data")
        return jsonify({'invoices': [], 'total': 0, 'error': str(e)}), 500
@app.route('/api/bulk/mark-unsynced-customers', methods=['POST'])
def bulk_mark_unsynced_customers():
    """Mark selected customers as unsynced"""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])

        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = config.SQLITE_DB_PATH
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE customers SET is_synced = 0, sync_attempts = 0, last_sync_error = NULL WHERE id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Marked {len(ids)} customers as unsynced ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Mark Unsynced',
                'status': 'success',
                'detail': f"{len(ids)} customers marked as unsynced"
            })

        return jsonify({'success': True, 'message': f'{len(ids)} customers marked as unsynced'})
    except Exception as e:
        logger.exception("Failed to mark customers as unsynced")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/mark-unsynced-products', methods=['POST'])
def bulk_mark_unsynced_products():
    """Mark selected products as unsynced"""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])

        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = config.SQLITE_DB_PATH
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE products SET is_synced = 0, sync_attempts = 0, last_sync_error = NULL WHERE id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Marked {len(ids)} products as unsynced ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Mark Unsynced',
                'status': 'success',
                'detail': f"{len(ids)} products marked as unsynced"
            })

        return jsonify({'success': True, 'message': f'{len(ids)} products marked as unsynced'})
    except Exception as e:
        logger.exception("Failed to mark products as unsynced")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/mark-unsynced-invoices', methods=['POST'])
def bulk_mark_unsynced_invoices():
    """Mark selected invoices/DCs as unsynced"""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])

        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE sync_status SET is_synced = 0, attempts = 0 WHERE delivery_note_id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Marked {len(ids)} invoices as unsynced ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Mark Unsynced',
                'status': 'success',
                'detail': f"{len(ids)} invoices marked as unsynced"
            })

        return jsonify({'success': True, 'message': f'{len(ids)} invoices marked as unsynced'})
    except Exception as e:
        logger.exception("Failed to mark invoices as unsynced")
        return jsonify({'success': False, 'error': str(e)}), 500


# Individual record resync endpoints
def _refetch_one_customer(customer_id: int) -> dict:
    """Refresh a single customer from Tally and mark it pending sync."""
    import json as _json
    from fetch_customers import _map_ledger_to_customer
    import tally_api

    db_path = config.SQLITE_DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            return {'success': False, 'error': 'Customer not found'}

        customer_name = (row['name'] or '').strip()
        company_name = (row['tally_company'] or cfg.get_env('TALLY_COMPANY', '')).strip()
        if not customer_name:
            return {'success': False, 'error': 'Customer name is empty'}
        if not company_name:
            return {'success': False, 'error': f'No Tally company configured for "{customer_name}"'}

        lookup_name = customer_name
        data_json = row['data_json'] or ''
        if data_json:
            try:
                saved_ledger = _json.loads(data_json)
                lookup_name = (saved_ledger.get('NAME') or saved_ledger.get('LEDGERNAME') or customer_name).strip() or customer_name
            except Exception:
                pass

        ledger = tally_api.get_ledger_by_name(company_name, lookup_name, config.TALLY_URL)
        if not ledger and lookup_name != customer_name:
            ledger = tally_api.get_ledger_by_name(company_name, customer_name, config.TALLY_URL)
        if not ledger:
            return {'success': False, 'error': f'"{customer_name}" not found in Tally for {company_name}'}

        customer_data = _map_ledger_to_customer(ledger, company_name)
        updated_name = customer_data.get('name') or customer_name

        db_obj = db.Database(db_path)
        try:
            db_obj.update_customer(customer_id, customer_data)
        finally:
            db_obj.close()

        return {'success': True, 'message': f'"{updated_name}" refetched from Tally'}
    finally:
        conn.close()


def _sync_one_customer(customer_id: int) -> dict:
    """Immediately sync a single customer to Catalytics. Returns result dict."""
    import json as _json
    db_path = config.SQLITE_DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            return {'success': False, 'error': 'Customer not found'}

        name = row['name'] or ''
        data_json = row['data_json'] or ''
        if not data_json:
            return {'success': False, 'error': f'No Tally data for "{name}" â€” run fetch first'}

        try:
            ledger = _json.loads(data_json)
        except Exception:
            return {'success': False, 'error': 'Invalid data_json in customer record'}

        # Clean GSTIN (strip leading colon)
        for k in ('GSTIN', 'PARTYGSTIN'):
            if isinstance(ledger.get(k), str) and ledger[k].startswith(':'):
                ledger[k] = ledger[k].lstrip(':')

        api_base = cfg.get_env('CATALYTICS_API_BASE_URL', '')
        entity_id = cfg.get_env_int('CATALYTICS_ENTITY_ID')
        _base = api_base.rstrip('/')
        endpoint = (_base + '/tally-customer-payload/') if _base.endswith('/import') else (_base + '/import/tally-customer-payload/')
        # Payload endpoints use AllowAny permission â€” no auth header needed
        headers = {'Content-Type': 'application/json'}

        payload = {'entity_id': entity_id, 'ledger': ledger}
        company = (row['tally_company'] or cfg.get_env('TALLY_COMPANY', '')).strip()
        if company:
            payload['company_name'] = company

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=30)
        if resp.status_code in (200, 201):
            result = resp.json()
            if result.get('status') != 'success':
                raise ValueError(result.get('message', 'API non-success'))
            data = result.get('data', {})
            catalytics_id = None
            for r in data.get('results', []):
                if isinstance(r, dict):
                    catalytics_id = r.get('customer_id') or r.get('id')
                    break
            resp_json = _json.dumps(result)
            conn.execute(
                "UPDATE customers SET is_synced=1, catalytics_id=?, last_response_json=?, "
                "last_sync_at=CURRENT_TIMESTAMP, last_sync_error=NULL, sync_attempts=sync_attempts+1 WHERE id=?",
                (catalytics_id, resp_json, customer_id)
            )
            conn.commit()
            created = data.get('created', 0)
            return {'success': True, 'message': f'"{name}" synced ({"created" if created else "updated"})', 'catalytics_id': catalytics_id}
        else:
            err = f'HTTP {resp.status_code}'
            try:
                err += f' - {resp.json().get("message", resp.text[:200])}'
            except Exception:
                pass
            conn.execute(
                "UPDATE customers SET sync_attempts=sync_attempts+1, last_sync_error=?, last_sync_at=CURRENT_TIMESTAMP WHERE id=?",
                (err, customer_id)
            )
            conn.commit()
            return {'success': False, 'error': err}
    except Exception as e:
        try:
            conn.execute(
                "UPDATE customers SET sync_attempts=sync_attempts+1, last_sync_error=?, last_sync_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(e), customer_id)
            )
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _refetch_one_product(product_id: int) -> dict:
    """Refresh a single product from Tally and mark it pending sync."""
    from fetch_products import _map_stock_item_to_product, parse_stock_item_name
    import tally_api

    db_path = config.SQLITE_DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not row:
            return {'success': False, 'error': 'Product not found'}

        product_name = (row['name'] or '').strip()
        company_name = (row['tally_company'] or cfg.get_env('TALLY_COMPANY', '')).strip()
        if not product_name:
            return {'success': False, 'error': 'Product name is empty'}
        if not company_name:
            return {'success': False, 'error': f'No Tally company configured for "{product_name}"'}

        item = tally_api.get_stock_item_by_name(company_name, product_name, config.TALLY_URL)
        if not item:
            return {'success': False, 'error': f'"{product_name}" not found in Tally for {company_name}'}

        parsed = parse_stock_item_name(product_name)
        if not parsed:
            name_lower = product_name.lower()
            matched_keyword = next(
                (kw for kw in config.PRODUCT_EXACT_KEYWORDS if kw in name_lower),
                None
            )
            if not matched_keyword:
                return {'success': False, 'error': f'Unable to parse "{product_name}" with current product rules'}
            parsed = {
                'product_master_name': product_name,
                'variant_name': '',
                'unit_name': '',
                'product_type_code': '',
                'product_type_name': '',
                'canonical_name': product_name,
            }

        product_data = _map_stock_item_to_product(item, company_name, parsed)
        product_data['name_canonical'] = parsed['canonical_name']

        db_obj = db.Database(db_path)
        try:
            db_obj.update_product(product_id, product_data)
        finally:
            db_obj.close()

        return {'success': True, 'message': f'"{product_name}" refetched from Tally'}
    finally:
        conn.close()


def _sync_one_product(product_id: int) -> dict:
    """Immediately sync a single product to Catalytics. Returns result dict."""
    import json as _json
    db_path = config.SQLITE_DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not row:
            return {'success': False, 'error': 'Product not found'}

        name = row['name'] or ''
        if not row['product_master_name']:
            return {'success': False, 'error': f'No parsed fields for "{name}" â€” run fetch first'}

        guid = ''
        if row['data_json']:
            try:
                stock_data = _json.loads(row['data_json'])
                guid = stock_data.get('GUID') or stock_data.get('MASTERID') or stock_data.get('REMOTEID') or ''
            except Exception:
                pass

        api_base = cfg.get_env('CATALYTICS_API_BASE_URL', '')
        entity_id = cfg.get_env_int('CATALYTICS_ENTITY_ID')
        _base = api_base.rstrip('/')
        endpoint = (_base + '/tally-product_name-payload/') if _base.endswith('/import') else (_base + '/import/tally-product_name-payload/')
        # Payload endpoints use AllowAny permission â€” no auth header needed
        headers = {'Content-Type': 'application/json'}

        payload = {
            'entity_id': entity_id,
            'stock_item_name': name,
            'product_master_name': row['product_master_name'],
            'unit_master_name': row['unit_name'],
            'variant_name': row['variant_name'],
            'product_type_code': row['product_type_code'],
            'product_type_name': row['product_type_name'],
            'hsn_code': row['hsn_code'] or '',
            'guid': str(guid).strip(),
            'rate': row['rate'] or 0.0,
            'gst_applicable': row['gst_applicable'] or '',
            'gst_rate': row['gst_rate'] or 0.0,
            'igst_rate': row['igst_rate'] or 0.0,
            'cgst_rate': row['cgst_rate'] or 0.0,
            'sgst_rate': row['sgst_rate'] or 0.0,
            'tally_company': row['tally_company'],
        }

        conn.execute(
            "UPDATE products SET sync_request_json=? WHERE id=?",
            (_json.dumps(payload), product_id)
        )
        conn.commit()

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=30)
        if resp.status_code in (200, 201):
            result = resp.json()
            if result.get('status') != 'success':
                raise ValueError(result.get('message', 'API non-success'))
            data = result.get('data', {})
            if data.get('errors', 0) > 0:
                raise ValueError(data.get('results', [{}])[0].get('message', 'API error'))
            catalytics_id = data.get('product_id') or data.get('id')
            if not catalytics_id:
                for r in data.get('results', []):
                    if isinstance(r, dict):
                        catalytics_id = r.get('product_id') or r.get('id')
                        break
            resp_json = _json.dumps(result)
            conn.execute(
                "UPDATE products SET is_synced=1, catalytics_id=?, last_response_json=?, "
                "last_sync_at=CURRENT_TIMESTAMP, last_sync_error=NULL, sync_attempts=sync_attempts+1 WHERE id=?",
                (catalytics_id, resp_json, product_id)
            )
            conn.commit()
            created = data.get('created', 0)
            return {'success': True, 'message': f'"{name}" synced ({"created" if created else "updated"})', 'catalytics_id': catalytics_id}
        else:
            err = f'HTTP {resp.status_code}'
            try:
                err += f' - {resp.json().get("message", resp.text[:200])}'
            except Exception:
                pass
            conn.execute(
                "UPDATE products SET sync_attempts=sync_attempts+1, last_sync_error=?, last_sync_at=CURRENT_TIMESTAMP WHERE id=?",
                (err, product_id)
            )
            conn.commit()
            return {'success': False, 'error': err}
    except Exception as e:
        try:
            conn.execute(
                "UPDATE products SET sync_attempts=sync_attempts+1, last_sync_error=?, last_sync_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(e), product_id)
            )
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _refetch_one_invoice(invoice_id: int) -> dict:
    """Refresh a single invoice/DC from Tally and mark it pending sync."""
    from fetch_invoices import _default_date_range, _extract_tally_guid, _normalize_dc_no
    import tally_api

    tally_db_path = cfg.get_env('TALLY_DB_PATH')
    if not tally_db_path:
        return {'success': False, 'error': 'TALLY_DB_PATH not configured'}

    tally_url = cfg.get_env('TALLY_URL', 'http://localhost:9000/')
    conn = db.connect(tally_db_path)
    try:
        row = conn.execute(
            "SELECT * FROM delivery_notes WHERE id = ?", (invoice_id,)
        ).fetchone()
        if not row:
            return {'success': False, 'error': 'DC not found'}

        note = dict(row)
        dc_no = (note.get('dc_no') or '').strip()
        stored_guid = (note.get('tally_guid') or '').strip()
        voucher_date = ''.join(ch for ch in (note.get('voucher_date') or '') if ch.isdigit())[:8]

        company_row = conn.execute(
            "SELECT name, tally_name FROM companies WHERE id = ?",
            (note.get('company_id'),)
        ).fetchone()
        company = ''
        if company_row:
            company = (company_row['tally_name'] or company_row['name'] or '').strip()
        if not company:
            company = cfg.get_env('TALLY_COMPANY', '').strip()
        if not company:
            return {'success': False, 'error': f'No Tally company configured for DC "{dc_no or invoice_id}"'}

        search_ranges = []
        seen_ranges = set()

        def add_range(start_date, end_date):
            if not start_date or not end_date:
                return
            key = (start_date, end_date)
            if key in seen_ranges:
                return
            seen_ranges.add(key)
            search_ranges.append(key)

        if len(voucher_date) == 8:
            add_range(voucher_date, voucher_date)

        default_from, default_to = _default_date_range(cfg.get_env_int('TALLY_DAYS_BACK'))
        add_range(cfg.get_env('TALLY_FROM_DATE') or default_from, cfg.get_env('TALLY_TO_DATE') or default_to)

        fy_from, fy_to = _default_date_range(None)
        add_range(fy_from, fy_to)

        matched_voucher = None
        for from_date, to_date in search_ranges:
            vouchers = tally_api.get_delivery_notes(company, tally_url, from_date, to_date)
            for voucher in vouchers:
                voucher_guid = _extract_tally_guid(voucher).strip()
                voucher_dc_no = _normalize_dc_no(voucher).strip()
                if stored_guid and voucher_guid and voucher_guid == stored_guid:
                    matched_voucher = dict(voucher)
                    break
                if dc_no and voucher_dc_no and voucher_dc_no == dc_no:
                    matched_voucher = dict(voucher)
                    break
            if matched_voucher:
                break

        if not matched_voucher:
            return {'success': False, 'error': f'DC "{dc_no or invoice_id}" not found in Tally for {company}'}

        matched_guid = _extract_tally_guid(matched_voucher).strip()
        matched_dc_no = _normalize_dc_no(matched_voucher).strip() or dc_no or str(invoice_id)
        if stored_guid and matched_guid and stored_guid == matched_guid and dc_no:
            matched_dc_no = dc_no
        matched_voucher['VOUCHERNUMBER'] = matched_dc_no

        updated_voucher_date = (matched_voucher.get('DATE') or note.get('voucher_date') or '').strip()
        party_name = (matched_voucher.get('PARTYLEDGERNAME') or matched_voucher.get('PARTYNAME') or '').strip()
        reference = (matched_voucher.get('REFERENCE') or matched_voucher.get('PONUMBER') or '').strip()

        dn_id = db.upsert_delivery_note(
            conn,
            company_id=note['company_id'],
            dc_no=matched_dc_no,
            voucher_date=updated_voucher_date,
            party_ledger_name=party_name,
            tally_guid=matched_guid,
            reference=reference,
            data=matched_voucher,
        )

        inventory_items = matched_voucher.get('INVENTORY') or []
        db.replace_delivery_note_items(conn, delivery_note_id=dn_id, items=inventory_items)

        if party_name:
            ledger_data = tally_api.get_ledger_by_name(company, party_name, tally_url)
            if ledger_data:
                db.upsert_json_row(
                    conn,
                    table='ledgers',
                    company_id=note['company_id'],
                    name=party_name,
                    data=ledger_data,
                )

        seen_stock_names = set()
        for item in inventory_items:
            stock_name = (item.get('STOCKITEMNAME') or item.get('ITEMNAME') or '').strip()
            if not stock_name or stock_name in seen_stock_names:
                continue
            seen_stock_names.add(stock_name)
            stock_data = tally_api.get_stock_item_by_name(company, stock_name, tally_url)
            if stock_data:
                db.upsert_json_row(
                    conn,
                    table='stock_items',
                    company_id=note['company_id'],
                    name=stock_name,
                    data=stock_data,
                )

        ts = db.now_ts()
        conn.execute(
            """
            INSERT INTO sync_status
                (delivery_note_id, is_synced, attempts, last_attempt_at, synced_at,
                 last_error, last_response_json, payload_hash, created_at, updated_at)
            VALUES (?, 0, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(delivery_note_id) DO UPDATE SET
                is_synced = 0,
                attempts = 0,
                last_attempt_at = NULL,
                synced_at = NULL,
                last_error = NULL,
                last_response_json = NULL,
                payload_hash = NULL,
                updated_at = excluded.updated_at
            """,
            (dn_id, ts, ts),
        )
        conn.commit()

        return {'success': True, 'message': f'DC "{matched_dc_no}" refetched from Tally', 'invoice_id': dn_id}
    finally:
        conn.close()


def _sync_one_invoice(invoice_id: int) -> dict:
    """Immediately sync a single invoice/DC to Catalytics. Returns result dict."""
    import json as _json
    from sync_catalytics import (
        _build_payload_for_note,
        _update_sync_status,
        _fetch_unsynced_instant_dcs,
        _find_matching_instant_dc,
        _mark_instant_dc_synced_on_portal,
        _lookup_dc_id_by_no,
    )

    tally_db_path = cfg.get_env('TALLY_DB_PATH')
    if not tally_db_path:
        return {'success': False, 'error': 'TALLY_DB_PATH not configured'}

    conn = db.connect(tally_db_path)
    try:
        row = conn.execute(
            "SELECT * FROM delivery_notes WHERE id = ?", (invoice_id,)
        ).fetchone()
        if not row:
            return {'success': False, 'error': 'DC not found'}

        note = dict(row)
        dc_no = note.get('dc_no') or str(invoice_id)

        api_base = cfg.get_env('CATALYTICS_API_BASE_URL', '')
        api_key = cfg.get_env('CATALYTICS_API_KEY')
        entity_id = cfg.get_env_int('CATALYTICS_ENTITY_ID')
        company_row = conn.execute(
            "SELECT name, tally_name FROM companies WHERE id = ?",
            (note.get('company_id'),)
        ).fetchone()
        company = ''
        if company_row:
            company = (company_row['tally_name'] or company_row['name'] or '').strip()
        if not company:
            company = cfg.get_env('TALLY_COMPANY', '').strip()
        _base = api_base.rstrip('/')
        endpoint = (_base + '/tally-delivery-challan-payload/') if _base.endswith('/import') else (_base + '/import/tally-delivery-challan-payload/')
        # Payload endpoints use AllowAny permission â€” no auth header needed
        headers = {'Content-Type': 'application/json'}

        payload, payload_hash = _build_payload_for_note(
            conn, note,
            api_base_url=api_base,
            entity_id=entity_id,
            company_name=company,
            allow_tally_fetch=False,
        )
        voucher = payload.get('voucher') or {}
        items = voucher.get('INVENTORY') or []

        # Restore previously persisted matched_dc_id (survives a prior failed sync attempt)
        matched_dc_id = note.get('matched_dc_id') or None

        if matched_dc_id:
            voucher['MATCHED_DC_ID'] = matched_dc_id
            logger.info("Resync using persisted MATCHED_DC_ID=%s for dc_no=%s", matched_dc_id, dc_no)
        else:
            try:
                instant_dcs = _fetch_unsynced_instant_dcs(api_base, entity_id, api_key)
                logger.info("Resync fetched %d unsynced instant DCs from portal", len(instant_dcs))
                for _idc in instant_dcs:
                    logger.info(
                        "  Portal instant DC: id=%s dc_no=%s customer=%s date=%s products=%s",
                        _idc.get('id'), _idc.get('dc_no'),
                        (_idc.get('customer') or {}).get('name') if isinstance(_idc.get('customer'), dict) else _idc.get('customer_name'),
                        _idc.get('dc_date') or _idc.get('date'),
                        [i.get('product_name') or (i.get('product') or {}).get('name') for i in (_idc.get('order_details') or _idc.get('items') or [])],
                    )
                matched_dc = _find_matching_instant_dc(note, voucher, items, instant_dcs)
                if matched_dc and matched_dc.get('id'):
                    matched_dc_id = matched_dc.get('id')
                    voucher['MATCHED_DC_ID'] = matched_dc_id
                    # Persist immediately so it survives any future retry
                    db.update_delivery_note_matched_dc(conn, delivery_note_id=invoice_id, matched_dc_id=matched_dc_id)
                    conn.commit()
                    logger.info("Resync matched instant DC for dc_no=%s -> portal id=%s dc_no=%s",
                                dc_no, matched_dc_id, matched_dc.get('dc_no'))
                elif dc_no:
                    logger.info("Resync no instant match for dc_no=%s; trying dc_no lookup fallback", dc_no)
                    existing_dc_id = _lookup_dc_id_by_no(
                        api_base,
                        entity_id,
                        dc_no,
                        api_key,
                        expected_date=note.get('voucher_date') or voucher.get('DATE'),
                        expected_customer=note.get('party_ledger_name') or voucher.get('PARTYLEDGERNAME') or voucher.get('PARTYNAME'),
                    )
                    if existing_dc_id:
                        matched_dc_id = existing_dc_id
                        voucher['MATCHED_DC_ID'] = existing_dc_id
                        logger.info("Resync matched existing portal DC by dc_no=%s -> id=%s", dc_no, existing_dc_id)
                    else:
                        logger.info("Resync no existing portal DC found for dc_no=%s", dc_no)
            except Exception as _exc:
                logger.warning("Resync instant DC matching error for dc_no=%s: %s", dc_no, _exc)

        if matched_dc_id:
            logger.info("Resync MATCHED_DC_ID=%s set for dc_no=%s", matched_dc_id, dc_no)
        else:
            logger.info("Resync MATCHED_DC_ID not set for dc_no=%s — will create new DC", dc_no)

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=30)
        success = resp.status_code in (200, 201)
        resp_data = None
        error_text = None
        try:
            resp_data = resp.json()
        except Exception:
            pass

        if success and resp_data and resp_data.get('status') != 'success':
            success = False
            error_text = resp_data.get('message', f'HTTP {resp.status_code}')
        elif not success:
            error_text = resp_data.get('message', f'HTTP {resp.status_code}') if resp_data else f'HTTP {resp.status_code}'

        _update_sync_status(
            conn,
            delivery_note_id=invoice_id,
            success=success,
            payload_hash=payload_hash,
            response_json=resp_data,
            error_text=error_text,
        )

        if success and matched_dc_id:
            try:
                tally_voucher_no = str(voucher.get('VOUCHERNUMBER') or dc_no).strip()
                _mark_instant_dc_synced_on_portal(
                    api_base,
                    matched_dc_id,
                    tally_voucher_no=tally_voucher_no,
                    api_key=api_key,
                )
            except Exception:
                pass
        conn.commit()

        if success:
            return {'success': True, 'message': f'DC "{dc_no}" synced successfully'}
        else:
            return {'success': False, 'error': error_text or f'HTTP {resp.status_code}'}
    finally:
        conn.close()


@app.route('/api/customers/<int:customer_id>/resync', methods=['POST'])
def resync_customer(customer_id):
    """Immediately sync a single customer to Catalytics."""
    try:
        result = _sync_one_customer(customer_id)
        if result['success']:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Resync Customer',
                    'status': 'success',
                    'detail': result['message']
                })
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Failed to resync customer {customer_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/customers/<int:customer_id>/refetch', methods=['POST'])
def refetch_customer(customer_id):
    """Refetch a single customer from Tally and mark it pending sync."""
    try:
        result = _refetch_one_customer(customer_id)
        if result['success']:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Refetch Customer',
                    'status': 'success',
                    'detail': result['message']
                })
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Failed to refetch customer {customer_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/products/<int:product_id>/resync', methods=['POST'])
def resync_product(product_id):
    """Immediately sync a single product to Catalytics."""
    try:
        result = _sync_one_product(product_id)
        if result['success']:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Resync Product',
                    'status': 'success',
                    'detail': result['message']
                })
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Failed to resync product {product_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/products/<int:product_id>/refetch', methods=['POST'])
def refetch_product(product_id):
    """Refetch a single product from Tally and mark it pending sync."""
    try:
        result = _refetch_one_product(product_id)
        if result['success']:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Refetch Product',
                    'status': 'success',
                    'detail': result['message']
                })
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Failed to refetch product {product_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/invoices/<int:invoice_id>/refetch', methods=['POST'])
def refetch_invoice(invoice_id):
    """Refetch a single invoice/DC from Tally and mark it pending sync."""
    try:
        result = _refetch_one_invoice(invoice_id)
        if result['success']:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Refetch Invoice',
                    'status': 'success',
                    'detail': result['message']
                })
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Failed to refetch invoice {invoice_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/invoices/<int:invoice_id>/resync', methods=['POST'])
def resync_invoice(invoice_id):
    """Immediately sync a single invoice/DC to Catalytics."""
    try:
        result = _sync_one_invoice(invoice_id)
        if result['success']:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Resync Invoice',
                    'status': 'success',
                    'detail': result['message']
                })
        return jsonify(result)
    except Exception as e:
        logger.exception(f"Failed to resync invoice {invoice_id}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/delete-customers', methods=['POST'])
def bulk_delete_customers():
    """Delete selected customers from local SQLite."""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = config.SQLITE_DB_PATH
        conn = db.connect(db_path)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM customers WHERE id IN ({placeholders})', tuple(ids))
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Deleted {len(ids)} customers from local DB ===")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Delete Customers',
                'status': 'success',
                'detail': f"{len(ids)} customers deleted"
            })
        return jsonify({'success': True, 'message': f'{len(ids)} customers deleted'})
    except Exception as e:
        logger.exception("Failed to delete customers")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/delete-products', methods=['POST'])
def bulk_delete_products():
    """Delete selected products from local SQLite."""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = config.SQLITE_DB_PATH
        conn = db.connect(db_path)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM products WHERE id IN ({placeholders})', tuple(ids))
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Deleted {len(ids)} products from local DB ===")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Delete Products',
                'status': 'success',
                'detail': f"{len(ids)} products deleted"
            })
        return jsonify({'success': True, 'message': f'{len(ids)} products deleted'})
    except Exception as e:
        logger.exception("Failed to delete products")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/delete-invoices', methods=['POST'])
def bulk_delete_invoices():
    """Delete selected invoices/DCs from local SQLite."""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM sync_status WHERE delivery_note_id IN ({placeholders})', tuple(ids))
        conn.execute(f'DELETE FROM delivery_note_items WHERE delivery_note_id IN ({placeholders})', tuple(ids))
        conn.execute(f'DELETE FROM delivery_notes WHERE id IN ({placeholders})', tuple(ids))
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Deleted {len(ids)} invoices/DCs from local DB ===")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Delete Invoices',
                'status': 'success',
                'detail': f"{len(ids)} invoices/DCs deleted"
            })
        return jsonify({'success': True, 'message': f'{len(ids)} invoices/DCs deleted'})
    except Exception as e:
        logger.exception("Failed to delete invoices")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/retry-customers', methods=['POST'])
def bulk_retry_customers():
    """Retry syncing selected customers"""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])

        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(ids))
        cursor.execute(
            f'UPDATE customers SET sync_attempts = 0, last_sync_error = NULL WHERE id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Reset {updated} customers for retry ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Retry Customers',
                'status': 'success',
                'detail': f"{updated} customers reset for retry"
            })

        return jsonify({'success': True, 'message': f'{updated} customers reset for retry', 'count': updated})
    except Exception as e:
        logger.exception("Failed to retry customers")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/retry-products', methods=['POST'])
def bulk_retry_products():
    """Retry syncing selected products"""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])

        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(ids))
        cursor.execute(
            f'UPDATE products SET sync_attempts = 0, last_sync_error = NULL WHERE id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Reset {updated} products for retry ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Retry Products',
                'status': 'success',
                'detail': f"{updated} products reset for retry"
            })

        return jsonify({'success': True, 'message': f'{updated} products reset for retry', 'count': updated})
    except Exception as e:
        logger.exception("Failed to retry products")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/bulk/retry-invoices', methods=['POST'])
def bulk_retry_invoices():
    """Retry syncing selected invoices/DCs"""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])

        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE sync_status SET attempts = 0, last_error = NULL WHERE delivery_note_id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Reset {len(ids)} invoices for retry ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Retry Invoices',
                'status': 'success',
                'detail': f"{len(ids)} invoices reset for retry"
            })

        return jsonify({'success': True, 'message': f'{len(ids)} invoices reset for retry'})
    except Exception as e:
        logger.exception("Failed to retry invoices")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/customers/<int:customer_id>/detail')
def api_customer_detail(customer_id):
    """Get detailed customer information"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, tally_company, tally_guid, gstin, pan, address,
                   state, city, pincode, phone, email,
                   is_synced, catalytics_id, sync_attempts, last_sync_error,
                   first_fetched_at, last_updated_at, last_sync_at,
                   data_json, sync_request_json, last_response_json
            FROM customers
            WHERE id = ?
        ''', (customer_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Customer not found'}), 404
        name = row['name']
        return jsonify({
            'customer': {
                'id': row['id'],
                'name': name,
                'tally_company': row['tally_company'] or '',
                'tally_guid': row['tally_guid'] or '',
                'gstin': row['gstin'] or '',
                'pan': row['pan'] or '',
                'address': row['address'] or '',
                'state': row['state'] or '',
                'city': row['city'] or '',
                'pincode': row['pincode'] or '',
                'phone': row['phone'] or '',
                'email': row['email'] or '',
                'is_synced': bool(row['is_synced']),
                'catalytics_id': row['catalytics_id'],
                'sync_attempts': row['sync_attempts'] or 0,
                'last_sync_error': row['last_sync_error'],
                'first_fetched_at': row['first_fetched_at'],
                'last_updated_at': row['last_updated_at'],
                'last_sync_at': row['last_sync_at'],
                'data_json': row['data_json'],
                'sync_request_json': row['sync_request_json'],
                'last_response_json': row['last_response_json'],
                'fetch_log': get_log_excerpt_by_keyword('customer_fetch.log', name),
                'sync_log': get_log_excerpt_by_keyword('customer_sync.log', name)
            }
        })
    except Exception as e:
        logger.exception(f"Failed to get customer detail {customer_id}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/products/<int:product_id>/detail')
def api_product_detail(product_id):
    """Get detailed product information"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, tally_company, tally_guid, hsn_code, unit, rate, description,
                   product_master_name, variant_name, unit_name,
                   product_type_code, product_type_name,
                   gst_applicable, gst_rate, igst_rate, cgst_rate, sgst_rate,
                   is_synced, catalytics_id, sync_attempts, last_sync_error,
                   first_fetched_at, last_updated_at, last_sync_at,
                   data_json, last_response_json
            FROM products
            WHERE id = ?
        ''', (product_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return jsonify({'error': 'Product not found'}), 404
        name = row['name']
        return jsonify({
            'product': {
                'id': row['id'],
                'name': name,
                'tally_company': row['tally_company'] or '',
                'tally_guid': row['tally_guid'] or '',
                'hsn_code': row['hsn_code'] or '',
                'unit': row['unit'] or '',
                'rate': float(row['rate'] or 0),
                'description': row['description'] or '',
                'product_master_name': row['product_master_name'] or '',
                'variant_name': row['variant_name'] or '',
                'unit_name': row['unit_name'] or '',
                'product_type_code': row['product_type_code'] or '',
                'product_type_name': row['product_type_name'] or '',
                'gst_applicable': row['gst_applicable'] or '',
                'gst_rate': float(row['gst_rate'] or 0),
                'igst_rate': float(row['igst_rate'] or 0),
                'cgst_rate': float(row['cgst_rate'] or 0),
                'sgst_rate': float(row['sgst_rate'] or 0),
                'is_synced': bool(row['is_synced']),
                'catalytics_id': row['catalytics_id'],
                'sync_attempts': row['sync_attempts'] or 0,
                'last_sync_error': row['last_sync_error'],
                'first_fetched_at': row['first_fetched_at'],
                'last_updated_at': row['last_updated_at'],
                'last_sync_at': row['last_sync_at'],
                'data_json': row['data_json'],
                'last_response_json': row['last_response_json'],
                'fetch_log': get_log_excerpt_by_keyword('product_fetch.log', name),
                'sync_log': get_log_excerpt_by_keyword('product_sync.log', name)
            }
        })
    except Exception as e:
        logger.exception(f"Failed to get product detail {product_id}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/invoices/<int:invoice_id>/detail')
def api_invoice_detail(invoice_id):
    """Get detailed invoice/DC information from delivery_notes table"""
    try:
        tally_db_path = cfg.get_env("TALLY_DB_PATH")
        if not tally_db_path:
            return jsonify({'error': 'TALLY_DB_PATH not configured'}), 500

        conn = sqlite3.connect(tally_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT dn.id, dn.dc_no, dn.voucher_date, dn.party_ledger_name,
                   dn.reference, dn.data_json, dn.created_at, dn.updated_at,
                   ss.is_synced, ss.attempts, ss.last_attempt_at,
                   ss.synced_at, ss.last_error, ss.last_response_json
            FROM delivery_notes dn
            LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id
            WHERE dn.id = ?
        ''', (invoice_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return jsonify({'error': 'Invoice not found'}), 404

        # Fetch items
        items_cursor = conn.cursor()
        items_cursor.execute('''
            SELECT stock_name, qty, rate, amount, godown_name, data_json
            FROM delivery_note_items
            WHERE delivery_note_id = ?
            ORDER BY line_no
        ''', (invoice_id,))
        items = [dict(r) for r in items_cursor.fetchall()]
        conn.close()

        company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
        dc_no = row['dc_no'] or ''

        return jsonify({
            'invoice': {
                'id': row['id'],
                'voucher_no': dc_no,
                'dc_no': dc_no,
                'tally_company': company_name,
                'voucher_date': row['voucher_date'] or '',
                'customer_name': row['party_ledger_name'] or '',
                'reference': row['reference'] or '',
                'total_amount': 0,
                'tax_amount': 0,
                'is_synced': bool(row['is_synced']) if row['is_synced'] is not None else False,
                'catalytics_dc_id': None,
                'sync_attempts': row['attempts'] or 0,
                'last_sync_error': row['last_error'],
                'last_sync_at': row['synced_at'],
                'first_fetched_at': row['created_at'],
                'last_updated_at': row['updated_at'],
                'data_json': row['data_json'],
                'items_json': json.dumps(items) if items else None,
                'last_response_json': row['last_response_json'],
            }
        })
    except Exception as e:
        logger.exception(f"Failed to get invoice detail {invoice_id}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/debug/products-tally', methods=['GET'])
def debug_products_tally():
    """Debug endpoint for products"""
    return jsonify({'success': True, 'message': 'Debug endpoint - not implemented'})


@app.route('/api/diagnostics', methods=['GET'])
def api_diagnostics():
    """System diagnostics"""
    try:
        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        diagnostics = {
            'database': {
                'path': db_path,
                'exists': os.path.exists(db_path) if db_path else False,
                'size_mb': round(os.path.getsize(db_path) / (1024*1024), 2) if db_path and os.path.exists(db_path) else 0
            },
            'tally': {
                'url': cfg.get_env("TALLY_URL"),
                'company': cfg.get_env("TALLY_COMPANY"),
                'connected': check_tally_connection()
            },
            'catalytics': {
                'url': cfg.get_env("CATALYTICS_API_BASE_URL"),
                'entity_id': cfg.get_env_int("CATALYTICS_ENTITY_ID"),
                'connected': check_catalytics_connection()
            }
        }

        conn.close()
        return jsonify(diagnostics)
    except Exception as e:
        logger.exception("Failed to get diagnostics")
        return jsonify({'error': str(e)}), 500


@app.route('/api/verify/data-match', methods=['POST'])
def verify_data_match():
    """Run data matching verification"""
    try:
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Data Matching Started',
                'status': 'started',
                'detail': 'Verifying SQLite → Catalytics consistency'
            })

        # Local SQLite counts (customers + products)
        cust_total = cust_synced = cust_unsynced = 0
        prod_total = prod_synced = prod_unsynced = 0

        try:
            conn = sqlite3.connect(config.SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM customers")
            cust_total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM customers WHERE is_synced = 1")
            cust_synced = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM customers WHERE is_synced = 0")
            cust_unsynced = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM products")
            prod_total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM products WHERE is_synced = 1")
            prod_synced = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM products WHERE is_synced = 0")
            prod_unsynced = cursor.fetchone()[0]
            conn.close()
        except Exception as exc:
            logger.warning(f"Data match local SQLite read failed: {exc}")

        # Local DC counts (delivery_notes)
        inv_total = inv_synced = inv_unsynced = 0
        try:
            tally_db_path = cfg.get_env("TALLY_DB_PATH")
            if tally_db_path:
                conn2 = db.connect(tally_db_path)
                inv_total = conn2.execute("SELECT COUNT(*) FROM delivery_notes").fetchone()[0]
                inv_synced = conn2.execute(
                    "SELECT COUNT(*) FROM delivery_notes dn LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id WHERE COALESCE(ss.is_synced, 0) = 1"
                ).fetchone()[0]
                inv_unsynced = conn2.execute(
                    "SELECT COUNT(*) FROM delivery_notes dn LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id WHERE COALESCE(ss.is_synced, 0) = 0"
                ).fetchone()[0]
                conn2.close()
        except Exception as exc:
            logger.warning(f"Data match DC SQLite read failed: {exc}")

        # Catalytics counts (PostgreSQL) - optional
        cat_cust = cat_prod = cat_inv = 0
        pg_error = None
        try:
            import psycopg2
            pg_host = cfg.get_env("POSTGRES_HOST")
            pg_port = cfg.get_env("POSTGRES_PORT")
            pg_db = cfg.get_env("POSTGRES_DB")
            pg_user = cfg.get_env("POSTGRES_USER")
            pg_password = cfg.get_env("POSTGRES_PASSWORD")

            if not all([pg_host, pg_port, pg_db, pg_user, pg_password]):
                raise ValueError("PostgreSQL env vars missing (POSTGRES_HOST/PORT/DB/USER/PASSWORD)")

            pg_conn = psycopg2.connect(
                host=pg_host,
                port=pg_port,
                database=pg_db,
                user=pg_user,
                password=pg_password,
            )
            pg_cursor = pg_conn.cursor()
            pg_cursor.execute('SELECT COUNT(*) FROM "master.Customer"')
            cat_cust = pg_cursor.fetchone()[0]
            pg_cursor.execute('SELECT COUNT(*) FROM "master.product"')
            cat_prod = pg_cursor.fetchone()[0]
            pg_cursor.execute('SELECT COUNT(*) FROM "transaction.delivery_challan"')
            cat_inv = pg_cursor.fetchone()[0]
            pg_conn.close()
        except Exception as exc:
            pg_error = str(exc)
            logger.warning(f"PostgreSQL data match skipped: {pg_error}")

        results = {
            'entity_name': config.ENTITY_NAME,
            'entity_id': config.ENTITY_ID,
            'timestamp': datetime.now().isoformat(),
            'customers': {
                'sqlite_total': cust_total,
                'sqlite_synced': cust_synced,
                'sqlite_unsynced': cust_unsynced,
                'catalytics_total': cat_cust,
                'count_match': cust_synced == cat_cust,
                'difference': cust_synced - cat_cust,
            },
            'products': {
                'sqlite_total': prod_total,
                'sqlite_synced': prod_synced,
                'sqlite_unsynced': prod_unsynced,
                'catalytics_total': cat_prod,
                'count_match': prod_synced == cat_prod,
                'difference': prod_synced - cat_prod,
            },
            'invoices': {
                'sqlite_total': inv_total,
                'sqlite_synced': inv_synced,
                'sqlite_unsynced': inv_unsynced,
                'catalytics_total': cat_inv,
                'count_match': inv_synced == cat_inv,
                'difference': inv_synced - cat_inv,
            },
            'overall_match': False,
            'pg_error': pg_error,
        }

        results['overall_match'] = (
            results['customers']['count_match']
            and results['products']['count_match']
            and results['invoices']['count_match']
        )

        status = 'success' if results.get('overall_match') else 'warning'
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Data Matching Complete',
                'status': status,
                'detail': f"Overall: {'All Match' if results.get('overall_match') else 'Mismatches Found'}"
            })

        return jsonify({'success': True, 'results': results})

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Data matching failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/open-db', methods=['POST'])
def open_db():
    """Open a SQLite database file in DB Browser for SQLite."""
    try:
        data = request.get_json() or {}
        db_type = data.get('db_type', 'master')

        if db_type == 'tally':
            db_path = cfg.get_env('TALLY_DB_PATH') or ''
            label = 'Tally DC DB'
        else:
            db_path = config.SQLITE_DB_PATH or ''
            label = 'Master DB'

        if not db_path:
            return jsonify({'success': False, 'error': f'{label} path not configured'}), 400

        # Resolve relative path
        if db_path and not os.path.isabs(db_path):
            db_path = os.path.join(ROOT_DIR, db_path)

        if not os.path.exists(db_path):
            return jsonify({'success': False, 'error': f'{label} file not found: {db_path}'}), 404

        # Try common SQLite browser executables
        candidates = ['sqlitebrowser', 'DB Browser for SQLite', 'sqlitebrowser.exe']
        launched = False
        for exe in candidates:
            try:
                subprocess.Popen([exe, db_path], close_fds=True)
                launched = True
                break
            except FileNotFoundError:
                continue

        if not launched:
            # Fallback: open containing folder
            folder = os.path.dirname(db_path)
            if os.name == 'nt':
                subprocess.Popen(['explorer', folder])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
            return jsonify({
                'success': True,
                'message': f'DB Browser not found â€” opened folder: {folder}. Install DB Browser for SQLite to open files directly.'
            })

        return jsonify({'success': True, 'message': f'Opened {label}: {os.path.basename(db_path)}'})

    except Exception as e:
        logger.error(f'Failed to open DB: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/clear-database', methods=['POST'])
def clear_database():
    """Delete the entire SQLite database file"""
    try:
        db_path = cfg.get_env("TALLY_DB_PATH")
        if not db_path:
            return jsonify({'success': False, 'error': 'Database path not configured'}), 400

        # Check if file exists
        if not os.path.exists(db_path):
            return jsonify({'success': True, 'message': 'Database file does not exist (already deleted)'}), 200

        # Stop automation first to release DB locks
        manager = get_manager()
        status = manager.get_status() or {}
        was_running = status.get('status') == 'running'
        if was_running:
            manager.stop()
            time.sleep(1.2)

        # Try deleting main DB + sidecar files with retries
        paths_to_delete = [db_path, f"{db_path}-wal", f"{db_path}-shm", f"{db_path}-journal"]
        delete_errors = []
        for path in paths_to_delete:
            if not os.path.exists(path):
                continue
            deleted = False
            for _ in range(8):
                try:
                    os.remove(path)
                    deleted = True
                    break
                except FileNotFoundError:
                    deleted = True
                    break
                except PermissionError as e:
                    delete_errors.append(f"{os.path.basename(path)}: {e}")
                    time.sleep(0.25)
                except Exception as e:
                    delete_errors.append(f"{os.path.basename(path)}: {e}")
                    break
            if not deleted and os.path.exists(path):
                return jsonify({
                    'success': False,
                    'error': f'Unable to delete {os.path.basename(path)}. It is still in use.'
                }), 500

        dashboard_logger.write_log(f"=== DATABASE FILE DELETED: {db_path} ===")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Clear Database',
                'status': 'success',
                'detail': f'Database file deleted: {db_path}'
            })

        # Recreate fresh empty DB immediately
        initialize_database_file(
            db_path,
            include_master_tables=_same_db_path(db_path, config.SQLITE_DB_PATH),
        )
        dashboard_logger.write_log("=== NEW EMPTY DATABASE INITIALIZED ===")

        # Restart automation if it was running before clear
        if was_running:
            manager.start()
            dashboard_logger.write_log("=== AUTOMATION RESTARTED AFTER DB CLEAR ===")

        return jsonify({
            'success': True,
            'message': f'Database cleared successfully: {db_path}',
            'warnings': delete_errors[:3]
        })

    except Exception as e:
        logger.exception("Failed to clear database")
        return jsonify({'success': False, 'error': str(e)}), 500



def _env_flag(name, default='false'):
    """Read boolean-like values from environment flags."""
    value = os.getenv(name, default)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def maybe_open_dashboard_browser():
    """Open dashboard URL automatically on EXE launch."""
    default_auto_open = 'true' if getattr(sys, 'frozen', False) else 'false'
    if not _env_flag('AUTO_OPEN_DASHBOARD', default_auto_open):
        return

    host = config.WEB_UI_HOST
    if host in ('0.0.0.0', '::', ''):
        host = 'localhost'

    url = f"http://{host}:{config.WEB_UI_PORT}"

    def _open():
        try:
            webbrowser.open(url, new=1)
        except Exception as exc:
            logger.warning(f"Could not auto-open browser: {exc}")

    threading.Timer(1.0, _open).start()


def maybe_start_automation():
    """Start background automation loops automatically when enabled."""
    default_auto_start = 'true' if getattr(sys, 'frozen', False) else 'false'
    if not _env_flag('AUTO_START_AUTOMATION', default_auto_start):
        return

    try:
        manager = get_manager()
        started = manager.start()
        if started:
            logger.info("Automation auto-started on dashboard launch")
            dashboard_logger.write_log("=== AUTO: AUTOMATION STARTED ON DASHBOARD LAUNCH ===")
        else:
            logger.info("Automation already running on dashboard launch")
    except Exception as exc:
        logger.error(f"Failed to auto-start automation: {exc}")


def maybe_register_windows_startup():
    """Register packaged EXE in HKCU Run for auto-start on Windows login."""
    if os.name != 'nt':
        return

    if not _env_flag('AUTO_REGISTER_WINDOWS_STARTUP', 'false'):
        return

    if not getattr(sys, 'frozen', False):
        logger.info("Skipping Windows startup registration (not running as EXE)")
        return

    try:
        import winreg

        app_name = os.getenv('WINDOWS_STARTUP_APP_NAME', 'ChennaiOxygenMiddlewareDashboard').strip() or 'ChennaiOxygenMiddlewareDashboard'
        exe_path = f'"{sys.executable}"'
        exe_env_path = Path(sys.executable).with_name('.env')
        env_path = str(exe_env_path if exe_env_path.exists() else cfg.get_env_path())

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\\Microsoft\\Windows\\CurrentVersion\\Run',
            0,
            winreg.KEY_SET_VALUE
        ) as run_key:
            winreg.SetValueEx(run_key, app_name, 0, winreg.REG_SZ, exe_path)

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r'Environment'
        ) as env_key:
            winreg.SetValueEx(env_key, 'TALLY_ENV_PATH', 0, winreg.REG_SZ, env_path)

        logger.info(f"Windows startup registration ensured for: {app_name}")
        logger.info(f"Windows env path ensured for startup: {env_path}")
    except Exception as exc:
        logger.warning(f"Could not register Windows startup: {exc}")


if __name__ == '__main__':
    ensure_runtime_support_files()

    # Setup logging
    log_level = cfg.get_env("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Route ALL Python logging (from every module/thread) to the dashboard terminal
    class _DashboardLogHandler(logging.Handler):
        def emit(self, record):
            try:
                dashboard_logger.write_raw(self.format(record))
            except Exception:
                pass

    _dh = _DashboardLogHandler()
    _dh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logging.getLogger().addHandler(_dh)

    maybe_toggle_existing_instance_on_launch()

    try:
        initialize_configured_databases()
    except Exception as e:
        logger.error(f"Failed to initialize configured databases: {e}")

    default_debug = 'false' if getattr(sys, 'frozen', False) else 'true'
    dashboard_debug = _env_flag('DASHBOARD_DEBUG', default_debug)

    print(f"""
============================================================
  Chennai Oxygen Middleware Dashboard
  Entity: {config.ENTITY_NAME} (ID: {config.ENTITY_ID})
  Active Companies: {', '.join(config.get_active_companies())}
  Dashboard URL: http://{config.WEB_UI_HOST}:{config.WEB_UI_PORT}
  Auto Start Automation: {_env_flag('AUTO_START_AUTOMATION', 'true' if getattr(sys, 'frozen', False) else 'false')}
  Auto Register Startup: {_env_flag('AUTO_REGISTER_WINDOWS_STARTUP', 'false')}
============================================================
    """)

    maybe_register_windows_startup()
    maybe_start_automation()
    maybe_open_dashboard_browser()
    app.run(host=config.WEB_UI_HOST, port=config.WEB_UI_PORT, debug=dashboard_debug, use_reloader=False)


