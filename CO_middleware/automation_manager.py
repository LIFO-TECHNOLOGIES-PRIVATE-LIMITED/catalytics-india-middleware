"""
Automation manager for CO middleware.

Two automation threads (BOL pattern):
- fetch_master: daily master sync — fetch + sync customers & products once per day
  at MASTER_SYNC_TIME (default 02:00), with retry on failure.
- sync: combined invoice fetch + sync — fetch DCs from Tally then sync to Catalytics,
  every FETCH_SYNC_INTERVAL_SECONDS (default 20s).

Uses a global operation lock (non-blocking skip) instead of a blocking run_lock,
so threads never stall waiting for each other.
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


def _reload_env_config():
    """Reload .env so automation uses the latest file-backed config."""
    cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))
    config.reload_from_env()


def _default_intervals():
    """Return default task intervals from the current environment."""
    _reload_env_config()
    fetch_sync_interval = max(1, cfg.get_env_int("FETCH_SYNC_INTERVAL_SECONDS", 10))
    return {
        'fetch_master': max(1, config.FETCH_MASTER_INTERVAL_MINUTES * 60),
        'fetch_invoices': fetch_sync_interval,
        'sync': fetch_sync_interval,
    }


def _empty_task_map():
    return {task: None for task in TASKS}


def _coerce_positive_int(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _parse_sync_time(raw):
    """Parse HH:MM string into (hour, minute). Returns (2, 0) on failure."""
    try:
        parts = str(raw).strip().split(':')
        return int(parts[0]), int(parts[1])
    except Exception:
        return 10, 0


class AutomationManager:
    """Manages automated fetch and sync operations for the dashboard."""

    def __init__(self):
        self.threads = {}
        self.stop_flags = {}
        self.lock = threading.Lock()
        # Non-blocking global lock — if held, other task skips instead of waiting.
        self._global_op_lock = threading.Lock()
        # Per-task locks to prevent overlapping runs of the same task.
        self._running_tasks = {task: threading.Lock() for task in TASKS}
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
            logger.error("Error loading state: %s", e)
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
            logger.error("Error saving state: %s", e)

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
                        try:
                            dt = datetime.fromisoformat(next_run)
                            status[f'{task}_countdown'] = int(max(0, (dt - now).total_seconds()))
                        except ValueError:
                            status[f'{task}_countdown'] = 0
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

        logger.info("Intervals updated: %s", intervals)
        return True

    def start(self):
        """Start automation loops."""
        _reload_env_config()
        with self.lock:
            if self.state['status'] == 'running':
                logger.warning("Automation already running")
                return False

            self.state['intervals'] = _default_intervals()
            self.state['status'] = 'running'
            self.state['started_at'] = datetime.now().isoformat()
            self.state['stopped_at'] = None
            for task in TASKS:
                self.state['next_runs'][task] = datetime.now().isoformat()
            self._save_state()

        # Thread 1: Daily master sync — fetch + sync customers & products once per day
        self._start_thread('fetch_master', self._daily_master_sync_loop)
        # Thread 2: Combined invoice fetch + sync — every FETCH_SYNC_INTERVAL_SECONDS
        self._start_thread('sync', self._invoice_fetch_sync_loop)

        logger.info("Automation started — daily master sync + invoice fetch+sync every %ss",
                     self.state['intervals']['sync'])
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

        logger.info("Automation stopped")
        return True

    def restart(self):
        """Restart automation loops."""
        logger.info("Restarting automation...")
        self.stop()
        time.sleep(2)
        return self.start()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_thread(self, task_name, target_func):
        self.stop_flags[task_name] = threading.Event()
        thread = threading.Thread(target=target_func, daemon=True, name=f'automation-{task_name}')
        thread.start()
        self.threads[task_name] = thread

    def _is_stopped(self, task):
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

    def _mark_task_complete(self, task):
        with self.lock:
            self.state['last_runs'][task] = datetime.now().isoformat()
            self._save_state()

    def _try_acquire_global(self, label):
        """Non-blocking: acquire global lock or skip."""
        if not self._global_op_lock.acquire(blocking=False):
            logger.info("[SKIP] %s blocked by another task running", label)
            self._log_to_dashboard(f"=== AUTO: {label} SKIPPED (another task running) ===")
            return False
        return True

    def _release_global(self):
        try:
            self._global_op_lock.release()
        except RuntimeError:
            pass

    def _check_tally_connected(self) -> bool:
        """Check if Tally is reachable."""
        try:
            import requests as _req
            tally_url = cfg.get_env("TALLY_URL", "http://localhost:9000/")
            resp = _req.get(tally_url, timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def _wait_for_tally(self, task):
        """Poll until Tally is reachable. Returns False if stopped."""
        logger.info("%s: Waiting for Tally connection...", task)
        self._log_to_dashboard(f"=== AUTO: {task} WAITING FOR TALLY CONNECTION ===")
        while not self._is_stopped(task):
            if self._check_tally_connected():
                logger.info("%s: Tally connected", task)
                self._log_to_dashboard(f"=== AUTO: TALLY CONNECTED — {task} starting ===")
                return True
            self._wait_stop(task, timeout=5)
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

    def _build_fetch_args(self):
        return SimpleNamespace(
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

    # ------------------------------------------------------------------
    # Thread 1: Daily master sync (BOL pattern)
    # ------------------------------------------------------------------

    def _daily_master_sync_loop(self):
        """
        Daily master data sync:
        1. Runs ONCE immediately on startup
        2. Then once per day at MASTER_SYNC_TIME (default 02:00)
        3. On failure, retries after MASTER_SYNC_FAILURE_RETRY_MINUTES (default 30)

        Each run: fetch customers + products from Tally, then sync both to Catalytics.
        """
        from fetch_customers import fetch_customers_from_all_companies
        from fetch_products import fetch_products_from_all_companies
        from sync_catalytics import build_config as build_dc_sync_config, run_once as sync_dc_once
        from sync_customers import build_config as build_customer_sync_config, run_once as sync_customers_once
        from sync_products import build_config as build_product_sync_config, run_once as sync_products_once

        task = 'fetch_master'

        # Wait for Tally
        if not self._wait_for_tally(task):
            return

        sync_time_str = cfg.get_env("MASTER_SYNC_TIME", "10:00")
        sync_hour, sync_minute = _parse_sync_time(sync_time_str)
        retry_minutes = cfg.get_env_int("MASTER_SYNC_FAILURE_RETRY_MINUTES", 30)

        first_run = True
        last_run_success = True

        while not self._is_stopped(task):
            try:
                now = datetime.now()

                if first_run:
                    logger.info("[DAILY MASTER] First run — starting immediately")
                    first_run = False
                else:
                    # Schedule next run
                    if last_run_success:
                        next_run = now.replace(hour=sync_hour, minute=sync_minute, second=0, microsecond=0)
                        if next_run <= now:
                            next_run += timedelta(days=1)
                        wait_seconds = (next_run - now).total_seconds()
                        wait_desc = "%dh %dm" % (int(wait_seconds // 3600), int((wait_seconds % 3600) // 60))
                        logger.info("[DAILY MASTER] Next run at %s (%s)",
                                    next_run.strftime('%Y-%m-%d %H:%M'), wait_desc)
                    else:
                        next_run = now + timedelta(minutes=retry_minutes)
                        wait_seconds = (next_run - now).total_seconds()
                        logger.warning("[DAILY MASTER] Previous run failed — retrying at %s (in %dm)",
                                       next_run.strftime('%Y-%m-%d %H:%M'), retry_minutes)

                    with self.lock:
                        self.state['next_runs'][task] = next_run.isoformat()
                        self._save_state()

                    # Wait until scheduled time (check stop flag every 60s)
                    while wait_seconds > 0 and not self._is_stopped(task):
                        chunk = min(wait_seconds, 60)
                        self._wait_stop(task, timeout=chunk)
                        wait_seconds -= chunk

                    if self._is_stopped(task):
                        return

                # Acquire per-task lock (non-blocking skip if already running)
                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info("[SKIP] %s still running from previous cycle", task)
                    continue

                try:
                    if not self._try_acquire_global('daily_master'):
                        continue

                    _reload_env_config()
                    self._log_to_dashboard("=== DAILY MASTER SYNC STARTED ===")
                    logger.info("[DAILY MASTER] === FETCH + SYNC ALL MASTER DATA ===")
                    success = True

                    # Step 1: Fetch customers from Tally
                    cust_fetch = {'new_saved': 0, 'updated': 0}
                    if cfg.get_env_bool("AUTO_FETCH_CUSTOMERS", False):
                        try:
                            cust_fetch = fetch_customers_from_all_companies()
                            logger.info("[DAILY MASTER] Customers fetched: new=%d, updated=%d",
                                        cust_fetch.get('new_saved', 0), cust_fetch.get('updated', 0))
                        except Exception as e:
                            success = False
                            logger.error("[DAILY MASTER] Customer fetch failed: %s", e, exc_info=True)
                    else:
                        logger.info("[DAILY MASTER] Customer auto-fetch disabled (AUTO_FETCH_CUSTOMERS=false)")

                    # Step 2: Fetch products from Tally
                    prod_fetch = {'new_saved': 0, 'updated': 0}
                    if cfg.get_env_bool("AUTO_FETCH_PRODUCTS", False):
                        try:
                            prod_fetch = fetch_products_from_all_companies()
                            logger.info("[DAILY MASTER] Products fetched: new=%d, updated=%d",
                                        prod_fetch.get('new_saved', 0), prod_fetch.get('updated', 0))
                        except Exception as e:
                            success = False
                            logger.error("[DAILY MASTER] Product fetch failed: %s", e, exc_info=True)
                    else:
                        logger.info("[DAILY MASTER] Product auto-fetch disabled (AUTO_FETCH_PRODUCTS=false)")

                    # Step 3: Sync customers to Catalytics
                    cust_sync = {'ok': 0, 'failed': 0}
                    try:
                        master_args = self._build_master_sync_args()
                        cust_sync = sync_customers_once(build_customer_sync_config(master_args))
                        logger.info("[DAILY MASTER] Customers synced: ok=%d, failed=%d",
                                    cust_sync.get('ok', 0), cust_sync.get('failed', 0))
                    except Exception as e:
                        success = False
                        logger.error("[DAILY MASTER] Customer sync failed: %s", e, exc_info=True)

                    # Step 4: Sync products to Catalytics
                    prod_sync = {'ok': 0, 'failed': 0}
                    try:
                        master_args = self._build_master_sync_args()
                        prod_sync = sync_products_once(build_product_sync_config(master_args))
                        logger.info("[DAILY MASTER] Products synced: ok=%d, failed=%d",
                                    prod_sync.get('ok', 0), prod_sync.get('failed', 0))
                    except Exception as e:
                        success = False
                        logger.error("[DAILY MASTER] Product sync failed: %s", e, exc_info=True)

                    self._log_to_dashboard(
                        "=== DAILY MASTER SYNC COMPLETED "
                        f"(cust fetch={cust_fetch.get('new_saved', 0)}+{cust_fetch.get('updated', 0)} "
                        f"sync={cust_sync.get('ok', 0)} | "
                        f"prod fetch={prod_fetch.get('new_saved', 0)}+{prod_fetch.get('updated', 0)} "
                        f"sync={prod_sync.get('ok', 0)}) ==="
                    )

                    self._mark_task_complete(task)
                    last_run_success = success
                    logger.info("[DAILY MASTER] Complete (success=%s)", success)

                finally:
                    self._release_global()
                    self._running_tasks[task].release()

            except Exception as e:
                last_run_success = False
                logger.error("[DAILY MASTER] Error: %s", e, exc_info=True)
                self._log_to_dashboard(f"=== DAILY MASTER SYNC ERROR: {e} ===")
                self._wait_stop(task, timeout=60)

    # ------------------------------------------------------------------
    # Thread 2: Combined invoice fetch + sync (BOL pattern)
    # ------------------------------------------------------------------

    def _invoice_fetch_sync_loop(self):
        """
        Combined invoice fetch + sync loop:
        1. Fetch DCs from Tally Day Book (auto-creates missing customers/products)
        2. Sync unsynced DCs to Catalytics (batch)
        Runs every FETCH_SYNC_INTERVAL_SECONDS (default 20s).
        No lock competition — fetch and sync are sequential in the same thread.
        """
        from fetch_invoices import build_config as build_fetch_config, run_once as fetch_once
        from sync_catalytics import build_config as build_dc_sync_config, run_once as sync_dc_once

        task = 'sync'

        # Wait for Tally
        if not self._wait_for_tally(task):
            return

        interval = max(1, cfg.get_env_int("FETCH_SYNC_INTERVAL_SECONDS", 10))

        while not self._is_stopped(task):
            try:
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self.state['next_runs']['fetch_invoices'] = next_run.isoformat()
                    self._save_state()

                # Non-blocking: skip if same task is still running from previous cycle
                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info("[SKIP] invoice fetch+sync still running from previous cycle")
                    self._wait_stop(task, timeout=interval)
                    continue

                try:
                    if not self._try_acquire_global('invoice_fetch_sync'):
                        self._wait_stop(task, timeout=interval)
                        continue

                    _reload_env_config()
                    self._log_to_dashboard("=== AUTO: INVOICE FETCH + SYNC STARTED ===")

                    # Step 1: Fetch DCs from Tally
                    fetch_created = fetch_updated = 0
                    try:
                        fetch_args = self._build_fetch_args()
                        fetch_result = fetch_once(build_fetch_config(fetch_args))
                        fetch_created = fetch_result.get('created', 0)
                        fetch_updated = fetch_result.get('updated', 0)
                    except Exception as e:
                        logger.error("DC fetch failed: %s", e, exc_info=True)
                        self._log_to_dashboard(f"=== AUTO: DC FETCH FAILED: {e} ===")

                    # Step 2: Sync unsynced DCs to Catalytics
                    dc_stats = {'ok': 0, 'failed': 0}
                    try:
                        dc_sync_args = self._build_dc_sync_args()
                        dc_stats = sync_dc_once(build_dc_sync_config(dc_sync_args))
                    except Exception as e:
                        logger.error("DC sync failed: %s", e, exc_info=True)
                        self._log_to_dashboard(f"=== AUTO: DC SYNC FAILED: {e} ===")

                    self._log_to_dashboard(
                        f"=== AUTO: FETCH+SYNC COMPLETED "
                        f"(fetched: new={fetch_created} upd={fetch_updated} | "
                        f"synced: ok={dc_stats.get('ok', 0)} failed={dc_stats.get('failed', 0)}) ==="
                    )

                    self._mark_task_complete(task)
                    with self.lock:
                        self.state['last_runs']['fetch_invoices'] = datetime.now().isoformat()
                        self._save_state()

                finally:
                    self._release_global()
                    self._running_tasks[task].release()

                # Wait until next cycle
                self._wait_stop(task, timeout=interval)

            except Exception as e:
                logger.error("Error in invoice fetch+sync: %s", e, exc_info=True)
                self._log_to_dashboard(f"=== AUTO: INVOICE FETCH+SYNC ERROR: {e} ===")
                self._wait_stop(task, timeout=10)


_manager = None


def get_manager():
    """Get or create the global automation manager instance."""
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager
