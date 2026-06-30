"""
BHOX Middleware - Web Dashboard
Provides real-time monitoring and control interface for the middleware
"""

import os
import sys
import io
import sqlite3
from pathlib import Path
import logging
import requests
import webbrowser
from datetime import datetime
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from config import config, BASE_DIR
from db import Database
from collections import deque
import threading
from automation_manager import get_manager, DashboardLogHandler
from data_matcher import DataMatcher
from log_capture import dashboard_logger
import auto_updater

# Determine template folder Ã¢â‚¬â€ inside _MEIPASS when frozen, else default
if getattr(sys, 'frozen', False):
    _template_folder = os.path.join(sys._MEIPASS, 'templates')
else:
    _template_folder = os.path.join(os.path.dirname(__file__), 'templates')

app = Flask(__name__, template_folder=_template_folder)
CORS(app)
logger = logging.getLogger(__name__)

# Initialize database connection
db = Database(config.SQLITE_DB_PATH)

# Activity and error tracking
activity_log = deque(maxlen=50)  # Keep last 50 activities
error_log = deque(maxlen=10)  # Keep last 10 errors
activity_lock = threading.Lock()

# Progress tracking
current_progress = {
    'active': False,
    'step': None,
    'current': 0,
    'total': 0,
    'percentage': 0
}
progress_lock = threading.Lock()

# Single-operation guard to avoid concurrent writes to SQLite
operation_lock = threading.Lock()
operation_state = {'active': False, 'name': None, 'started_at': None}

def _start_operation(name):
    with operation_lock:
        if operation_state['active']:
            return False, operation_state.copy()
        operation_state['active'] = True
        operation_state['name'] = name
        operation_state['started_at'] = datetime.now().isoformat()
        return True, operation_state.copy()

def _finish_operation():
    with operation_lock:
        operation_state['active'] = False
        operation_state['name'] = None
        operation_state['started_at'] = None

@app.route('/api/operation')
def api_operation_status():
    with operation_lock:
        return jsonify(operation_state)


def check_tally_connection():
    """Check if Tally server is accessible.
    Uses the global Tally lock to avoid concurrent requests which cause
    Tally to crash with Memory Access Violation."""
    from tally_client import _tally_lock
    if not _tally_lock.acquire(timeout=3):
        # Another request in progress — Tally is reachable, just busy
        return True
    try:
        response = requests.get(
            f"{config.TALLY_URL}",
            timeout=config.TALLY_CHECK_TIMEOUT,
            headers={"Connection": "close"},
        )
        return response.status_code == 200
    except:
        return False
    finally:
        _tally_lock.release()

def check_catalytics_connection():
    """Check if Catalytics backend is accessible"""
    try:
        response = requests.get(f"{config.CATALYTICS_API_BASE}", timeout=config.CATALYTICS_CHECK_TIMEOUT)
        return response.status_code in [200, 404]  # 404 is OK, means server is up
    except:
        return False

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
        log_path = Path(BASE_DIR) / 'logs' / log_file
        if not log_path.exists():
            return []
        data = log_path.read_text(encoding='utf-8', errors='ignore').splitlines()
        keyword_lower = (keyword or '').lower()
        matched = [ln for ln in data if keyword_lower in ln.lower()] if keyword_lower else []
        source = matched if matched else data
        return source[-lines:]
    except Exception:
        return []

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html',
                         entity_name=config.ENTITY_NAME,
                         entity_id=config.ENTITY_ID)

@app.route('/api/status')
def api_status():
    """Get current system status"""
    # Check connections
    tally_online = check_tally_connection()
    catalytics_online = check_catalytics_connection()

    # Get database statistics
    stats = db.get_statistics()

    # Get sync status
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM customers WHERE is_synced = 0')
    unsynced_customers = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM products WHERE is_synced = 0')
    unsynced_products = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM invoices WHERE is_synced = 0')
    unsynced_invoices = cursor.fetchone()[0]

    # Get recent sync timestamps
    cursor.execute('SELECT MAX(last_sync_at) FROM customers WHERE is_synced = 1')
    last_customer_sync = cursor.fetchone()[0]

    cursor.execute('SELECT MAX(last_sync_at) FROM products WHERE is_synced = 1')
    last_product_sync = cursor.fetchone()[0]

    cursor.execute('SELECT MAX(last_sync_at) FROM invoices WHERE is_synced = 1')
    last_invoice_sync = cursor.fetchone()[0]

    # Get duplicate counts
    cursor.execute('SELECT COUNT(*) FROM duplicate_log')
    total_duplicates = cursor.fetchone()[0]

    cursor.execute('''
        SELECT entity_type, COUNT(*) as count
        FROM duplicate_log
        GROUP BY entity_type
    ''')
    duplicate_breakdown = {row[0]: row[1] for row in cursor.fetchall()}

    conn.close()

    active_companies = config.get_active_companies()

    return jsonify({
        'timestamp': datetime.now().isoformat(),
        'connections': {
            'tally': tally_online,
            'catalytics': catalytics_online
        },
        'statistics': {
            'customers': {
                'total': stats['total_customers'],
                'synced': stats['synced_customers'],
                'unsynced': unsynced_customers,
                'last_sync': last_customer_sync,
                'by_company': stats.get('customers_by_company', {})
            },
            'products': {
                'total': stats['total_products'],
                'synced': stats['synced_products'],
                'unsynced': unsynced_products,
                'last_sync': last_product_sync,
                'by_company': stats.get('products_by_company', {})
            },
            'invoices': {
                'total': stats['total_invoices'],
                'synced': stats['synced_invoices'],
                'unsynced': unsynced_invoices,
                'last_sync': last_invoice_sync,
                'by_company': stats.get('invoices_by_company', {})
            },
            'duplicates': {
                'total': total_duplicates,
                'breakdown': duplicate_breakdown
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
            'invoice_fetch_start_date': config.INVOICE_FETCH_START_DATE or 'Today',
            'product_type_map': config.PRODUCT_TYPE_MAP
        }
    })

@app.route('/api/logs')
def api_logs():
    """Get recent log entries"""
    log_type = request.args.get('type', 'main')
    lines = int(request.args.get('lines', 50))

    log_files = {
        'main': 'bhox.log',
        'fetch_master': 'fetch_master_data.log',
        'fetch_invoices': 'fetch_invoices.log',
        'sync': 'sync_to_catalytics.log'
    }

    log_file = log_files.get(log_type, 'bhox.log')
    logs = get_recent_file_logs(log_file, lines)

    return jsonify({
        'log_type': log_type,
        'log_file': log_file,
        'lines': logs
    })

@app.route('/api/duplicates')
def api_duplicates():
    """Get duplicate log entries"""
    limit = int(request.args.get('limit', 20))

    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT entity_type, entity_name, tally_company, owned_by_company, details, logged_at
        FROM duplicate_log
        ORDER BY logged_at DESC
        LIMIT ?
    ''', (limit,))

    duplicates = []
    for row in cursor.fetchall():
        duplicates.append({
            'entity_type': row[0],
            'entity_name': row[1],
            'tally_company': row[2],
            'owned_by_company': row[3],
            'details': row[4],
            'logged_at': row[5]
        })

    conn.close()

    return jsonify({
        'duplicates': duplicates,
        'total': len(duplicates)
    })

@app.route('/api/data/customers')
def api_data_customers():
    """Get all customers with sync status"""
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, name, tally_company, gstin, phone, email, city, state,
               is_synced, catalytics_id, first_fetched_at, last_sync_at,
               sync_attempts, last_sync_error
        FROM customers
        ORDER BY first_fetched_at DESC
    ''')

    customers = []
    for row in cursor.fetchall():
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
            'sync_attempts': row[12],
            'last_sync_error': row[13]
        })

    conn.close()
    return jsonify({'customers': customers, 'total': len(customers)})

@app.route('/api/data/products')
def api_data_products():
    """Get all products with sync status"""
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, name, tally_company, hsn_code, unit, rate, description,
               is_synced, catalytics_id, first_fetched_at, last_sync_at,
               sync_attempts, last_sync_error
        FROM products
        ORDER BY first_fetched_at DESC
    ''')

    products = []
    for row in cursor.fetchall():
        products.append({
            'id': row[0],
            'name': row[1],
            'tally_company': row[2],
            'hsn_code': row[3] or '-',
            'unit': row[4] or '-',
            'rate': row[5] or 0,
            'description': row[6] or '-',
            'is_synced': bool(row[7]),
            'catalytics_id': row[8],
            'first_fetched_at': row[9],
            'last_sync_at': row[10],
            'sync_attempts': row[11],
            'last_sync_error': row[12]
        })

    conn.close()
    return jsonify({'products': products, 'total': len(products)})

@app.route('/api/data/invoices')
def api_data_invoices():
    """Get all invoices/DCs with sync status"""
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, tally_voucher_no, tally_company, voucher_date,
               customer_name, total_amount, dc_no,
               is_synced, catalytics_dc_id, first_fetched_at, last_sync_at,
               sync_attempts, last_sync_error, items_json
        FROM invoices
        ORDER BY first_fetched_at DESC
    ''')

    invoices = []
    dc_ids = []
    for row in cursor.fetchall():
        inv = {
            'id': row[0],
            'voucher_no': row[1],
            'tally_company': row[2],
            'voucher_date': row[3],
            'customer_name': row[4],
            'total_amount': row[5] or 0,
            'dc_no': row[6] or '-',
            'is_synced': bool(row[7]),
            'catalytics_dc_id': row[8],
            'first_fetched_at': row[9],
            'last_sync_at': row[10],
            'sync_attempts': row[11],
            'last_sync_error': row[12],
            'items_json': row[13] or '[]',
            'order_status': None
        }
        invoices.append(inv)
        if row[8]:  # catalytics_dc_id exists
            dc_ids.append(row[8])
    conn.close()

    # Fetch order_status from Catalytics PostgreSQL for synced invoices
    if dc_ids:
        try:
            import psycopg2
            pg_conn = psycopg2.connect(
                host=config.POSTGRES_HOST,
                port=config.POSTGRES_PORT,
                database=config.POSTGRES_DB,
                user=config.POSTGRES_USER,
                password=config.POSTGRES_PASSWORD
            )
            pg_cursor = pg_conn.cursor()
            pg_cursor.execute(
                'SELECT id, order_status FROM "transaction.delivery_challan" WHERE id = ANY(%s)',
                (dc_ids,)
            )
            status_map = {row[0]: row[1] for row in pg_cursor.fetchall()}
            pg_conn.close()
            for inv in invoices:
                if inv['catalytics_dc_id'] and inv['catalytics_dc_id'] in status_map:
                    inv['order_status'] = status_map[inv['catalytics_dc_id']]
        except Exception:
            pass  # If PG is down, just return without order_status

    return jsonify({'invoices': invoices, 'total': len(invoices)})

def _guarded_run(op_name, func, *args, **kwargs):
    started, state = _start_operation(op_name)
    if not started:
        msg = f"Another operation is running: {state['name']}"
        return False, msg
    try:
        return _run_with_dashboard_capture(func, *args, **kwargs)
    finally:
        _finish_operation()

def _run_with_dashboard_capture(func, *args, **kwargs):
    """Run a function while routing its log output to the dashboard terminal.
    Returns (success: bool, output_text: str)."""
    buf = io.StringIO()
    handler = DashboardLogHandler(string_buffer=buf)
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        func(*args, **kwargs)
        return True, buf.getvalue()
    except Exception as exc:
        return False, f"{buf.getvalue()}\nERROR: {exc}"
    finally:
        root.removeHandler(handler)


@app.route('/api/trigger/fetch_customers', methods=['POST'])
def trigger_fetch_customers():
    """Manually trigger customer fetch"""
    try:
        from fetch_customers import fetch_customers_from_all_companies
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Customers',
                'status': 'started',
                'detail': f"Companies: {', '.join(config.get_active_companies())}"
            })
        dashboard_logger.write_log("=== FETCH CUSTOMERS STARTED ===")
        dashboard_logger.write_log(f"Companies: {', '.join(config.get_active_companies())}")

        success, output = _guarded_run("Fetch Customers", fetch_customers_from_all_companies)

        dashboard_logger.write_log(f"=== FETCH CUSTOMERS {'COMPLETED' if success else 'FAILED'} ===")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Customers',
                'status': 'success' if success else 'error',
                'detail': output[:200]
            })
        return jsonify({
            'success': success,
            'output': output,
            'error': '' if success else output
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({'time': datetime.now().isoformat(), 'error': f'Fetch customers failed: {str(e)}'})
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/trigger/fetch_products', methods=['POST'])
def trigger_fetch_products():
    """Manually trigger product fetch"""
    try:
        from fetch_products import fetch_products_from_all_companies
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Products',
                'status': 'started',
                'detail': f"Companies: {', '.join(config.get_active_companies())}"
            })
        dashboard_logger.write_log("=== FETCH PRODUCTS STARTED ===")
        dashboard_logger.write_log(f"Companies: {', '.join(config.get_active_companies())}")

        success, output = _guarded_run("Fetch Products", fetch_products_from_all_companies)

        dashboard_logger.write_log(f"=== FETCH PRODUCTS {'COMPLETED' if success else 'FAILED'} ===")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Products',
                'status': 'success' if success else 'error',
                'detail': output[:200]
            })
        return jsonify({
            'success': success,
            'output': output,
            'error': '' if success else output
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({'time': datetime.now().isoformat(), 'error': f'Fetch products failed: {str(e)}'})
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/trigger/fetch_invoices', methods=['POST'])
def trigger_fetch_invoices():
    """Manually trigger invoice fetch — runs in background, returns immediately."""
    import threading
    try:
        from fetch_invoices import fetch_invoices_from_all_companies
        companies = config.get_active_companies()

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Fetch Invoices',
                'status': 'started',
                'detail': f"Fetching from {len(companies)} companies: {', '.join(companies)}"
            })
        dashboard_logger.write_log("=== FETCH INVOICES STARTED (background) ===")
        dashboard_logger.write_log(f"Companies: {', '.join(companies)}")

        def _bg_fetch():
            try:
                success, output = _guarded_run("Fetch Invoices", fetch_invoices_from_all_companies)
                dashboard_logger.write_log(
                    f"=== FETCH INVOICES {'COMPLETED' if success else 'FAILED'} ==="
                )
                with activity_lock:
                    activity_log.appendleft({
                        'time': datetime.now().isoformat(),
                        'action': 'Fetch Invoices',
                        'status': 'success' if success else 'error',
                        'detail': output[:200]
                    })
                if not success:
                    with activity_lock:
                        error_log.appendleft({
                            'time': datetime.now().isoformat(),
                            'error': f'Invoice fetch failed: {output[:200]}'
                        })
            except Exception as exc:
                with activity_lock:
                    error_log.appendleft({
                        'time': datetime.now().isoformat(),
                        'error': f'Invoice fetch bg error: {str(exc)}'
                    })

        t = threading.Thread(target=_bg_fetch, daemon=True)
        t.start()

        return jsonify({
            'success': True,
            'started': True,
            'message': f'Invoice fetch started for {len(companies)} companies. Check terminal/logs panel for progress.'
        })

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Fetch invoices exception: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/trigger/sync', methods=['POST'])
def trigger_sync():
    """Manually trigger sync to Catalytics"""
    try:
        from sync_to_catalytics import CatalyticsSyncer

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Sync',
                'status': 'started'
            })

        dashboard_logger.write_log("=== SYNC TO CATALYTICS STARTED ===")

        syncer = CatalyticsSyncer()
        success, output = _guarded_run("Sync All", syncer.sync_all)

        dashboard_logger.write_log(f"=== SYNC TO CATALYTICS {'COMPLETED' if success else 'FAILED'} ===")

        # Log result
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Sync',
                'status': 'success' if success else 'error',
                'detail': output[:200]
            })

        if not success:
            with activity_lock:
                error_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'error': f"Sync failed: {output[:200]}"
                })

        return jsonify({
            'success': success,
            'output': output,
            'error': '' if success else output
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f"Sync exception: {str(e)}"
            })
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/trigger/sync_products', methods=['POST'])
def trigger_sync_products():
    """Manually trigger product-only sync to Catalytics"""
    try:
        from sync_to_catalytics import CatalyticsSyncer

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Product Sync',
                'status': 'started'
            })

        dashboard_logger.write_log("=== PRODUCT SYNC TO CATALYTICS STARTED ===")

        syncer = CatalyticsSyncer()
        success, output = _guarded_run("Sync Products", syncer.sync_products)

        dashboard_logger.write_log(
            f"=== PRODUCT SYNC TO CATALYTICS {'COMPLETED' if success else 'FAILED'} ==="
        )

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Product Sync',
                'status': 'success' if success else 'error',
                'detail': output[:200]
            })

        if not success:
            with activity_lock:
                error_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'error': f"Product sync failed: {output[:200]}"
                })

        return jsonify({
            'success': success,
            'output': output,
            'error': '' if success else output
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f"Product sync exception: {str(e)}"
            })
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/trigger/sync_customers', methods=['POST'])
def trigger_sync_customers():
    """Manually trigger customer-only sync to Catalytics"""
    try:
        from sync_to_catalytics import CatalyticsSyncer

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Customer Sync',
                'status': 'started'
            })

        dashboard_logger.write_log("=== CUSTOMER SYNC TO CATALYTICS STARTED ===")

        syncer = CatalyticsSyncer()
        success, output = _guarded_run("Sync Customers", syncer.sync_customers)

        dashboard_logger.write_log(
            f"=== CUSTOMER SYNC TO CATALYTICS {'COMPLETED' if success else 'FAILED'} ==="
        )

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Customer Sync',
                'status': 'success' if success else 'error',
                'detail': output[:200]
            })

        if not success:
            with activity_lock:
                error_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'error': f"Customer sync failed: {output[:200]}"
                })

        return jsonify({
            'success': success,
            'output': output,
            'error': '' if success else output
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f"Customer sync exception: {str(e)}"
            })
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500



@app.route('/api/trigger/sync_invoices', methods=['POST'])
def trigger_sync_invoices():
    """Manually trigger invoice-only sync to Catalytics (as DCs) using lightweight API"""
    try:
        from sync_to_catalytics import CatalyticsSyncer

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Invoice Sync',
                'status': 'started'
            })

        dashboard_logger.write_log("=== INVOICE SYNC TO CATALYTICS STARTED (LIGHTWEIGHT API) ===")

        syncer = CatalyticsSyncer()
        success, output = _guarded_run("Sync Invoices", syncer.sync_invoices_to_dc)

        dashboard_logger.write_log(
            f"=== INVOICE SYNC TO CATALYTICS {'COMPLETED' if success else 'FAILED'} ==="
        )

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Invoice Sync',
                'status': 'success' if success else 'error',
                'detail': output[:200]
            })

        if not success:
            with activity_lock:
                error_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'error': f"Invoice sync failed: {output[:200]}"
                })

        return jsonify({
            'success': success,
            'output': output,
            'error': '' if success else output
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f"Invoice sync exception: {str(e)}"
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/activity')

def api_activity():
    """Get recent activity log"""
    with activity_lock:
        return jsonify({
            'activities': list(activity_log)[:20]
        })

@app.route('/api/errors')
def api_errors():
    """Get recent errors"""
    with activity_lock:
        return jsonify({
            'errors': list(error_log)[:5]
        })

@app.route('/api/progress')
def api_progress():
    """Get current progress status"""
    with progress_lock:
        return jsonify(current_progress)

@app.route('/api/customers/<int:customer_id>/detail')
def api_customer_detail(customer_id):
    """Get detailed customer information"""
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
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

    return jsonify({
        'customer': {
            'id': row[0],
            'name': row[1],
            'tally_company': row[2],
            'tally_guid': row[3],
            'gstin': row[4],
            'pan': row[5],
            'address': row[6],
            'state': row[7],
            'city': row[8],
            'pincode': row[9],
            'phone': row[10],
            'email': row[11],
            'is_synced': bool(row[12]),
            'catalytics_id': row[13],
            'sync_attempts': row[14],
            'last_sync_error': row[15],
            'first_fetched_at': row[16],
            'last_updated_at': row[17],
            'last_sync_at': row[18],
            'data_json': row[19],
            'sync_request_json': row[20],
            'last_response_json': row[21],
            'fetch_log': get_log_excerpt_by_keyword('customer_fetch.log', row[1]),
            'sync_log': get_log_excerpt_by_keyword('customer_sync.log', row[1])
        }
    })

@app.route('/api/products/<int:product_id>/detail')
def api_product_detail(product_id):
    """Get detailed product information"""
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, name, tally_company, tally_guid, hsn_code, unit, rate, description,
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

    return jsonify({
        'product': {
            'id': row[0],
            'name': row[1],
            'tally_company': row[2],
            'tally_guid': row[3],
            'hsn_code': row[4],
            'unit': row[5],
            'rate': row[6],
            'description': row[7],
            'is_synced': bool(row[8]),
            'catalytics_id': row[9],
            'sync_attempts': row[10],
            'last_sync_error': row[11],
            'first_fetched_at': row[12],
            'last_updated_at': row[13],
            'last_sync_at': row[14],
            'data_json': row[15],
            'last_response_json': row[16],
            'fetch_log': get_log_excerpt_by_keyword('product_fetch.log', row[1]),
            'sync_log': get_log_excerpt_by_keyword('product_sync.log', row[1])
        }
    })

@app.route('/api/invoices/<int:invoice_id>/detail')
def api_invoice_detail(invoice_id):
    """Get detailed invoice information"""
    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, tally_voucher_no, tally_company, tally_guid, voucher_date,
               customer_name, customer_guid, billing_address, delivery_address,
               total_amount, tax_amount, items_json, dc_no,
               is_synced, catalytics_dc_id, sync_attempts, last_sync_error,
               first_fetched_at, last_updated_at, last_sync_at,
               data_json, sync_request_json, last_response_json
        FROM invoices
        WHERE id = ?
    ''', (invoice_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Invoice not found'}), 404

    return jsonify({
        'invoice': {
            'id': row[0],
            'voucher_no': row[1],
            'tally_company': row[2],
            'tally_guid': row[3],
            'voucher_date': row[4],
            'customer_name': row[5],
            'customer_guid': row[6],
            'billing_address': row[7],
            'delivery_address': row[8],
            'total_amount': row[9],
            'tax_amount': row[10],
            'items_json': row[11],
            'dc_no': row[12],
            'is_synced': bool(row[13]),
            'catalytics_dc_id': row[14],
            'sync_attempts': row[15],
            'last_sync_error': row[16],
            'first_fetched_at': row[17],
            'last_updated_at': row[18],
            'last_sync_at': row[19],
            'data_json': row[20],
            'last_response_json': row[21]
        }
    })

@app.route('/api/bulk/retry-customers', methods=['POST'])
def bulk_retry_customers():
    """Bulk retry failed customer syncs"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        customer_ids = data.get('ids', [])

        if not customer_ids:
            # Retry all failed customers
            conn = sqlite3.connect(config.SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM customers WHERE is_synced = 0')
            customer_ids = [row[0] for row in cursor.fetchall()]
            conn.close()

        if not customer_ids:
            return jsonify({'success': True, 'message': 'No customers to retry', 'count': 0})

        # Mark customers for resync
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in customer_ids])
        cursor.execute(f'''
            UPDATE customers
            SET last_sync_error = NULL, sync_attempts = 0
            WHERE id IN ({placeholders})
        ''', customer_ids)
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Bulk Retry Customers',
                'status': 'success',
                'detail': f'Reset {updated} customers for retry'
            })

        return jsonify({
            'success': True,
            'message': f'Marked {updated} customers for retry',
            'count': updated
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Bulk retry customers failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk/retry-products', methods=['POST'])
def bulk_retry_products():
    """Bulk retry failed product syncs"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        product_ids = data.get('ids', [])

        if not product_ids:
            # Retry all failed products
            conn = sqlite3.connect(config.SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM products WHERE is_synced = 0')
            product_ids = [row[0] for row in cursor.fetchall()]
            conn.close()

        if not product_ids:
            return jsonify({'success': True, 'message': 'No products to retry', 'count': 0})

        # Mark products for resync
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in product_ids])
        cursor.execute(f'''
            UPDATE products
            SET last_sync_error = NULL, sync_attempts = 0
            WHERE id IN ({placeholders})
        ''', product_ids)
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Bulk Retry Products',
                'status': 'success',
                'detail': f'Reset {updated} products for retry'
            })

        return jsonify({
            'success': True,
            'message': f'Marked {updated} products for retry',
            'count': updated
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Bulk retry products failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk/retry-invoices', methods=['POST'])
def bulk_retry_invoices():
    """Bulk retry failed invoice syncs and optionally trigger sync immediately"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        invoice_ids = data.get('ids', [])
        sync_now = data.get('sync_now', False)  # Option to sync immediately

        if not invoice_ids:
            # Retry all failed invoices
            conn = sqlite3.connect(config.SQLITE_DB_PATH)
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM invoices WHERE is_synced = 0')
            invoice_ids = [row[0] for row in cursor.fetchall()]
            conn.close()

        if not invoice_ids:
            return jsonify({'success': True, 'message': 'No invoices to retry', 'count': 0})

        # Mark invoices for resync (reset all fields including enhanced ones)
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in invoice_ids])
        cursor.execute(f'''
            UPDATE invoices
            SET is_synced = 0,
                is_deleted = 0,
                deleted_at = NULL,
                catalytics_dc_id = NULL,
                dc_no = NULL,
                last_sync_at = NULL,
                last_sync_error = NULL,
                sync_attempts = 0,
                last_updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
        ''', invoice_ids)
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        message = f'Marked {updated} invoices for retry'
        status = 'success'

        # If sync_now is requested, trigger sync immediately
        synced_count = 0
        if sync_now and updated > 0:
            try:
                from sync_to_catalytics import CatalyticsSyncer
                syncer = CatalyticsSyncer()
                result = syncer.sync_invoices_to_dc()
                synced_count = result.get('synced', 0)

                if synced_count > 0:
                    message = f'Synced {synced_count} of {updated} invoices successfully!'
                    status = 'success'
                else:
                    message = f'Marked {updated} invoices for retry. Sync completed with {result.get("failed", 0)} errors'
                    status = 'warning' if result.get('failed', 0) > 0 else 'success'
            except Exception as sync_error:
                message = f'Marked {updated} invoices for retry. Sync error: {str(sync_error)}'
                status = 'warning'

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Bulk Retry Invoices',
                'status': status,
                'detail': message
            })

        return jsonify({
            'success': True,
            'message': message,
            'count': updated,
            'synced': synced_count,
            'sync_now': sync_now
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Bulk retry invoices failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk/mark-unsynced-customers', methods=['POST'])
def bulk_mark_unsynced_customers():
    """Bulk mark customers as unsynced"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        customer_ids = data.get('ids', [])

        if not customer_ids:
            return jsonify({'success': False, 'error': 'No customer IDs provided'}), 400

        # Mark customers as unsynced
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in customer_ids])
        cursor.execute(f'''
            UPDATE customers
            SET is_synced = 0, catalytics_id = NULL, last_sync_at = NULL,
                last_sync_error = NULL, sync_attempts = 0
            WHERE id IN ({placeholders})
        ''', customer_ids)
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Bulk Mark Unsynced Customers',
                'status': 'success',
                'detail': f'Marked {updated} customers as unsynced'
            })

        return jsonify({
            'success': True,
            'message': f'Marked {updated} customers as unsynced',
            'count': updated
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Bulk mark unsynced customers failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk/mark-unsynced-products', methods=['POST'])
def bulk_mark_unsynced_products():
    """Bulk mark products as unsynced"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        product_ids = data.get('ids', [])

        if not product_ids:
            return jsonify({'success': False, 'error': 'No product IDs provided'}), 400

        # Mark products as unsynced
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in product_ids])
        cursor.execute(f'''
            UPDATE products
            SET is_synced = 0, catalytics_id = NULL, last_sync_at = NULL,
                last_sync_error = NULL, sync_attempts = 0
            WHERE id IN ({placeholders})
        ''', product_ids)
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Bulk Mark Unsynced Products',
                'status': 'success',
                'detail': f'Marked {updated} products as unsynced'
            })

        return jsonify({
            'success': True,
            'message': f'Marked {updated} products as unsynced',
            'count': updated
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Bulk mark unsynced products failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk/mark-unsynced-invoices', methods=['POST'])
def bulk_mark_unsynced_invoices():
    """Bulk mark invoices as unsynced"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        invoice_ids = data.get('ids', [])

        if not invoice_ids:
            return jsonify({'success': False, 'error': 'No invoice IDs provided'}), 400

        # Mark invoices as unsynced
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in invoice_ids])
        cursor.execute(f'''
            UPDATE invoices
            SET is_synced = 0, catalytics_dc_id = NULL, last_sync_at = NULL,
                last_sync_error = NULL, sync_attempts = 0
            WHERE id IN ({placeholders})
        ''', invoice_ids)
        conn.commit()
        updated = cursor.rowcount
        conn.close()

        # Log activity
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Bulk Mark Unsynced Invoices',
                'status': 'success',
                'detail': f'Marked {updated} invoices as unsynced'
            })

        return jsonify({
            'success': True,
            'message': f'Marked {updated} invoices as unsynced',
            'count': updated
        })
    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Bulk mark unsynced invoices failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500

def _check_dc_order_status(catalytics_dc_id):
    """Check DC order_status in Catalytics PostgreSQL.
    Returns (order_status, error_msg). order_status is None if not found or DB error.
    """
    if not catalytics_dc_id:
        return None, None
    try:
        import psycopg2
        pg_conn = psycopg2.connect(
            host=config.POSTGRES_HOST,
            port=config.POSTGRES_PORT,
            database=config.POSTGRES_DB,
            user=config.POSTGRES_USER,
            password=config.POSTGRES_PASSWORD
        )
        pg_cursor = pg_conn.cursor()
        pg_cursor.execute(
            'SELECT order_status FROM "transaction.delivery_challan" WHERE id = %s',
            (catalytics_dc_id,)
        )
        row = pg_cursor.fetchone()
        pg_conn.close()
        if row:
            return row[0], None
        return None, None
    except Exception as e:
        return None, str(e)


@app.route('/api/customers/<int:customer_id>/resync', methods=['POST'])
def resync_customer(customer_id):
    """Mark customer for resync (JS frontend triggers /api/trigger/sync next)"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT name FROM customers WHERE id = ?', (customer_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Customer not found'}), 404

        customer_name = row[0]
        cursor.execute('''
            UPDATE customers SET is_synced = 0, sync_attempts = 0, last_sync_error = NULL
            WHERE id = ?
        ''', (customer_id,))
        conn.commit()
        conn.close()

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Resync Customer',
                'status': 'success',
                'detail': f'Customer "{customer_name}" marked for resync'
            })

        return jsonify({'success': True, 'message': 'Customer marked for resync'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/products/<int:product_id>/resync', methods=['POST'])
def resync_product(product_id):
    """Mark product for resync (JS frontend triggers /api/trigger/sync next)"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT name FROM products WHERE id = ?', (product_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        product_name = row[0]
        cursor.execute('''
            UPDATE products SET is_synced = 0, sync_attempts = 0, last_sync_error = NULL
            WHERE id = ?
        ''', (product_id,))
        conn.commit()
        conn.close()

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Resync Product',
                'status': 'success',
                'detail': f'Product "{product_name}" marked for resync'
            })

        return jsonify({'success': True, 'message': 'Product marked for resync'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/invoices/<int:invoice_id>/resync', methods=['POST'])
def resync_invoice(invoice_id):
    """Mark individual invoice for resync and optionally trigger sync immediately"""
    try:
        # Get request parameters (force=True ignores Content-Type header issues)
        data = request.get_json(force=True, silent=True) or {}
        sync_now = data.get('sync_now', True)  # Sync immediately by default

        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()

        # Get invoice details
        cursor.execute('''
            SELECT id, tally_voucher_no, tally_company, customer_name, catalytics_dc_id
            FROM invoices WHERE id = ?
        ''', (invoice_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Invoice not found'}), 404

        voucher_no = row[1]
        company_name = row[2]
        customer_name = row[3]
        catalytics_dc_id = row[4]

        # Check if DC is accepted in Catalytics (order_status 1 or 2 = accepted)
        if catalytics_dc_id:
            order_status, pg_err = _check_dc_order_status(catalytics_dc_id)
            if order_status is not None and order_status in (1, 2):
                conn.close()
                status_name = 'Accepted' if order_status == 1 else 'Processed'
                return jsonify({
                    'success': False,
                    'error': f'Cannot resync Invoice #{voucher_no} Ã¢â‚¬â€ DC #{catalytics_dc_id} is already {status_name} (order_status={order_status}) in Catalytics.'
                }), 400

        # Reset invoice for resync (including enhanced fields)
        cursor.execute('''
            UPDATE invoices
            SET is_synced = 0,
                is_deleted = 0,
                deleted_at = NULL,
                catalytics_dc_id = NULL,
                dc_no = NULL,
                last_sync_at = NULL,
                last_sync_error = NULL,
                sync_attempts = 0,
                last_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (invoice_id,))
        conn.commit()
        conn.close()

        message = f'Invoice #{voucher_no} marked for resync'

        # If sync_now is requested, trigger sync immediately
        if sync_now:
            try:
                from sync_to_catalytics import CatalyticsSyncer
                syncer = CatalyticsSyncer()
                result = syncer.sync_invoices_to_dc()

                if result.get('synced', 0) > 0:
                    message = f'Invoice #{voucher_no} resynced successfully! DC: {result.get("synced")} created/updated'
                    status = 'success'
                else:
                    message = f'Invoice #{voucher_no} marked for resync. Sync returned: {result}'
                    status = 'info'
            except Exception as sync_error:
                message = f'Invoice #{voucher_no} marked for resync. Sync error: {str(sync_error)}'
                status = 'warning'
        else:
            status = 'success'

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Resync Invoice',
                'status': status,
                'detail': message
            })

        return jsonify({'success': True, 'message': message, 'synced': sync_now})

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Resync invoice failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== REFETCH ENDPOINTS ====================

def _sync_customer_to_catalytics(customer_id):
    """Helper: Sync a single customer to Catalytics server"""
    try:
        from sync_to_catalytics import CatalyticsSyncer
        from db import Database

        db = Database(config.SQLITE_DB_PATH)
        customer = db.query_all('SELECT * FROM customers WHERE id = ? LIMIT 1', (customer_id,))
        db.close()

        if not customer:
            return None, "Customer not found"

        cust_data = customer[0]
        syncer = CatalyticsSyncer(config.CATALYTICS_API_BASE, config.ENTITY_ID)

        # Build and send payload
        payload = {
            'entity_id': config.ENTITY_ID,
            'customer_name': cust_data['name'],
            'gstin': cust_data['gstin'] or '',
            'pan': cust_data['pan'] or '',
            'address': cust_data['address'] or '',
            'state': cust_data['state'] or '',
            'city': cust_data['city'] or '',
            'pincode': cust_data['pincode'] or '',
            'phone': cust_data['phone'] or '',
            'email': cust_data['email'] or '',
            'tally_company': cust_data['tally_company'],
        }

        response = syncer._api_request('POST', '/import/tally-customer-payload/', json=payload)

        if response.status_code not in [200, 201]:
            return None, f"Catalytics API error: {response.text}"

        result = response.json()
        if result.get('status') != 'success':
            return None, f"API error: {result.get('message')}"

        # Mark as synced
        db = Database(config.SQLITE_DB_PATH)
        data = result.get('data', {})
        catalytics_id = data.get('customer_id') or data.get('id')
        db.mark_customer_synced(customer_id, catalytics_id, json.dumps(result))
        db.close()

        return catalytics_id, None

    except Exception as e:
        return None, str(e)


@app.route('/api/customers/<int:customer_id>/refetch', methods=['POST'])
def refetch_customer(customer_id):
    """Refetch individual customer from Tally, update local DB, and sync to Catalytics"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()

        # Get customer details
        cursor.execute('''
            SELECT id, name, tally_company
            FROM customers WHERE id = ?
        ''', (customer_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Customer not found'}), 404

        customer_id_val, customer_name, company_name = row

        # Fetch full ledger data from Tally
        try:
            import tally_client
            import json

            ledger_data = tally_client.get_ledger_by_name(company_name, customer_name, config.TALLY_URL)
            if not ledger_data:
                conn.close()
                return jsonify({'success': False, 'error': f'Customer "{customer_name}" not found in Tally'}), 404

            # Prepare update data with all customer fields
            customer_data = {
                'tally_guid': ledger_data.get('guid'),
                'gstin': ledger_data.get('gstin', ''),
                'pan': ledger_data.get('pan', ''),
                'address': ledger_data.get('address', ''),
                'state': ledger_data.get('state', ''),
                'city': ledger_data.get('city', ''),
                'pincode': ledger_data.get('pincode', ''),
                'phone': ledger_data.get('phone', ''),
                'email': ledger_data.get('email', ''),
                'data_json': json.dumps(ledger_data),
            }

        except Exception as fetch_error:
            conn.close()
            return jsonify({'success': False, 'error': f'Tally fetch error: {str(fetch_error)}'}), 500

        # Update customer record with fresh data (same ID, updated data)
        try:
            from db import Database
            db = Database(config.SQLITE_DB_PATH)
            db.update_customer(customer_id_val, customer_data)
            db.close()
        except Exception as update_error:
            conn.close()
            return jsonify({'success': False, 'error': f'Database update error: {str(update_error)}'}), 500

        conn.close()

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Refetch Customer',
                'status': 'success',
                'detail': f'Customer "{customer_name}" refetched from Tally'
            })

        return jsonify({
            'success': True,
            'message': f'Customer "{customer_name}" refetched successfully from Tally'
        })

    except Exception as e:
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Refetch Customer',
                'status': 'error',
                'detail': f'Error: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


def _sync_product_to_catalytics(product_id):
    """Helper: Sync a single product to Catalytics server"""
    try:
        from sync_to_catalytics import CatalyticsSyncer
        from db import Database

        db = Database(config.SQLITE_DB_PATH)
        product = db.query_all('SELECT * FROM products WHERE id = ? LIMIT 1', (product_id,))
        db.close()

        if not product:
            return None, "Product not found"

        product_data = product[0]
        syncer = CatalyticsSyncer(config.CATALYTICS_API_BASE, config.ENTITY_ID)

        # Build and send payload
        payload = {
            'entity_id': config.ENTITY_ID,
            'stock_item_name': product_data['name'],
            'product_master_name': product_data['product_master_name'],
            'unit_master_name': product_data['unit_name'],
            'variant_name': product_data['variant_name'],
            'product_type_code': product_data['product_type_code'],
            'product_type_name': product_data['product_type_name'],
            'hsn_code': product_data['hsn_code'] or '',
            'rate': product_data['rate'] or 0.0,
            'gst_applicable': product_data['gst_applicable'] or '',
            'gst_rate': product_data['gst_rate'] or 0.0,
            'igst_rate': product_data['igst_rate'] or 0.0,
            'cgst_rate': product_data['cgst_rate'] or 0.0,
            'sgst_rate': product_data['sgst_rate'] or 0.0,
            'tally_company': product_data['tally_company'],
        }

        response = syncer._api_request('POST', '/import/tally-product_name-payload/', json=payload)

        if response.status_code not in [200, 201]:
            return None, f"Catalytics API error: {response.text}"

        result = response.json()
        if result.get('status') != 'success':
            return None, f"API error: {result.get('message')}"

        data = result.get('data', {})
        item_results = data.get('results', [])
        item_result = item_results[0] if item_results else {}
        item_status = item_result.get('status', '')

        catalytics_id = (
            item_result.get('product_id')
            or item_result.get('id')
            or data.get('product_id')
            or data.get('id')
        )

        # treat created, updated, skipped all as success
        if item_status == 'error':
            return None, item_result.get('message', 'Backend returned error')

        # Mark as synced
        db = Database(config.SQLITE_DB_PATH)
        db.mark_product_synced(product_id, catalytics_id, json.dumps(result))
        db.close()

        return catalytics_id, None

    except Exception as e:
        return None, str(e)


@app.route('/api/products/<int:product_id>/refetch', methods=['POST'])
def refetch_product(product_id):
    """Refetch individual product from Tally, update local DB, and sync to Catalytics"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()

        # Get product details
        cursor.execute('''
            SELECT id, name, tally_company
            FROM products WHERE id = ?
        ''', (product_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Product not found'}), 404

        product_id_val, product_name, company_name = row

        # Fetch full stock item data from Tally
        try:
            import tally_client
            import fetch_products
            import json

            stock_item = tally_client.get_stock_item_by_name(company_name, product_name, config.TALLY_URL)
            if not stock_item:
                conn.close()
                return jsonify({'success': False, 'error': f'Product "{product_name}" not found in Tally'}), 404

            # Parse the stock item name to extract components
            parsed = fetch_products.parse_stock_item_name(product_name)
            if not parsed:
                conn.close()
                return jsonify({'success': False, 'error': f'Product name "{product_name}" does not match expected format'}), 400

            # Prepare update data with all fields
            product_data = {
                'tally_guid': stock_item.get('guid'),
                'hsn_code': stock_item.get('hsn_code', ''),
                'unit': stock_item.get('unit', ''),
                'rate': stock_item.get('rate', 0.0),
                'description': stock_item.get('description', ''),
                'data_json': json.dumps(stock_item),
                'product_master_name': parsed['product_master_name'],
                'variant_name': parsed['variant_name'],
                'unit_name': parsed['unit_name'],
                'product_type_code': parsed['product_type_code'],
                'product_type_name': parsed['product_type_name'],
                'gst_applicable': stock_item.get('gst_applicable', ''),
                'gst_rate': stock_item.get('gst_rate', 0.0),
                'igst_rate': stock_item.get('igst_rate', 0.0),
                'cgst_rate': stock_item.get('cgst_rate', 0.0),
                'sgst_rate': stock_item.get('sgst_rate', 0.0),
            }

        except Exception as fetch_error:
            conn.close()
            return jsonify({'success': False, 'error': f'Tally fetch error: {str(fetch_error)}'}), 500

        # Update product record with fresh data (same ID, updated data)
        try:
            from db import Database
            db = Database(config.SQLITE_DB_PATH)
            db.update_product(product_id_val, product_data)
            db.close()
        except Exception as update_error:
            conn.close()
            return jsonify({'success': False, 'error': f'Database update error: {str(update_error)}'}), 500

        conn.close()

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Refetch Product',
                'status': 'success',
                'detail': f'Product "{product_name}" refetched from Tally'
            })

        return jsonify({
            'success': True,
            'message': f'Product "{product_name}" refetched successfully from Tally'
        })

    except Exception as e:
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Refetch Product',
                'status': 'error',
                'detail': f'Error: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/invoices/<int:invoice_id>/refetch', methods=['POST'])
def refetch_invoice(invoice_id):
    """Refetch individual invoice from Tally and update local DB"""
    try:
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()

        # Get invoice details
        cursor.execute('''
            SELECT id, tally_voucher_no, tally_company, catalytics_dc_id
            FROM invoices WHERE id = ?
        ''', (invoice_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Invoice not found'}), 404

        invoice_id, voucher_no, company_name, catalytics_dc_id = row

        # Check if DC is accepted in Catalytics (order_status 1 or 2 = accepted)
        if catalytics_dc_id:
            order_status, pg_err = _check_dc_order_status(catalytics_dc_id)
            if order_status is not None and order_status in (1, 2):
                conn.close()
                status_name = 'Accepted' if order_status == 1 else 'Processed'
                return jsonify({
                    'success': False,
                    'error': f'Cannot refetch Invoice #{voucher_no} Ã¢â‚¬â€ DC #{catalytics_dc_id} is already {status_name} (order_status={order_status}) in Catalytics.'
                }), 400

        # Fetch invoices from Tally using the correct TallyClient method
        try:
            from tally_client import TallyClient
            tally = TallyClient(config.TALLY_URL)
            invoices = tally.get_sales_invoices(company_name)

            if not invoices:
                conn.close()
                return jsonify({'success': False, 'error': f'No invoices found in Tally for {company_name}'}), 500

            # Find the specific voucher by number
            matched_invoice = None
            for inv in invoices:
                if str(inv.get('voucher_no', '')).strip() == str(voucher_no).strip():
                    matched_invoice = inv
                    break

            if not matched_invoice:
                conn.close()
                return jsonify({
                    'success': False,
                    'error': f'Invoice #{voucher_no} not found in Tally for company {company_name}'
                }), 404

            # Update local DB with fresh Tally data
            from db import json_dumps
            items_json = json_dumps(matched_invoice.get('items', []))

            # Build data_json from raw voucher if available
            raw_voucher = matched_invoice.get('raw_voucher', {})
            data_json = json_dumps(raw_voucher) if raw_voucher else None

            # Fetch ledger data for customer
            ledger_data_json = None
            customer_name = matched_invoice.get('customer_name', '')
            if customer_name:
                try:
                    from tally_client import get_ledger_by_name
                    ledger_data = get_ledger_by_name(company_name, customer_name, config.TALLY_URL)
                    if ledger_data:
                        ledger_data_json = json_dumps(ledger_data)
                except Exception:
                    pass

            cursor.execute('''
                UPDATE invoices
                SET customer_name = ?,
                    voucher_date = ?,
                    total_amount = ?,
                    tax_amount = ?,
                    items_json = ?,
                    data_json = ?,
                    ledger_data_json = ?,
                    billing_address = ?,
                    delivery_address = ?,
                    is_synced = 0,
                    last_updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                customer_name,
                matched_invoice.get('voucher_date', ''),
                matched_invoice.get('total_amount', 0),
                matched_invoice.get('tax_amount', 0),
                items_json,
                data_json,
                ledger_data_json,
                matched_invoice.get('billing_address', ''),
                matched_invoice.get('delivery_address', ''),
                invoice_id
            ))
            conn.commit()
            conn.close()

        except Exception as fetch_error:
            conn.close()
            return jsonify({'success': False, 'error': f'Tally fetch error: {str(fetch_error)}'}), 500

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Refetch Invoice',
                'status': 'success',
                'detail': f'Invoice #{voucher_no} refetched from Tally and updated'
            })

        return jsonify({
            'success': True,
            'message': f'Invoice #{voucher_no} refetched from Tally and updated. Click Resync to sync to Catalytics.'
        })

    except Exception as e:
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Refetch Invoice',
                'status': 'error',
                'detail': f'Error: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== LOG ENDPOINTS ====================

@app.route('/api/test/version', methods=['GET'])
def test_version():
    """Test endpoint to verify code version"""
    return jsonify({'version': 'enhanced-refetch-sync-v2', 'timestamp': datetime.now().isoformat()})


@app.route('/api/logs/recent', methods=['GET'])
def api_recent_logs():
    """Get recent operation logs for dashboard display"""
    try:
        lines = request.args.get('lines', 50, type=int)
        logs = dashboard_logger.get_recent_logs(lines)
        return jsonify({
            'success': True,
            'logs': logs,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear operation logs"""
    try:
        dashboard_logger.clear_logs()
        return jsonify({'success': True, 'message': 'Logs cleared'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



# ==================== PRODUCT DEBUG ENDPOINT ====================

@app.route('/api/debug/products-tally')
def debug_products_tally():
    """
    Debug endpoint: for each active company, call Tally directly and report:
    - How many stock items Tally returned
    - How many matched the PRODUCT_TYPE_MAP regex
    - How many were skipped (no type code / duplicate)
    - First 10 raw item names returned from Tally
    """
    try:
        from tally_client import TallyClient
        from fetch_products import parse_stock_item_name
        from db import Database

        tally = TallyClient(config.TALLY_URL)
        db_conn = Database(config.SQLITE_DB_PATH)
        active_companies = config.get_active_companies()

        result = {
            'active_companies': active_companies,
            'product_type_map': config.PRODUCT_TYPE_MAP,
            'companies': {}
        }

        for company_name in active_companies:
            try:
                raw_items = tally.get_products(company_name)
                company_result = {
                    'total_from_tally': len(raw_items),
                    'sample_names': [p['name'] for p in raw_items[:15]],
                    'matched': [],
                    'skipped_no_type': [],
                    'skipped_duplicate': [],
                    'errors': []
                }

                for product in raw_items:
                    name = product.get('name', '')
                    if not name:
                        continue
                    parsed = parse_stock_item_name(name)
                    if not parsed:
                        company_result['skipped_no_type'].append(name)
                    else:
                        existing = db_conn.product_exists(name)
                        if existing:
                            company_result['skipped_duplicate'].append({
                                'name': name,
                                'owned_by': existing.get('tally_company', '?')
                            })
                        else:
                            company_result['matched'].append({
                                'name': name,
                                'type': parsed['product_type_code'],
                                'variant': parsed['variant_name']
                            })

                company_result['count_matched']   = len(company_result['matched'])
                company_result['count_no_type']   = len(company_result['skipped_no_type'])
                company_result['count_duplicate']  = len(company_result['skipped_duplicate'])
                result['companies'][company_name] = company_result

            except Exception as e:
                result['companies'][company_name] = {'error': str(e)}

        db_conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== DATABASE CLEAR ENDPOINT ====================

@app.route('/api/database/clear', methods=['POST'])
def clear_database():
    """
    Clear selected tables (or all) from the local SQLite buffer.
    Request JSON body (optional):
        { "tables": ["customers", "products", "invoices"] }
    If 'tables' is omitted, ALL tables are cleared.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        allowed = ['customers', 'products', 'invoices', 'sync_status', 'duplicate_log']
        requested = data.get('tables', allowed)
        to_clear = [t for t in requested if t in allowed]
        if not to_clear:
            return jsonify({'success': False, 'error': 'No valid table names provided'}), 400

        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()
        counts = {}
        for table in to_clear:
            cursor.execute(f'SELECT COUNT(*) FROM {table}')
            counts[table] = cursor.fetchone()[0]
            cursor.execute(f'DELETE FROM {table}')
        conn.commit()
        conn.close()

        global db
        db = Database(config.SQLITE_DB_PATH)

        logger.warning(f"[CLEAR DB] Tables cleared: {to_clear} | Rows deleted: {counts}")
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Clear Database',
                'status': 'success',
                'detail': f"Cleared {', '.join(to_clear)} | rows: {counts}"
            })
        return jsonify({'success': True, 'cleared_tables': to_clear, 'rows_deleted': counts})
    except Exception as e:
        logger.error(f"Error clearing database: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== IMMEDIATE SYNC ENDPOINTS ====================

@app.route('/api/sync/now', methods=['POST'])
def sync_now():
    """Trigger immediate invoice sync (don't wait for automation)"""
    try:
        from sync_to_catalytics import CatalyticsSyncer

        logger.info("Triggering immediate invoice sync from dashboard")

        syncer = CatalyticsSyncer()
        result = syncer.sync_invoices_to_dc()

        synced = result.get('synced', 0)
        verified = result.get('verified', 0)
        failed = result.get('failed', 0)
        total = result.get('total', 0)

        message = f'Sync complete: {synced} synced, {verified} verified, {failed} failed (Total: {total})'
        status = 'success' if failed == 0 else 'warning'

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Sync Trigger',
                'status': status,
                'detail': message
            })

        return jsonify({
            'success': True,
            'message': message,
            'synced': synced,
            'verified': verified,
            'failed': failed,
            'total': total
        })

    except Exception as e:
        error_msg = f'Sync failed: {str(e)}'
        logger.error(error_msg, exc_info=True)

        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': error_msg
            })

        return jsonify({'success': False, 'error': error_msg}), 500


@app.route('/api/fetch/now', methods=['POST'])
def fetch_now():
    """Trigger immediate invoice fetch (don't wait for automation)"""
    try:
        from fetch_invoices import fetch_invoices_from_all_companies

        logger.info("Triggering immediate invoice fetch from dashboard")

        stats = fetch_invoices_from_all_companies()

        message = (f'Fetch complete: {stats.get("new_saved", 0)} new, '
                   f'{stats.get("updated_saved", 0)} updated, '
                   f'{stats.get("deleted", 0)} deleted, '
                   f'{stats.get("errors", 0)} errors')
        status = 'success' if stats.get('errors', 0) == 0 else 'warning'

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Manual Fetch Trigger',
                'status': status,
                'detail': message
            })

        return jsonify({
            'success': True,
            'message': message,
            'new_saved': stats.get('new_saved', 0),
            'updated': stats.get('updated_saved', 0),
            'deleted': stats.get('deleted', 0),
            'errors': stats.get('errors', 0),
            'total': stats.get('total', 0)
        })

    except Exception as e:
        error_msg = f'Fetch failed: {str(e)}'
        logger.error(error_msg, exc_info=True)

        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': error_msg
            })

        return jsonify({'success': False, 'error': error_msg}), 500


# ==================== AUTOMATION CONTROL ENDPOINTS ====================

@app.route('/api/automation/status')
def automation_status():
    """Get automation status and configuration"""
    try:
        manager = get_manager()
        status = manager.get_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/automation/start', methods=['POST'])
def automation_start():
    """Start automation loops"""
    try:
        manager = get_manager()
        success = manager.start()

        if success:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Start Automation',
                    'status': 'success',
                    'detail': 'Automation loops started'
                })
            return jsonify({'success': True, 'message': 'Automation started'})
        else:
            return jsonify({'success': False, 'message': 'Automation already running'}), 400

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Start automation failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/stop', methods=['POST'])
def automation_stop():
    """Stop automation loops"""
    try:
        manager = get_manager()
        success = manager.stop()

        if success:
            with activity_lock:
                activity_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'action': 'Stop Automation',
                    'status': 'success',
                    'detail': 'Automation loops stopped'
                })
            return jsonify({'success': True, 'message': 'Automation stopped'})
        else:
            return jsonify({'success': False, 'message': 'Automation not running'}), 400

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Stop automation failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/restart', methods=['POST'])
def automation_restart():
    """Restart automation loops"""
    try:
        manager = get_manager()
        success = manager.restart()

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Restart Automation',
                'status': 'success',
                'detail': 'Automation loops restarted'
            })

        return jsonify({'success': True, 'message': 'Automation restarted'})

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Restart automation failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/automation/set-intervals', methods=['POST'])
def automation_set_intervals():
    """Update automation intervals"""
    try:
        data = request.get_json(force=True, silent=True)
        intervals = {}

        # Validate and convert intervals
        if 'fetch_master' in data:
            intervals['fetch_master'] = int(data['fetch_master'])
        if 'fetch_invoices' in data:
            intervals['fetch_invoices'] = int(data['fetch_invoices'])
        if 'sync' in data:
            intervals['sync'] = int(data['sync'])

        if not intervals:
            return jsonify({'success': False, 'message': 'No valid intervals provided'}), 400

        manager = get_manager()
        manager.set_intervals(intervals)

        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Update Intervals',
                'status': 'success',
                'detail': f'Updated: {intervals}'
            })

        return jsonify({'success': True, 'message': 'Intervals updated', 'intervals': intervals})

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Set intervals failed: {str(e)}'
            })
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== DIAGNOSTICS ENDPOINT ====================

@app.route('/api/diagnostics')
def api_diagnostics():
    """Get system diagnostics and health check"""
    try:
        diagnostics = {
            'timestamp': datetime.now().isoformat(),
            'system': {},
            'database': {},
            'connections': {},
            'last_sync': {},
            'errors': {}
        }

        # System info
        import platform
        diagnostics['system'] = {
            'platform': platform.system(),
            'python_version': platform.python_version(),
            'entity_name': config.ENTITY_NAME,
            'entity_id': config.ENTITY_ID
        }

        # Database stats
        conn = sqlite3.connect(config.SQLITE_DB_PATH)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM customers")
        total_customers = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM customers WHERE is_synced = 1")
        synced_customers = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM products")
        total_products = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM products WHERE is_synced = 1")
        synced_products = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM invoices")
        total_invoices = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM invoices WHERE is_synced = 1")
        synced_invoices = cursor.fetchone()[0]

        # Get database file size
        import os
        db_size = os.path.getsize(config.SQLITE_DB_PATH) if os.path.exists(config.SQLITE_DB_PATH) else 0

        diagnostics['database'] = {
            'path': config.SQLITE_DB_PATH,
            'size_mb': round(db_size / (1024 * 1024), 2),
            'customers': {'total': total_customers, 'synced': synced_customers, 'pending': total_customers - synced_customers},
            'products': {'total': total_products, 'synced': synced_products, 'pending': total_products - synced_products},
            'invoices': {'total': total_invoices, 'synced': synced_invoices, 'pending': total_invoices - synced_invoices}
        }

        # Test Tally connection (uses lock to avoid crashing Tally)
        try:
            from tally_client import _tally_lock
            if not _tally_lock.acquire(timeout=3):
                diagnostics['connections']['tally'] = {
                    'status': 'online',
                    'url': config.TALLY_URL,
                    'response_time_ms': 0,
                    'note': 'busy (request in progress)'
                }
            else:
                try:
                    tally_response = requests.get(
                        config.TALLY_URL, timeout=config.TALLY_CHECK_TIMEOUT,
                        headers={"Connection": "close"},
                    )
                    diagnostics['connections']['tally'] = {
                        'status': 'online' if tally_response.status_code == 200 else 'error',
                        'url': config.TALLY_URL,
                        'response_time_ms': round(tally_response.elapsed.total_seconds() * 1000)
                    }
                finally:
                    _tally_lock.release()
        except Exception as e:
            diagnostics['connections']['tally'] = {
                'status': 'offline',
                'url': config.TALLY_URL,
                'error': str(e)
            }

        # Test Catalytics connection
        try:
            cat_response = requests.get(f"{config.CATALYTICS_API_BASE}/api/health/", timeout=5)
            diagnostics['connections']['catalytics'] = {
                'status': 'online' if cat_response.status_code == 200 else 'error',
                'url': config.CATALYTICS_API_BASE,
                'response_time_ms': round(cat_response.elapsed.total_seconds() * 1000)
            }
        except Exception as e:
            diagnostics['connections']['catalytics'] = {
                'status': 'offline',
                'url': config.CATALYTICS_API_BASE,
                'error': str(e)
            }

        # Last sync times
        cursor.execute("SELECT MAX(last_sync_at) FROM customers WHERE is_synced = 1")
        last_customer_sync = cursor.fetchone()[0]

        cursor.execute("SELECT MAX(last_sync_at) FROM products WHERE is_synced = 1")
        last_product_sync = cursor.fetchone()[0]

        cursor.execute("SELECT MAX(last_sync_at) FROM invoices WHERE is_synced = 1")
        last_invoice_sync = cursor.fetchone()[0]

        diagnostics['last_sync'] = {
            'customers': last_customer_sync or 'Never',
            'products': last_product_sync or 'Never',
            'invoices': last_invoice_sync or 'Never'
        }

        # Recent errors
        cursor.execute("""
            SELECT 'customer' as type, name, last_sync_error, last_sync_at
            FROM customers
            WHERE last_sync_error IS NOT NULL
            ORDER BY last_sync_at DESC
            LIMIT 5
        """)
        customer_errors = cursor.fetchall()

        cursor.execute("""
            SELECT 'product' as type, name, last_sync_error, last_sync_at
            FROM products
            WHERE last_sync_error IS NOT NULL
            ORDER BY last_sync_at DESC
            LIMIT 5
        """)
        product_errors = cursor.fetchall()

        cursor.execute("""
            SELECT 'invoice' as type, tally_voucher_no, last_sync_error, last_sync_at
            FROM invoices
            WHERE last_sync_error IS NOT NULL
            ORDER BY last_sync_at DESC
            LIMIT 5
        """)
        invoice_errors = cursor.fetchall()

        all_errors = []
        for err in customer_errors + product_errors + invoice_errors:
            all_errors.append({
                'type': err[0],
                'name': err[1],
                'error': err[2],
                'time': err[3]
            })

        diagnostics['errors'] = {
            'recent_count': len(all_errors),
            'items': all_errors[:10]  # Limit to 10 most recent
        }

        conn.close()

        return jsonify(diagnostics)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== DATA MATCHING ENDPOINT ====================

@app.route('/api/verify/data-match', methods=['POST'])
def verify_data_match():
    """Run data matching verification"""
    try:
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Data Matching Started',
                'status': 'started',
                'detail': 'Verifying SQLite Ã¢â€ â€ Catalytics consistency'
            })

        # Run data matcher
        matcher = DataMatcher()
        results = matcher.match_all()

        # Log result
        status = 'success' if results.get('overall_match') else 'warning'
        with activity_lock:
            activity_log.appendleft({
                'time': datetime.now().isoformat(),
                'action': 'Data Matching Complete',
                'status': status,
                'detail': f"Overall: {'All Match' if results.get('overall_match') else 'Mismatches Found'}"
            })

        if not results.get('overall_match'):
            with activity_lock:
                error_log.appendleft({
                    'time': datetime.now().isoformat(),
                    'error': 'Data mismatches detected - check detailed report'
                })

        return jsonify({
            'success': True,
            'results': results
        })

    except Exception as e:
        with activity_lock:
            error_log.appendleft({
                'time': datetime.now().isoformat(),
                'error': f'Data matching failed: {str(e)}'
            })
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

        app_name = os.getenv('WINDOWS_STARTUP_APP_NAME', 'BOLMiddlewareDashboard').strip() or 'BOLMiddlewareDashboard'
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


# ==================== OFFLINE DC PRINT ====================

@app.route('/print/dc/<path:voucher_no>')
def print_dc(voucher_no):
    """Offline DC print page — renders a printable DC from local SQLite data."""
    import json as _json

    conn = sqlite3.connect(config.SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find invoice by voucher number (match across any company)
    cursor.execute(
        'SELECT * FROM invoices WHERE tally_voucher_no = ? ORDER BY first_fetched_at DESC LIMIT 1',
        (voucher_no,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return f'<h3>DC not found: {voucher_no}</h3><p>No local record for this voucher number.</p>', 404

    invoice = dict(row)

    # Parse items
    try:
        items = _json.loads(invoice.get('items_json') or '[]') or []
    except Exception:
        items = []

    # Parse raw voucher data_json for vehicle / driver / filling station
    try:
        raw_voucher = _json.loads(invoice.get('data_json') or '{}') or {}
    except Exception:
        raw_voucher = {}

    vehicle_no = (
        raw_voucher.get('DISPATCHEDTHROUGH') or
        raw_voucher.get('MOTORVEHICLENO') or
        ''
    ).strip()
    driver_name = (
        raw_voucher.get('BASICSHIPDOCUMENTNO') or
        raw_voucher.get('DRIVERNAME') or
        ''
    ).strip()
    filling_station = (
        invoice.get('filling_station') or
        invoice.get('godown_name') or
        invoice.get('location_name') or
        ''
    ).strip()

    # Parse customer details from ledger_data_json first, fallback to customers table
    customer_gstin = ''
    customer_phone = ''
    customer_address = invoice.get('billing_address') or invoice.get('delivery_address') or ''
    customer_city = ''
    customer_state = ''
    customer_pincode = ''

    try:
        ledger = _json.loads(invoice.get('ledger_data_json') or '{}') or {}
        customer_gstin = (ledger.get('gstin') or ledger.get('GSTIN') or ledger.get('PARTYGSTIN') or '').lstrip(':')
        customer_phone = ledger.get('phone') or ledger.get('MOBILE') or ledger.get('LEDGERMOBILE') or ''
        if not customer_address:
            addr_list = ledger.get('ADDRESSES') or ledger.get('addresses') or []
            customer_address = ', '.join(addr_list) if isinstance(addr_list, list) else str(addr_list)
        customer_state = ledger.get('state') or ledger.get('STATENAME') or ''
        customer_pincode = ledger.get('pincode') or ledger.get('PINCODE') or ''
    except Exception:
        pass

    # Fall back to customers table
    if not customer_gstin or not customer_phone:
        cursor.execute(
            'SELECT gstin, phone, address, city, state, pincode FROM customers WHERE name = ? LIMIT 1',
            (invoice.get('customer_name', ''),)
        )
        cust_row = cursor.fetchone()
        if cust_row:
            customer_gstin = customer_gstin or cust_row['gstin'] or ''
            customer_phone = customer_phone or cust_row['phone'] or ''
            if not customer_address:
                customer_address = cust_row['address'] or ''
            customer_city = customer_city or cust_row['city'] or ''
            customer_state = customer_state or cust_row['state'] or ''
            customer_pincode = customer_pincode or cust_row['pincode'] or ''

    conn.close()

    # Get HSN codes for items from products table (best effort)
    hsn_map = {}
    try:
        conn2 = sqlite3.connect(config.SQLITE_DB_PATH)
        c2 = conn2.cursor()
        for item in items:
            nm = (item.get('item_name') or '').strip()
            if nm and nm not in hsn_map:
                c2.execute(
                    'SELECT hsn_code FROM products WHERE lower(replace(name," ","")) = ? LIMIT 1',
                    (nm.lower().replace(' ', ''),)
                )
                r = c2.fetchone()
                if r and r[0]:
                    hsn_map[nm] = r[0]
        conn2.close()
    except Exception:
        pass

    for item in items:
        nm = (item.get('item_name') or '').strip()
        item['hsn_code'] = hsn_map.get(nm, '')

    # Format date (YYYYMMDD → DD-MM-YYYY)
    raw_date = str(invoice.get('voucher_date') or '').replace('-', '').strip()
    if len(raw_date) == 8:
        voucher_date = f"{raw_date[6:8]}-{raw_date[4:6]}-{raw_date[:4]}"
    else:
        voucher_date = invoice.get('voucher_date') or '-'

    total_quantity = sum(
        int(item.get('quantity') or 0)
        for item in items
    )

    # Generate QR code from voucher_no (works fully offline)
    qr_data_uri = ''
    try:
        import qrcode
        import io as _io
        import base64 as _b64
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(voucher_no)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color='black', back_color='white')
        buf = _io.BytesIO()
        qr_img.save(buf, format='PNG')
        qr_data_uri = 'data:image/png;base64,' + _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass  # QR code is optional; print still works without it

    return render_template(
        'dc_print.html',
        voucher_no=voucher_no,
        voucher_date=voucher_date,
        entity_name=config.ENTITY_NAME,
        tally_company=invoice.get('tally_company', ''),
        filling_station=filling_station,
        customer_name=invoice.get('customer_name', ''),
        customer_address=customer_address,
        customer_city=customer_city,
        customer_state=customer_state,
        customer_pincode=customer_pincode,
        customer_gstin=customer_gstin,
        customer_phone=customer_phone,
        vehicle_no=vehicle_no,
        driver_name=driver_name,
        items=items,
        total_quantity=total_quantity,
        qr_data_uri=qr_data_uri,
    )


if __name__ == '__main__':
    # Ensure logs directory exists (next to the exe, not in CWD)
    os.makedirs(str(BASE_DIR / 'logs'), exist_ok=True)

    default_debug = 'false' if getattr(sys, 'frozen', False) else 'true'
    dashboard_debug = _env_flag('DASHBOARD_DEBUG', default_debug)

    # Run Flask dashboard server
    print(f"""
============================================================
  BHOX Middleware Dashboard
  Entity: {config.ENTITY_NAME} (ID: {config.ENTITY_ID})
  Active Companies: {', '.join(config.get_active_companies())}
  Dashboard URL: http://{config.WEB_UI_HOST}:{config.WEB_UI_PORT}
  Auto Start Automation: {_env_flag('AUTO_START_AUTOMATION', 'true' if getattr(sys, 'frozen', False) else 'false')}
  Auto Register Startup: {_env_flag('AUTO_REGISTER_WINDOWS_STARTUP', 'false')}
============================================================
    """)

    maybe_register_windows_startup()
    maybe_start_automation()
    auto_updater.start()
    maybe_open_dashboard_browser()
    app.run(host=config.WEB_UI_HOST, port=config.WEB_UI_PORT, debug=dashboard_debug, use_reloader=False)

