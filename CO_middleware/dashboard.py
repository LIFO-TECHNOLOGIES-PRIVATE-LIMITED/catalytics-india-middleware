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

import config as cfg
import db
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

# Activity tracking
activity_log = deque(maxlen=50)
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
    """Get current system status - matches arasan API structure"""
    # Check connections
    tally_online = check_tally_connection()
    catalytics_online = check_catalytics_connection()
    
    # Get automation status
    manager = get_manager()
    automation_status = manager.get_status()
    middleware_online = (automation_status.get('status') == 'running')
    
    # Get database statistics
    db_path = cfg.get_env("TALLY_DB_PATH")
    if not db_path:
        return jsonify({'error': 'Database path not configured'}), 500
    
    conn = db.connect(db_path)
    
    # DC/Invoice statistics
    dc_total = conn.execute('SELECT COUNT(*) FROM delivery_notes').fetchone()[0]
    dc_synced = conn.execute(
        'SELECT COUNT(*) FROM delivery_notes dn LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id WHERE COALESCE(ss.is_synced, 0) = 1'
    ).fetchone()[0]
    dc_unsynced = conn.execute(
        'SELECT COUNT(*) FROM delivery_notes dn LEFT JOIN sync_status ss ON ss.delivery_note_id = dn.id WHERE COALESCE(ss.is_synced, 0) = 0'
    ).fetchone()[0]
    dc_last_sync = conn.execute(
        'SELECT MAX(ss.synced_at) FROM sync_status ss WHERE ss.is_synced = 1'
    ).fetchone()[0]
    
    # Customer statistics
    cust_total = conn.execute('SELECT COUNT(*) FROM ledgers').fetchone()[0]
    cust_synced = conn.execute(
        'SELECT COUNT(*) FROM ledgers l LEFT JOIN ledger_sync_status ss ON ss.ledger_id = l.id WHERE COALESCE(ss.is_synced, 0) = 1'
    ).fetchone()[0]
    cust_unsynced = conn.execute(
        'SELECT COUNT(*) FROM ledgers l LEFT JOIN ledger_sync_status ss ON ss.ledger_id = l.id WHERE COALESCE(ss.is_synced, 0) = 0'
    ).fetchone()[0]
    cust_last_sync = conn.execute(
        'SELECT MAX(ss.synced_at) FROM ledger_sync_status ss WHERE ss.is_synced = 1'
    ).fetchone()[0]
    
    # Product statistics
    prod_total = conn.execute('SELECT COUNT(*) FROM stock_items').fetchone()[0]
    prod_synced = conn.execute(
        'SELECT COUNT(*) FROM stock_items s LEFT JOIN stock_sync_status ss ON ss.stock_item_id = s.id WHERE COALESCE(ss.is_synced, 0) = 1'
    ).fetchone()[0]
    prod_unsynced = conn.execute(
        'SELECT COUNT(*) FROM stock_items s LEFT JOIN stock_sync_status ss ON ss.stock_item_id = s.id WHERE COALESCE(ss.is_synced, 0) = 0'
    ).fetchone()[0]
    prod_last_sync = conn.execute(
        'SELECT MAX(ss.synced_at) FROM stock_sync_status ss WHERE ss.is_synced = 1'
    ).fetchone()[0]
    
    conn.close()
    
    company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
    
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
                'by_company': {company_name: cust_total}  # Single company
            },
            'products': {
                'total': prod_total,
                'synced': prod_synced,
                'unsynced': prod_unsynced,
                'last_sync': prod_last_sync,
                'by_company': {company_name: prod_total}  # Single company
            },
            'invoices': {
                'total': dc_total,
                'synced': dc_synced,
                'unsynced': dc_unsynced,
                'last_sync': dc_last_sync,
                'by_company': {company_name: dc_total}  # Single company
            },
            'duplicates': {
                'total': 0,
                'breakdown': {}
            }
        },
        'config': {
            'entity_name': company_name,
            'entity_id': cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            'active_companies': [company_name],  # Single company
            'tally_company_map': {'COMPANY_1': company_name},  # Single company
            'sync_batch_size': cfg.get_env_int("SYNC_BATCH_SIZE", 10),
            'invoice_fetch_start_date': cfg.get_env("TALLY_FROM_DATE", "Today"),
            'product_type_map': {}
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
            subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
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
        from fetch_customers import build_config as build_cust_config, run_once as fetch_cust
        from fetch_products import build_config as build_prod_config, run_once as fetch_prod
        from types import SimpleNamespace
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Master',
                'status': 'started'
            })
        
        dashboard_logger.write_log("=== MANUAL: FETCH MASTER DATA STARTED ===")
        
        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=cfg.get_env("TALLY_DB_PATH"),
            tally_url=cfg.get_env("TALLY_URL"),
            company=cfg.get_env("TALLY_COMPANY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            fetch_full=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=cfg.get_env_bool("LOG_JSON", False),
            log_file=None
        )
        
        # Fetch customers
        cust_config = build_cust_config(args)
        cust_stats = fetch_cust(cust_config)
        
        # Fetch products
        prod_config = build_prod_config(args)
        prod_stats = fetch_prod(prod_config)
        
        dashboard_logger.write_log(f"=== MANUAL: FETCH MASTER DATA COMPLETED ===")
        
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Master',
                'status': 'success',
                'detail': f"Customers: {cust_stats.get('created', 0)}/{cust_stats.get('updated', 0)}, Products: {prod_stats.get('created', 0)}/{prod_stats.get('updated', 0)}"
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
        from fetch_customers import build_config, run_once
        from types import SimpleNamespace

        dashboard_logger.write_log("=== MANUAL: FETCH CUSTOMERS STARTED ===")

        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=cfg.get_env("TALLY_DB_PATH"),
            tally_url=cfg.get_env("TALLY_URL"),
            company=cfg.get_env("TALLY_COMPANY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            fetch_full=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=cfg.get_env_bool("LOG_JSON", False),
            log_file=None
        )

        fetch_config = build_config(args)
        stats = run_once(fetch_config)

        dashboard_logger.write_log(f"=== MANUAL: FETCH CUSTOMERS COMPLETED - {stats} ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Customers',
                'status': 'success',
                'detail': f"Created: {stats.get('created', 0)}, Updated: {stats.get('updated', 0)}"
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
        from fetch_products import build_config, run_once
        from types import SimpleNamespace

        dashboard_logger.write_log("=== MANUAL: FETCH PRODUCTS STARTED ===")

        args = SimpleNamespace(
            config=cfg.resolve_env_path(ROOT_DIR),
            db_path=cfg.get_env("TALLY_DB_PATH"),
            tally_url=cfg.get_env("TALLY_URL"),
            company=cfg.get_env("TALLY_COMPANY"),
            entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
            fetch_full=False,
            log_level=cfg.get_env("LOG_LEVEL", "INFO"),
            log_json=cfg.get_env_bool("LOG_JSON", False),
            log_file=None
        )

        fetch_config = build_config(args)
        stats = run_once(fetch_config)

        dashboard_logger.write_log(f"=== MANUAL: FETCH PRODUCTS COMPLETED - {stats} ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Products',
                'status': 'success',
                'detail': f"Created: {stats.get('created', 0)}, Updated: {stats.get('updated', 0)}"
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
        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)
        company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
        
        rows = conn.execute('''
            SELECT l.id, l.name, l.updated_at, l.data_json,
                   ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at, ss.last_error
            FROM ledgers l
            LEFT JOIN ledger_sync_status ss ON ss.ledger_id = l.id
            ORDER BY l.updated_at DESC
        ''').fetchall()
        
        customers = []
        for row in rows:
            # Parse data_json to extract customer details
            data_json = json.loads(row[3]) if row[3] else {}
            
            customers.append({
                'id': row[0],
                'name': row[1],
                'tally_company': company_name,
                'gstin': data_json.get('GSTIN', '-') or data_json.get('gstin', '-') or '-',
                'phone': data_json.get('phone', '-') or data_json.get('Phone', '-') or '-',
                'email': data_json.get('email', '-') or data_json.get('Email', '-') or '-',
                'city': data_json.get('city', '-') or data_json.get('City', '-') or '-',
                'state': data_json.get('state', '-') or data_json.get('State', '-') or '-',
                'is_synced': bool(row[4]) if row[4] is not None else False,
                'catalytics_id': None,
                'first_fetched_at': row[2],
                'last_sync_at': row[7],
                'sync_attempts': row[5] or 0,
                'last_sync_error': row[8]
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
        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)
        company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
        
        rows = conn.execute('''
            SELECT s.id, s.name, s.updated_at, s.data_json,
                   ss.is_synced, ss.attempts, ss.last_attempt_at, ss.synced_at, ss.last_error
            FROM stock_items s
            LEFT JOIN stock_sync_status ss ON ss.stock_item_id = s.id
            ORDER BY s.updated_at DESC
        ''').fetchall()
        
        products = []
        for row in rows:
            # Parse data_json to extract product details
            data_json = json.loads(row[3]) if row[3] else {}
            
            products.append({
                'id': row[0],
                'name': row[1],
                'tally_company': company_name,
                'hsn_code': data_json.get('HSN', '-') or data_json.get('hsn', '-') or '-',
                'unit': data_json.get('BaseUnits', '-') or data_json.get('unit', '-') or '-',
                'rate': float(data_json.get('Rate', 0) or data_json.get('rate', 0) or 0),
                'description': data_json.get('Description', '-') or data_json.get('description', '-') or '-',
                'is_synced': bool(row[4]) if row[4] is not None else False,
                'catalytics_id': None,
                'first_fetched_at': row[2],
                'last_sync_at': row[7],
                'sync_attempts': row[5] or 0,
                'last_sync_error': row[8]
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

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE ledger_sync_status SET is_synced = 0, attempts = 0 WHERE ledger_id IN ({placeholders})',
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

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE stock_sync_status SET is_synced = 0, attempts = 0 WHERE stock_item_id IN ({placeholders})',
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


@app.route('/api/bulk/delete-customers', methods=['POST'])
def bulk_delete_customers():
    """Delete selected customers from local SQLite."""
    try:
        data = request.get_json() or {}
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'success': False, 'error': 'No IDs provided'}), 400

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM ledger_sync_status WHERE ledger_id IN ({placeholders})', tuple(ids))
        conn.execute(f'DELETE FROM ledgers WHERE id IN ({placeholders})', tuple(ids))
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

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f'DELETE FROM stock_sync_status WHERE stock_item_id IN ({placeholders})', tuple(ids))
        conn.execute(f'DELETE FROM stock_items WHERE id IN ({placeholders})', tuple(ids))
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

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE ledger_sync_status SET attempts = 0, last_error = NULL WHERE ledger_id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Reset {len(ids)} customers for retry ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Retry Customers',
                'status': 'success',
                'detail': f"{len(ids)} customers reset for retry"
            })

        return jsonify({'success': True, 'message': f'{len(ids)} customers reset for retry'})
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

        db_path = cfg.get_env("TALLY_DB_PATH")
        conn = db.connect(db_path)

        placeholders = ','.join('?' * len(ids))
        conn.execute(
            f'UPDATE stock_sync_status SET attempts = 0, last_error = NULL WHERE stock_item_id IN ({placeholders})',
            tuple(ids)
        )
        conn.commit()
        conn.close()

        dashboard_logger.write_log(f"=== BULK: Reset {len(ids)} products for retry ===")

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Retry Products',
                'status': 'success',
                'detail': f"{len(ids)} products reset for retry"
            })

        return jsonify({'success': True, 'message': f'{len(ids)} products reset for retry'})
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



def maybe_start_automation():
    """Auto-start automation if enabled in .env"""
    auto_start = cfg.get_env_bool("AUTO_START_AUTOMATION", True)
    if not auto_start:
        logger.info("Auto-start disabled in .env")
        return
    
    try:
        manager = get_manager()
        started = manager.start()
        if started:
            logger.info("✓ Automation auto-started on dashboard launch")
            print("✓ Automation auto-started (fetch + sync running)")
        else:
            logger.info("Automation already running")
            print("✓ Automation already running")
    except Exception as e:
        logger.error(f"Failed to auto-start automation: {e}")
        print(f"❌ Failed to auto-start automation: {e}")


def main():
    """Main entry point"""
    # Load environment
    env_path = cfg.resolve_env_path(ROOT_DIR)
    print(f"=" * 70)
    print(f"LOADING CONFIGURATION")
    print(f"=" * 70)
    print(f"Script directory: {ROOT_DIR}")
    print(f"Current directory: {os.getcwd()}")
    print(f"Loading .env from: {env_path}")
    
    if os.path.exists(env_path):
        print(f"✓ .env file found")
        cfg.load_env_file(env_path)
    else:
        print(f"❌ WARNING: .env file not found at {env_path}")
    
    # Show loaded config
    api_base_url = cfg.get_env("CATALYTICS_API_BASE_URL", "")
    entity_id = cfg.get_env_int("CATALYTICS_ENTITY_ID", 0)
    api_key = cfg.get_env("CATALYTICS_API_KEY", "")
    
    print(f"\nLoaded configuration:")
    print(f"  CATALYTICS_API_BASE_URL = {api_base_url}")
    print(f"  CATALYTICS_ENTITY_ID = {entity_id}")
    print(f"  CATALYTICS_API_KEY = {'SET (' + str(len(api_key)) + ' chars)' if api_key else 'NOT SET'}")
    print(f"=" * 70)
    print()
    
    # Setup logging
    log_level = cfg.get_env("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Get configuration
    host = cfg.get_env("WEB_UI_HOST", "localhost")
    port = cfg.get_env_int("WEB_UI_PORT", 8787)
    db_path = cfg.get_env("TALLY_DB_PATH")
    
    # Initialize database if needed
    if db_path:
        logger.info(f"Initializing database: {db_path}")
        try:
            conn = db.connect(db_path)
            db.init_db(conn)
            conn.close()
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
    
    company_name = cfg.get_env("TALLY_COMPANY", "Unknown")
    logger.info(f"Starting CO Middleware Dashboard for company: {company_name}")
    logger.info(f"Dashboard will be available at: http://{host}:{port}")
    
    # Auto-start automation
    maybe_start_automation()
    
    # Open browser
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f'http://{host}:{port}')
    
    threading.Thread(target=open_browser, daemon=True).start()
    
    # Run Flask app
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    main()
