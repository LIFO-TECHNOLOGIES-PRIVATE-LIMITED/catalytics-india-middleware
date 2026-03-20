"""
Automation Manager for CO Middleware
Manages automated fetch and sync operations with configurable intervals
Single company version (adapted from arasan_gas)
"""

import json
import time
import threading
import io
from datetime import datetime, timedelta
from pathlib import Path
import logging
import os
import sys

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Dashboard logger for terminal output
try:
    from log_capture import dashboard_logger
except ImportError:
    dashboard_logger = None

# State file path
STATE_FILE = Path(ROOT_DIR) / 'automation_state.json'

# Default intervals (in seconds)
DEFAULT_INTERVALS = {
    'fetch_invoices': 120,  # 2 minutes - DCs
    'sync': 60,  # 60 seconds (1 minute)
    'fetch_master': 1800,  # 30 minutes - products + customers (INCREASED to reduce Tally load)
}

INITIAL_DELAYS = {
    'fetch_master': 10,  # Products + Customers start at 0:10 (customers skip full fetch if already have details)
    'fetch_invoices': 120,  # DCs start at 2:00 (safe timing)
    'sync': 180,  # Sync starts at 3:00
}


class DashboardLogHandler(logging.Handler):
    """Routes logging output to the dashboard terminal and a StringIO buffer."""

    def __init__(self, string_buffer=None):
        super().__init__()
        self.buffer = string_buffer

    def emit(self, record):
        msg = self.format(record)
        if dashboard_logger:
            dashboard_logger.write_raw(msg)
        if self.buffer:
            self.buffer.write(msg + '\n')


class AutomationManager:
    """Manages automated fetch and sync operations for single company"""

    def __init__(self):
        self.state = self._load_state()
        self.threads = {}
        self.stop_flags = {}
        self.lock = threading.Lock()

        # Load intervals from .env if available
        sync_interval = cfg.get_env_int("SYNC_INTERVAL")
        if sync_interval and sync_interval > 0:
            self.state['intervals']['sync'] = sync_interval
            logger.info(f"Using SYNC_INTERVAL from .env: {sync_interval} seconds")

        # Reset status on startup — threads don't survive process restart
        if self.state['status'] == 'running':
            logger.info("Resetting stale 'running' state to 'stopped' (process restarted)")
            self.state['status'] = 'stopped'
            self._save_state()

    def _load_state(self):
        """Load automation state from file"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading state: {e}")

        # Default state
        return {
            'status': 'stopped',  # stopped, running
            'intervals': DEFAULT_INTERVALS.copy(),
            'last_runs': {
                'fetch_master': None,
                'fetch_invoices': None,
                'sync': None
            },
            'next_runs': {
                'fetch_master': None,
                'fetch_invoices': None,
                'sync': None
            },
            'started_at': None,
            'stopped_at': None
        }

    def _save_state(self):
        """Save automation state to file"""
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def get_status(self):
        """Get current automation status"""
        with self.lock:
            status = self.state.copy()

            # Calculate time until next runs
            if self.state['status'] == 'running':
                now = datetime.now()
                for task in ['fetch_master', 'fetch_invoices', 'sync']:
                    if self.state['next_runs'][task]:
                        next_run = datetime.fromisoformat(self.state['next_runs'][task])
                        seconds_until = max(0, (next_run - now).total_seconds())
                        status[f'{task}_countdown'] = int(seconds_until)
                    else:
                        status[f'{task}_countdown'] = 0

            return status

    def set_intervals(self, intervals):
        """Update automation intervals"""
        with self.lock:
            for task, interval in intervals.items():
                if task in self.state['intervals'] and interval > 0:
                    self.state['intervals'][task] = interval
            self._save_state()

        logger.info(f"Intervals updated: {intervals}")
        return True

    def start(self):
        """Start automation loops - threads have built-in delays to avoid overwhelming Tally"""
        with self.lock:
            if self.state['status'] == 'running':
                logger.warning("Automation already running")
                return False

            self.state['status'] = 'running'
            now = datetime.now()
            self.state['started_at'] = now.isoformat()
            for task, delay in INITIAL_DELAYS.items():
                self.state['next_runs'][task] = (now + timedelta(seconds=delay)).isoformat()
            self._save_state()

        # Start all automation threads immediately
        # Each thread has its own initial delay (5s, 10s, 15s) to stagger Tally requests
        logger.info("Starting automation threads (each has built-in delay to protect Tally)...")
        
        self._start_thread('fetch_master', self._fetch_master_loop)
        self._start_thread('fetch_invoices', self._fetch_invoices_loop)
        self._start_thread('sync', self._sync_loop)

        logger.info("Automation started - threads will begin after their initial delays")
        return True

    def stop(self):
        """Stop automation loops"""
        with self.lock:
            if self.state['status'] != 'running':
                logger.warning("Automation not running")
                return False

            self.state['status'] = 'stopped'
            self.state['stopped_at'] = datetime.now().isoformat()
            self.state['next_runs'] = {
                'fetch_master': None,
                'fetch_invoices': None,
                'sync': None
            }
            self._save_state()

        # Stop all threads
        for task in list(self.stop_flags.keys()):
            self.stop_flags[task].set()

        # Wait for threads to finish
        for task, thread in list(self.threads.items()):
            thread.join(timeout=5)

        self.threads.clear()
        self.stop_flags.clear()

        logger.info("Automation stopped")
        return True

    def restart(self):
        """Restart automation loops"""
        logger.info("Restarting automation...")
        self.stop()
        time.sleep(2)
        return self.start()

    def _start_thread(self, task_name, target_func):
        """Start a background thread for a task"""
        self.stop_flags[task_name] = threading.Event()
        thread = threading.Thread(target=target_func, daemon=True)
        thread.start()
        self.threads[task_name] = thread

    def _log_to_dashboard(self, message):
        """Write a message to the dashboard terminal output"""
        if dashboard_logger:
            dashboard_logger.write_log(message)

    def _run_with_log_capture(self, func, *args, **kwargs):
        """Run a function while capturing its log output to the dashboard.
        Returns (success: bool, output: str)."""
        buf = io.StringIO()
        handler = DashboardLogHandler(string_buffer=buf)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

        root = logging.getLogger()
        root.addHandler(handler)
        try:
            func(*args, **kwargs)
            return True, buf.getvalue()
        except Exception as exc:
            logger.error(f"Error running {func.__name__}: {exc}")
            return False, buf.getvalue()
        finally:
            root.removeHandler(handler)

    def _fetch_master_loop(self):
        """Continuous loop for fetching master data (customers + products)"""
        from fetch_customers import build_config as build_fetch_customers_config, run_once as fetch_customers_once
        from fetch_products import build_config as build_fetch_products_config, run_once as fetch_products_once
        from types import SimpleNamespace

        task = 'fetch_master'
        
        # Initial delay to avoid hitting Tally immediately on startup
        # INCREASED to 20 seconds for maximum stability on 8GB RAM systems
        initial_delay = INITIAL_DELAYS[task]
        logger.info(f"{task}: Waiting {initial_delay} seconds before first run...")
        if self.stop_flags[task].wait(timeout=initial_delay):
            return  # Stop flag was set during initial delay
        
        while not self.stop_flags[task].is_set():
            try:
                # Update next run time
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # Run fetch
                logger.info(f"Running {task}...")
                self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA STARTED ===")

                # Build args for products (FETCH PRODUCTS FIRST)
                args = SimpleNamespace(
                    config=cfg.resolve_env_path(ROOT_DIR),
                    db_path=cfg.get_env("TALLY_DB_PATH"),
                    tally_url=cfg.get_env("TALLY_URL"),
                    company=cfg.get_env("TALLY_COMPANY"),
                    entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
                    fetch_full=cfg.get_env_bool("TALLY_FETCH_FULL_PRODUCTS", False),
                    log_level=cfg.get_env("LOG_LEVEL", "INFO"),
                    log_json=cfg.get_env_bool("LOG_JSON", False),
                    log_file=None
                )

                # Fetch products FIRST
                fetch_config = build_fetch_products_config(args)
                ok2 = True
                prod_created = 0
                prod_updated = 0
                try:
                    result = fetch_products_once(fetch_config)
                    prod_created = result.get('created', 0)
                    prod_updated = result.get('updated', 0)
                except Exception as e:
                    logger.error(f"Product fetch failed: {e}", exc_info=True)
                    ok2 = False

                # Add extra delay between products and customers to protect Tally
                logger.info(f"{task}: Waiting 10 seconds before fetching customers...")
                if self.stop_flags[task].wait(timeout=10):
                    return  # Stop flag was set during delay

                # Build args namespace for customers
                args = SimpleNamespace(
                    config=cfg.resolve_env_path(ROOT_DIR),
                    db_path=cfg.get_env("TALLY_DB_PATH"),
                    tally_url=cfg.get_env("TALLY_URL"),
                    company=cfg.get_env("TALLY_COMPANY"),
                    entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
                    fetch_full=True,  # Fetch full details only for customers that don't have them
                    log_level=cfg.get_env("LOG_LEVEL", "INFO"),
                    log_json=cfg.get_env_bool("LOG_JSON", False),
                    log_file=None
                )

                # Fetch customers SECOND
                fetch_config = build_fetch_customers_config(args)
                ok1 = True
                cust_created = 0
                cust_updated = 0
                try:
                    result = fetch_customers_once(fetch_config)
                    cust_created = result.get('created', 0)
                    cust_updated = result.get('updated', 0)
                except Exception as e:
                    logger.error(f"Customer fetch failed: {e}", exc_info=True)
                    ok1 = False

                if ok1 and ok2:
                    self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA COMPLETED (products: created={prod_created}, updated={prod_updated} | customers: created={cust_created}, updated={cust_updated}) ===")
                elif ok2:
                    self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA PARTIAL (products OK: created={prod_created}, updated={prod_updated} | customers FAILED) ===")
                elif ok1:
                    self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA PARTIAL (products FAILED | customers OK: created={cust_created}, updated={cust_updated}) ===")
                else:
                    self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA FAILED ===")

                # Update last run time
                with self.lock:
                    self.state['last_runs'][task] = datetime.now().isoformat()
                    self._save_state()

                logger.info(f"{task} completed")

                # Wait for interval or stop signal
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in {task}: {e}")
                self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)

    def _fetch_invoices_loop(self):
        """Continuous loop for fetching invoices/DCs"""
        from fetch_invoices import build_config as build_fetch_invoices_config, run_once as fetch_invoices_once
        from types import SimpleNamespace

        task = 'fetch_invoices'

        # Initial delay to avoid hitting Tally immediately on startup
        # INCREASED to 30 seconds for maximum stability on 8GB RAM systems
        initial_delay = INITIAL_DELAYS[task]
        logger.info(f"{task}: Waiting {initial_delay} seconds before first run...")
        if self.stop_flags[task].wait(timeout=initial_delay):
            return  # Stop flag was set during initial delay

        while not self.stop_flags[task].is_set():
            try:
                # Update next run time
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # Run fetch
                logger.info(f"Running {task}...")
                self._log_to_dashboard(f"=== AUTO: FETCH DCs STARTED ===")

                # Build args namespace
                args = SimpleNamespace(
                    config=cfg.resolve_env_path(ROOT_DIR),
                    db_path=cfg.get_env("TALLY_DB_PATH"),
                    tally_url=cfg.get_env("TALLY_URL"),
                    company=cfg.get_env("TALLY_COMPANY"),
                    entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
                    from_date=cfg.get_env("TALLY_FROM_DATE"),  # Read from .env
                    to_date=cfg.get_env("TALLY_TO_DATE"),  # Read from .env
                    days_back=cfg.get_env_int("TALLY_DAYS_BACK"),  # Read from .env (CRITICAL FIX)
                    fetch_stock=False,  # Don't fetch stock details for DCs (too slow)
                    dry_run=False,
                    log_level=cfg.get_env("LOG_LEVEL", "INFO"),
                    log_json=cfg.get_env_bool("LOG_JSON", False),
                    log_file=None
                )

                # Fetch DCs
                fetch_config = build_fetch_invoices_config(args)
                ok = True
                error_msg = None
                try:
                    result = fetch_invoices_once(fetch_config)
                    # Check if any DCs were created/updated
                    created = result.get('created', 0)
                    updated = result.get('updated', 0)
                    if created > 0 or updated > 0:
                        self._log_to_dashboard(f"=== AUTO: FETCH DCs COMPLETED (created={created}, updated={updated}) ===")
                    else:
                        self._log_to_dashboard(f"=== AUTO: FETCH DCs COMPLETED (no new DCs) ===")
                except Exception as e:
                    logger.error(f"DC fetch failed: {e}", exc_info=True)
                    error_msg = str(e)
                    ok = False
                    self._log_to_dashboard(f"=== AUTO: FETCH DCs FAILED: {error_msg} ===")

                # Update last run time
                with self.lock:
                    self.state['last_runs'][task] = datetime.now().isoformat()
                    self._save_state()

                logger.info(f"{task} completed")

                # Wait for interval or stop signal
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in {task}: {e}")
                self._log_to_dashboard(f"=== AUTO: FETCH DCs ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)


    def _sync_loop(self):
        """Continuous loop for syncing to Catalytics"""
        from sync_catalytics import build_config as build_sync_config, run_once as sync_once
        from sync_customers import build_config as build_sync_customers_config, run_once as sync_customers_once
        from sync_products import build_config as build_sync_products_config, run_once as sync_products_once
        from types import SimpleNamespace

        task = 'sync'
        
        # Initial delay to avoid hitting Tally immediately on startup
        # INCREASED to 60 seconds for maximum stability on 8GB RAM systems
        initial_delay = INITIAL_DELAYS[task]
        logger.info(f"{task}: Waiting {initial_delay} seconds before first run...")
        if self.stop_flags[task].wait(timeout=initial_delay):
            return  # Stop flag was set during initial delay
        
        while not self.stop_flags[task].is_set():
            try:
                # Update next run time
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # Run sync
                logger.info(f"Running {task}...")
                self._log_to_dashboard(f"=== AUTO: SYNC TO CATALYTICS STARTED ===")

                # Build args namespace
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
                sync_config = build_sync_config(args)
                ok1 = True
                try:
                    sync_once(sync_config)
                except Exception as e:
                    logger.error(f"DC sync failed: {e}")
                    ok1 = False

                # Sync customers
                sync_config = build_sync_customers_config(args)
                ok2 = True
                try:
                    sync_customers_once(sync_config)
                except Exception as e:
                    logger.error(f"Customer sync failed: {e}")
                    ok2 = False

                # Sync products
                sync_config = build_sync_products_config(args)
                ok3 = True
                try:
                    sync_products_once(sync_config)
                except Exception as e:
                    logger.error(f"Product sync failed: {e}")
                    ok3 = False

                if ok1 and ok2 and ok3:
                    self._log_to_dashboard(f"=== AUTO: SYNC TO CATALYTICS COMPLETED ===")
                else:
                    self._log_to_dashboard(f"=== AUTO: SYNC TO CATALYTICS FAILED ===")

                # Update last run time
                with self.lock:
                    self.state['last_runs'][task] = datetime.now().isoformat()
                    self._save_state()

                logger.info(f"{task} completed")

                # Wait for interval or stop signal
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in {task}: {e}")
                self._log_to_dashboard(f"=== AUTO: SYNC ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)


# Global automation manager instance
_manager = None

def get_manager():
    """Get or create the global automation manager instance"""
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager
