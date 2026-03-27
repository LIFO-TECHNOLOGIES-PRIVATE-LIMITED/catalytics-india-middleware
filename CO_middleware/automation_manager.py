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
from config import config

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

# Default intervals (in seconds) — read from .env via config
# Master (products + customers): once per day (1440 min default)
# DC fetch: every 20 seconds (FETCH_INVOICES_INTERVAL_SECONDS default)
# DC sync runs inline after each DC fetch — no separate sync timer needed
DEFAULT_INTERVALS = {
    'fetch_master': config.FETCH_MASTER_INTERVAL_MINUTES * 60,   # default 1440 min = 1 day
    'fetch_invoices': config.FETCH_INVOICES_INTERVAL_SECONDS,    # default 20s
}

INITIAL_DELAYS = {
    'fetch_master': 0,    # Products + Customers run immediately on startup
    'fetch_invoices': 0,  # DCs run immediately after master data is ready
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
        # Signalled after the very first product+customer fetch completes.
        # DC fetch waits on this so it only starts once master data is ready.
        self._master_initial_done = threading.Event()

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
            },
            'next_runs': {
                'fetch_master': None,
                'fetch_invoices': None,
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
                for task in ['fetch_master', 'fetch_invoices']:
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

        logger.info("Starting automation threads...")
        self._start_thread('fetch_master', self._fetch_master_loop)
        self._start_thread('fetch_invoices', self._fetch_invoices_loop)

        logger.info("Automation started — master fetch once/day, DC fetch+sync every 20s")
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
        """
        Once-per-day loop: fetch products + customers from Tally,
        then immediately sync any pending ones to the server.
        """
        from fetch_products import fetch_products_from_all_companies
        from fetch_customers import fetch_customers_from_all_companies
        from sync_customers import build_config as build_sync_customers_config, run_once as sync_customers_once
        from sync_products import build_config as build_sync_products_config, run_once as sync_products_once
        from types import SimpleNamespace

        task = 'fetch_master'
        consecutive_errors = 0
        _first_run = True

        while not self.stop_flags[task].is_set():
            try:
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                logger.info(f"Running {task}...")
                self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA STARTED ===")

                # --- Fetch products ---
                ok_prod = True
                prod_new = prod_updated = 0
                try:
                    result = fetch_products_from_all_companies()
                    prod_new = result.get('new_saved', 0)
                    prod_updated = result.get('updated', 0)
                except Exception as e:
                    logger.error(f"Product fetch failed: {e}", exc_info=True)
                    ok_prod = False

                if self.stop_flags[task].wait(timeout=10):
                    return

                # --- Fetch customers ---
                ok_cust = True
                cust_new = cust_updated = 0
                try:
                    result = fetch_customers_from_all_companies()
                    cust_new = result.get('new_saved', 0)
                    cust_updated = result.get('updated', 0)
                except Exception as e:
                    logger.error(f"Customer fetch failed: {e}", exc_info=True)
                    ok_cust = False

                self._log_to_dashboard(
                    f"=== AUTO: FETCH MASTER DATA COMPLETED "
                    f"(products: new={prod_new} upd={prod_updated} | "
                    f"customers: new={cust_new} upd={cust_updated}) ==="
                )

                with self.lock:
                    self.state['last_runs'][task] = datetime.now().isoformat()
                    self._save_state()

                consecutive_errors = 0

                if _first_run:
                    _first_run = False
                    self._master_initial_done.set()
                    logger.info(f"{task}: Initial master fetch done — DC fetch unblocked")

                # --- Sync pending products + customers immediately after fetch ---
                sync_args = SimpleNamespace(
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
                    log_json=cfg.get_env_bool("LOG_JSON", False),
                    log_file=None,
                )
                try:
                    sync_products_once(build_sync_products_config(sync_args))
                except Exception as e:
                    logger.error(f"Product sync failed: {e}")
                try:
                    sync_customers_once(build_sync_customers_config(sync_args))
                except Exception as e:
                    logger.error(f"Customer sync failed: {e}")

                # Wait full day before next master fetch
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                consecutive_errors += 1
                backoff = min(30 * consecutive_errors, 120)
                logger.error(f"Error in {task} (attempt {consecutive_errors}): {e}. Retrying in {backoff}s")
                self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA ERROR: {e} (retry in {backoff}s) ===")
                if _first_run:
                    _first_run = False
                    self._master_initial_done.set()
                    logger.warning(f"{task}: Master fetch failed on first run — DC fetch unblocked anyway")
                self.stop_flags[task].wait(timeout=backoff)

    def _fetch_invoices_loop(self):
        """
        Every-20s loop: fetch DCs from Tally, then immediately sync
        any pending DCs to the server (inline — no separate sync timer).
        """
        from fetch_invoices import build_config as build_fetch_invoices_config, run_once as fetch_invoices_once
        from sync_catalytics import build_config as build_sync_config, run_once as sync_once
        from types import SimpleNamespace

        task = 'fetch_invoices'
        consecutive_errors = 0

        logger.info(f"{task}: Waiting for initial master data fetch to complete...")
        self._master_initial_done.wait()
        if self.stop_flags[task].is_set():
            return

        logger.info(f"{task}: Master data ready — starting DC fetch+sync loop (every 20s)")

        while not self.stop_flags[task].is_set():
            try:
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # --- Fetch DCs from Tally ---
                fetch_args = SimpleNamespace(
                    config=cfg.resolve_env_path(ROOT_DIR),
                    db_path=cfg.get_env("TALLY_DB_PATH"),
                    tally_url=cfg.get_env("TALLY_URL"),
                    company=cfg.get_env("TALLY_COMPANY"),
                    entity_id=cfg.get_env_int("CATALYTICS_ENTITY_ID"),
                    from_date=cfg.get_env("TALLY_FROM_DATE"),
                    to_date=cfg.get_env("TALLY_TO_DATE"),
                    days_back=cfg.get_env_int("TALLY_DAYS_BACK"),
                    fetch_stock=False,
                    dry_run=False,
                    log_level=cfg.get_env("LOG_LEVEL", "INFO"),
                    log_json=cfg.get_env_bool("LOG_JSON", False),
                    log_file=None,
                )
                fetch_ok = True
                fetch_created = fetch_updated = 0
                try:
                    result = fetch_invoices_once(build_fetch_invoices_config(fetch_args))
                    fetch_created = result.get('created', 0)
                    fetch_updated = result.get('updated', 0)
                    if fetch_created or fetch_updated:
                        self._log_to_dashboard(
                            f"=== AUTO: FETCH DCs COMPLETED (created={fetch_created}, updated={fetch_updated}) ==="
                        )
                except Exception as e:
                    logger.error(f"DC fetch failed: {e}", exc_info=True)
                    fetch_ok = False
                    self._log_to_dashboard(f"=== AUTO: FETCH DCs FAILED: {e} ===")

                # --- Sync pending DCs immediately after fetch ---
                sync_args = SimpleNamespace(
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
                    log_file=None,
                )
                try:
                    sync_once(build_sync_config(sync_args))
                except Exception as e:
                    logger.error(f"DC sync failed: {e}")

                with self.lock:
                    self.state['last_runs'][task] = datetime.now().isoformat()
                    self._save_state()

                consecutive_errors = 0
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                consecutive_errors += 1
                backoff = min(30 * consecutive_errors, 120)
                logger.error(f"Error in {task} (attempt {consecutive_errors}): {e}. Retrying in {backoff}s")
                self._log_to_dashboard(f"=== AUTO: FETCH DCs ERROR: {e} (retry in {backoff}s) ===")
                self.stop_flags[task].wait(timeout=backoff)


# Global automation manager instance
_manager = None

def get_manager():
    """Get or create the global automation manager instance"""
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager
