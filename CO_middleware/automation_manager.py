"""
Automation manager for CO middleware.

The dashboard uses three automation timers:
- fetch_master: fetch products + customers from Tally
- fetch_invoices: fetch delivery challans from Tally
- sync: sync pending products, customers, and delivery challans to Catalytics

Intervals default from .env when automation starts. Dashboard interval changes
are runtime-only and are reset the next time automation is started.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg
from config import config


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

try:
    from log_capture import dashboard_logger
except ImportError:
    dashboard_logger = None


STATE_FILE = Path(cfg.BASE_DIR) / 'automation_state.json'
TASKS = ('fetch_master', 'fetch_invoices', 'sync')
INITIAL_DELAYS = {task: 0 for task in TASKS}


def _reload_env_config():
    """Reload .env so automation uses the latest file-backed config."""
    cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))
    config.reload_from_env()


def _default_intervals():
    """Return default task intervals from the current environment."""
    _reload_env_config()
    return {
        'fetch_master': max(1, config.FETCH_MASTER_INTERVAL_MINUTES * 60),
        'fetch_invoices': max(1, config.FETCH_INVOICES_INTERVAL_SECONDS),
        'sync': max(1, config.SYNC_INVOICES_INTERVAL_SECONDS),
    }


def _empty_task_map():
    return {task: None for task in TASKS}


def _coerce_positive_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class AutomationManager:
    """Manages automated fetch and sync operations for the dashboard."""

    def __init__(self):
        self.threads = {}
        self.stop_flags = {}
        self.lock = threading.Lock()
        self._master_initial_done = threading.Event()
        self.state = self._load_state()

        if self.state['status'] == 'running':
            logger.info("Resetting stale 'running' state to 'stopped' (process restarted)")
            self.state['status'] = 'stopped'
            self.state['next_runs'] = _empty_task_map()
            self._save_state()

    def _new_state(self):
        return {
            'status': 'stopped',
            'intervals': _default_intervals(),
            'last_runs': _empty_task_map(),
            'next_runs': _empty_task_map(),
            'started_at': None,
            'stopped_at': None,
        }

    def _load_state(self):
        """Load automation state from disk, but keep env as the interval source."""
        state = self._new_state()
        if not STATE_FILE.exists():
            return state

        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
        except Exception as e:
            logger.error(f"Error loading state: {e}")
            return state

        if not isinstance(loaded, dict):
            return state

        state['status'] = loaded.get('status', state['status'])
        state['started_at'] = loaded.get('started_at')
        state['stopped_at'] = loaded.get('stopped_at')

        for section in ('last_runs', 'next_runs'):
            values = loaded.get(section)
            if isinstance(values, dict):
                for task in TASKS:
                    state[section][task] = values.get(task)

        return state

    def _save_state(self):
        """Save automation state to disk."""
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def get_status(self):
        """Get current automation status."""
        with self.lock:
            status = {
                'status': self.state['status'],
                'intervals': dict(self.state['intervals']),
                'last_runs': dict(self.state['last_runs']),
                'next_runs': dict(self.state['next_runs']),
                'started_at': self.state['started_at'],
                'stopped_at': self.state['stopped_at'],
            }

            if self.state['status'] == 'running':
                now = datetime.now()
                for task in TASKS:
                    next_run = self.state['next_runs'].get(task)
                    if next_run:
                        dt = datetime.fromisoformat(next_run)
                        status[f'{task}_countdown'] = int(max(0, (dt - now).total_seconds()))
                    else:
                        status[f'{task}_countdown'] = 0

            return status

    def set_intervals(self, intervals):
        """Update live automation intervals."""
        now = datetime.now()
        with self.lock:
            for task, interval in (intervals or {}).items():
                interval = _coerce_positive_int(interval)
                if task not in self.state['intervals'] or interval is None:
                    continue
                self.state['intervals'][task] = interval
                if self.state['status'] == 'running':
                    self.state['next_runs'][task] = (now + timedelta(seconds=interval)).isoformat()
            self._save_state()

        logger.info(f"Intervals updated: {intervals}")
        return True

    def start(self):
        """Start automation loops."""
        _reload_env_config()
        with self.lock:
            if self.state['status'] == 'running':
                logger.warning("Automation already running")
                return False

            env_defaults = _default_intervals()
            self.state['intervals'] = dict(env_defaults)


            self.state['status'] = 'running'
            self.state['started_at'] = datetime.now().isoformat()
            self.state['stopped_at'] = None
            for task, delay in INITIAL_DELAYS.items():
                self.state['next_runs'][task] = (datetime.now() + timedelta(seconds=delay)).isoformat()
            self._save_state()

        # DC fetch and sync start immediately — no need to wait for master fetch.
        # DC-driven creation handles missing customers/products in real-time.
        # Master fetch runs daily as a background full refresh.
        self._master_initial_done.set()
        logger.info("Starting automation threads...")
        self._start_thread('fetch_master', self._fetch_master_loop)
        self._start_thread('fetch_invoices', self._fetch_invoices_loop)
        self._start_thread('sync', self._sync_loop)
        logger.info(
            "Automation started - master fetch every %ss, DC fetch every %ss, sync every %ss",
            self.state['intervals']['fetch_master'],
            self.state['intervals']['fetch_invoices'],
            self.state['intervals']['sync'],
        )
        return True

    def stop(self):
        """Stop automation loops."""
        with self.lock:
            if self.state['status'] != 'running':
                logger.warning("Automation not running")
                return False

            self.state['status'] = 'stopped'
            self.state['stopped_at'] = datetime.now().isoformat()
            self.state['next_runs'] = _empty_task_map()
            self._save_state()

        for flag in list(self.stop_flags.values()):
            flag.set()

        for task, thread in list(self.threads.items()):
            thread.join(timeout=5)

        self.threads.clear()
        self.stop_flags.clear()
        self._master_initial_done.clear()

        logger.info("Automation stopped")
        return True

    def restart(self):
        """Restart automation loops."""
        logger.info("Restarting automation...")
        self.stop()
        time.sleep(2)
        return self.start()

    def _start_thread(self, task_name, target_func):
        self.stop_flags[task_name] = threading.Event()
        thread = threading.Thread(target=target_func, daemon=True, name=f'automation-{task_name}')
        thread.start()
        self.threads[task_name] = thread

    def _is_stopped(self, task):
        """Check if a task has been stopped (flag missing or set)."""
        flag = self.stop_flags.get(task)
        return flag is None or flag.is_set()

    def _wait_stop(self, task, timeout):
        """Wait on a task's stop flag. Returns True if stopped."""
        flag = self.stop_flags.get(task)
        if flag is None:
            return True
        return flag.wait(timeout=timeout)

    def _log_to_dashboard(self, message):
        if dashboard_logger:
            dashboard_logger.write_log(message)

    def _prepare_task_run(self, task):
        _reload_env_config()
        with self.lock:
            interval = int(self.state['intervals'][task])
            self.state['next_runs'][task] = (datetime.now() + timedelta(seconds=interval)).isoformat()
            self._save_state()
        return interval

    def _mark_task_complete(self, task):
        with self.lock:
            self.state['last_runs'][task] = datetime.now().isoformat()
            self._save_state()

    def _wait_for_initial_master(self, task):
        logger.info("%s: Waiting for initial master data fetch to complete...", task)
        while not self._master_initial_done.is_set():
            if self._wait_stop(task, timeout=1.0):
                return False
        return True

    def _wait_for_next_run(self, task):
        while not self._is_stopped(task):
            with self.lock:
                next_run_iso = self.state['next_runs'].get(task)

            if not next_run_iso:
                return False

            try:
                next_run = datetime.fromisoformat(next_run_iso)
            except ValueError:
                return True

            remaining = (next_run - datetime.now()).total_seconds()
            if remaining <= 0:
                return True

            self._wait_stop(task, timeout=min(1.0, remaining))

        return False

    def _build_master_sync_args(self):
        return SimpleNamespace(
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

    def _build_dc_sync_args(self):
        return SimpleNamespace(
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

    def _check_tally_connected(self) -> bool:
        """Check if Tally is reachable."""
        try:
            import requests as _req
            tally_url = cfg.get_env("TALLY_URL", "http://localhost:9000/")
            resp = _req.get(tally_url, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def _fetch_master_loop(self):
        """Fetch products + customers from Tally daily.
        On first run, waits for Tally connection before starting."""
        from fetch_customers import fetch_customers_from_all_companies
        from fetch_products import fetch_products_from_all_companies

        task = 'fetch_master'
        consecutive_errors = 0
        first_run = True

        # Wait for Tally to be connected before first master fetch
        if first_run:
            logger.info("%s: Waiting for Tally connection...", task)
            self._log_to_dashboard("=== AUTO: MASTER FETCH WAITING FOR TALLY CONNECTION ===")
            while not self._is_stopped(task):
                if self._check_tally_connected():
                    logger.info("%s: Tally connected - starting master fetch", task)
                    self._log_to_dashboard("=== AUTO: TALLY CONNECTED - STARTING MASTER FETCH ===")
                    break
                self._wait_stop(task, timeout=5)
            if self._is_stopped(task):
                return

        while not self._is_stopped(task):
            try:
                self._prepare_task_run(task)
                logger.info("Running %s...", task)
                self._log_to_dashboard("=== AUTO: FETCH MASTER DATA STARTED ===")

                prod_new = prod_updated = 0
                cust_new = cust_updated = 0

                if cfg.get_env_bool("AUTO_FETCH_PRODUCTS", False):
                    try:
                        result = fetch_products_from_all_companies()
                        prod_new = result.get('new_saved', 0)
                        prod_updated = result.get('updated', 0)
                    except Exception as e:
                        logger.error(f"Product fetch failed: {e}", exc_info=True)
                else:
                    logger.info("Product auto-fetch disabled (AUTO_FETCH_PRODUCTS=false)")

                if cfg.get_env_bool("AUTO_FETCH_CUSTOMERS", False):
                    try:
                        result = fetch_customers_from_all_companies()
                        cust_new = result.get('new_saved', 0)
                        cust_updated = result.get('updated', 0)
                    except Exception as e:
                        logger.error(f"Customer fetch failed: {e}", exc_info=True)
                else:
                    logger.info("Customer auto-fetch disabled (AUTO_FETCH_CUSTOMERS=false)")

                self._log_to_dashboard(
                    f"=== AUTO: FETCH MASTER DATA COMPLETED "
                    f"(products: new={prod_new} upd={prod_updated} | "
                    f"customers: new={cust_new} upd={cust_updated}) ==="
                )

                self._mark_task_complete(task)
                consecutive_errors = 0

                if first_run:
                    first_run = False
                    self._master_initial_done.set()
                    logger.info("%s: Initial master fetch done - DC fetch and sync unblocked", task)

                if not self._wait_for_next_run(task):
                    return

            except Exception as e:
                consecutive_errors += 1
                backoff = min(30 * consecutive_errors, 120)
                logger.error(f"Error in {task} (attempt {consecutive_errors}): {e}. Retrying in {backoff}s")
                self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA ERROR: {e} (retry in {backoff}s) ===")
                if first_run:
                    first_run = False
                    self._master_initial_done.set()
                    logger.warning("%s: Master fetch failed on first run - DC fetch and sync unblocked anyway", task)
                if self._wait_stop(task, timeout=backoff):
                    return

    def _fetch_invoices_loop(self):
        """Fetch delivery challans from Tally on the invoice schedule."""
        from fetch_invoices import build_config as build_fetch_invoices_config, run_once as fetch_invoices_once

        task = 'fetch_invoices'
        consecutive_errors = 0

        # Wait for Tally connection before starting DC fetch
        logger.info("%s: Waiting for Tally connection...", task)
        while not self._is_stopped(task):
            if self._check_tally_connected():
                logger.info("%s: Tally connected - starting DC fetch loop", task)
                break
            self._wait_stop(task, timeout=5)
        if self._is_stopped(task):
            return

        while not self._is_stopped(task):
            try:
                self._prepare_task_run(task)

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
                    self._log_to_dashboard(f"=== AUTO: FETCH DCs FAILED: {e} ===")

                self._mark_task_complete(task)
                consecutive_errors = 0

                if not self._wait_for_next_run(task):
                    return

            except Exception as e:
                consecutive_errors += 1
                backoff = min(30 * consecutive_errors, 120)
                logger.error(f"Error in {task} (attempt {consecutive_errors}): {e}. Retrying in {backoff}s")
                self._log_to_dashboard(f"=== AUTO: FETCH DCs ERROR: {e} (retry in {backoff}s) ===")
                if self._wait_stop(task, timeout=backoff):
                    return

    def _sync_loop(self):
        """Sync products, customers, and delivery challans on the sync schedule."""
        from sync_catalytics import build_config as build_dc_sync_config, run_once as sync_dc_once
        from sync_customers import build_config as build_customer_sync_config, run_once as sync_customers_once
        from sync_products import build_config as build_product_sync_config, run_once as sync_products_once

        task = 'sync'
        consecutive_errors = 0

        if not self._wait_for_initial_master(task):
            return

        logger.info("%s: Master data ready - starting sync loop", task)

        while not self._is_stopped(task):
            try:
                self._prepare_task_run(task)
                logger.info("Running %s...", task)
                self._log_to_dashboard("=== AUTO: SYNC STARTED ===")

                product_stats = {'sent': 0, 'ok': 0, 'failed': 0}
                customer_stats = {'sent': 0, 'ok': 0, 'failed': 0}
                dc_stats = {'sent': 0, 'ok': 0, 'failed': 0}

                master_sync_args = self._build_master_sync_args()
                try:
                    product_stats = sync_products_once(build_product_sync_config(master_sync_args))
                except Exception as e:
                    logger.error(f"Product sync failed: {e}", exc_info=True)

                try:
                    customer_stats = sync_customers_once(build_customer_sync_config(master_sync_args))
                except Exception as e:
                    logger.error(f"Customer sync failed: {e}", exc_info=True)

                dc_sync_args = self._build_dc_sync_args()
                try:
                    dc_stats = sync_dc_once(build_dc_sync_config(dc_sync_args))
                except Exception as e:
                    logger.error(f"DC sync failed: {e}", exc_info=True)

                self._log_to_dashboard(
                    "=== AUTO: SYNC COMPLETED "
                    f"(products ok={product_stats.get('ok', 0)} failed={product_stats.get('failed', 0)} | "
                    f"customers ok={customer_stats.get('ok', 0)} failed={customer_stats.get('failed', 0)} | "
                    f"dcs ok={dc_stats.get('ok', 0)} failed={dc_stats.get('failed', 0)}) ==="
                )

                self._mark_task_complete(task)
                consecutive_errors = 0

                if not self._wait_for_next_run(task):
                    return

            except Exception as e:
                consecutive_errors += 1
                backoff = min(30 * consecutive_errors, 120)
                logger.error(f"Error in {task} (attempt {consecutive_errors}): {e}. Retrying in {backoff}s")
                self._log_to_dashboard(f"=== AUTO: SYNC ERROR: {e} (retry in {backoff}s) ===")
                if self._wait_stop(task, timeout=backoff):
                    return


_manager = None


def get_manager():
    """Get or create the global automation manager instance."""
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager

