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

# Determine template folder — inside _MEIPASS when frozen, else default
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


@app.route('/')
def index():
    """Main dashboard page"""
    company_name = cfg.get_env("TALLY_COMPANY", "Unknown Company")
    entity_id = cfg.get_env_int("CATALYTICS_ENTITY_ID", 0)
    return render_template('dashboard.html',
                         company_name=company_name,
                         entity_id=entity_id)


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
            'invoice_fetch_start_date': cfg.get_env("TALLY_FROM_DATE", "Today"),
            'product_type_map': config.PRODUCT_TYPE_MAP
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


@app.route('/api/logs')
def api_logs():
    """Get recent log entries"""
    lines = int(request.args.get('lines', 100))
    log_type = request.args.get('type', 'main')  # main, fetch, sync
    logs = dashboard_logger.get_logs(lines)
    return jsonify({'lines': logs, 'total': len(logs)})


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
    """Get recent logs - alias for /api/logs"""
    lines = int(request.args.get('lines', 100))
    logs = dashboard_logger.get_logs(lines)
    return jsonify({'lines': logs, 'total': len(logs)})


@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    """Clear logs"""
    dashboard_logger.clear()
    return jsonify({'success': True, 'message': 'Logs cleared'})


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

        cust_stats = fetch_customers_from_all_companies()
        prod_stats = fetch_products_from_all_companies()

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
        
        # Sync DCs
        dc_config = build_dc_config(args)
        dc_stats = sync_dc(dc_config)
        
        # Sync customers
        cust_config = build_cust_config(args)
        cust_stats = sync_cust(cust_config)
        
        # Sync products
        prod_config = build_prod_config(args)
        prod_stats = sync_prod(prod_config)
        
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
            db_path=cfg.get_env("TALLY_DB_PATH"),
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
            db_path=cfg.get_env("TALLY_DB_PATH"),
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
            return {'success': False, 'error': f'No Tally data for "{name}" — run fetch first'}

        try:
            ledger = _json.loads(data_json)
        except Exception:
            return {'success': False, 'error': 'Invalid data_json in customer record'}

        # Clean GSTIN (strip leading colon)
        for k in ('GSTIN', 'PARTYGSTIN'):
            if isinstance(ledger.get(k), str) and ledger[k].startswith(':'):
                ledger[k] = ledger[k].lstrip(':')

        api_base = cfg.get_env('CATALYTICS_API_BASE_URL', '')
        api_key = cfg.get_env('CATALYTICS_API_KEY', '')
        entity_id = cfg.get_env_int('CATALYTICS_ENTITY_ID')
        endpoint = api_base.rstrip('/') + '/tally-customer-payload/'
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['X-API-Key'] = api_key

        payload = {'entity_id': entity_id, 'ledger': ledger}
        company = cfg.get_env('TALLY_COMPANY')
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
            return {'success': False, 'error': f'No parsed fields for "{name}" — run fetch first'}

        guid = ''
        if row['data_json']:
            try:
                stock_data = _json.loads(row['data_json'])
                guid = stock_data.get('GUID') or stock_data.get('MASTERID') or stock_data.get('REMOTEID') or ''
            except Exception:
                pass

        api_base = cfg.get_env('CATALYTICS_API_BASE_URL', '')
        api_key = cfg.get_env('CATALYTICS_API_KEY', '')
        entity_id = cfg.get_env_int('CATALYTICS_ENTITY_ID')
        endpoint = api_base.rstrip('/') + '/tally-product_name-payload/'
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['X-API-Key'] = api_key

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


def _sync_one_invoice(invoice_id: int) -> dict:
    """Immediately sync a single invoice/DC to Catalytics. Returns result dict."""
    import json as _json
    from sync_catalytics import _build_payload_for_note, _update_sync_status

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
        api_key = cfg.get_env('CATALYTICS_API_KEY', '')
        entity_id = cfg.get_env_int('CATALYTICS_ENTITY_ID')
        company = cfg.get_env('TALLY_COMPANY')
        endpoint = api_base.rstrip('/') + '/tally-delivery-challan-payload/'
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['X-API-Key'] = api_key

        payload, payload_hash = _build_payload_for_note(
            conn, note,
            entity_id=entity_id,
            company_name=company,
            allow_tally_fetch=False,
        )

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
    """Verify data match between Tally and Catalytics"""
    return jsonify({'success': True, 'message': 'Verification endpoint - not implemented'})


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
                'message': f'DB Browser not found — opened folder: {folder}. Install DB Browser for SQLite to open files directly.'
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
        conn = db.connect(db_path)
        db.init_db(conn)
        conn.close()
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

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\\Microsoft\\Windows\\CurrentVersion\\Run',
            0,
            winreg.KEY_SET_VALUE
        ) as run_key:
            winreg.SetValueEx(run_key, app_name, 0, winreg.REG_SZ, exe_path)

        logger.info(f"Windows startup registration ensured for: {app_name}")
    except Exception as exc:
        logger.warning(f"Could not register Windows startup: {exc}")


if __name__ == '__main__':
    # Ensure logs directory exists (next to the exe, not in CWD)
    os.makedirs(str(BASE_DIR / 'logs'), exist_ok=True)

    # Setup logging
    log_level = cfg.get_env("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Initialize DC database (TALLY_DB_PATH)
    db_path = cfg.get_env("TALLY_DB_PATH")
    if db_path:
        try:
            conn = db.connect(db_path)
            db.init_db(conn)
            conn.close()
        except Exception as e:
            logger.error(f"Failed to initialize DC database: {e}")

    # Initialize master data database (SQLITE_DB_PATH: customers/products tables)
    try:
        from db import Database as _Database
        _master_db = _Database(config.SQLITE_DB_PATH)
        _master_db.close()
    except Exception as e:
        logger.error(f"Failed to initialize master database: {e}")

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
