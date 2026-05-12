"""
Automation Manager for BOL Middleware
Manages automated fetch and sync operations with configurable intervals
"""

import json
import time
import threading
import io
from datetime import datetime, timedelta
from pathlib import Path
import logging
from config import config, BASE_DIR

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
STATE_FILE = BASE_DIR / 'automation_state.json'


class DashboardLogHandler(logging.Handler):
    """Routes logging output to the dashboard terminal and a StringIO buffer."""

    def __init__(self, string_buffer=None):
        super().__init__()
        self.buffer = string_buffer  # optional StringIO for capturing output

    def emit(self, record):
        msg = self.format(record)
        if dashboard_logger:
            dashboard_logger.write_raw(msg)
        if self.buffer:
            self.buffer.write(msg + '\n')

# Default intervals (in seconds)
import os as _os
_fetch_sync_interval = max(1, int(_os.getenv('FETCH_SYNC_INTERVAL_SECONDS', '10')))
DEFAULT_INTERVALS = {
    'fetch_master': 86400,  # daily (controlled by MASTER_SYNC_TIME)
    'fetch_invoices': _fetch_sync_interval,
    'sync': _fetch_sync_interval,
    'sync_master': 86400,  # daily (controlled by MASTER_SYNC_TIME)
}


class AutomationManager:
    """Manages automated fetch and sync operations"""

    def __init__(self):
        self.state = self._load_state()
        self.threads = {}
        self.stop_flags = {}
        self.lock = threading.Lock()
        # Prevent overlapping runs across tasks
        self._global_op_lock = threading.Lock()
        # Prevent overlapping runs of the same task
        self._running_tasks = {
            'fetch_master': threading.Lock(),
            'fetch_invoices': threading.Lock(),
            'sync': threading.Lock(),
            'sync_master': threading.Lock(),
        }

        # Reset status on startup — threads don't survive process restart
        if self.state['status'] == 'running':
            logger.info("Resetting stale 'running' state to 'stopped' (process restarted)")
            self.state['status'] = 'stopped'
            self._save_state()

        # Log invoice fetch configuration
        if config.INVOICE_FETCH_START_DATE:
            logger.info(f"Invoice fetch configured from: {config.INVOICE_FETCH_START_DATE}")
        else:
            logger.info("Invoice fetch will use Day Book range (Yesterday to Tomorrow)")

    def _load_state(self):
        """Load automation state from file"""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    
                    # Merge missing intervals/runs from defaults (handles software updates)
                    for key in ['intervals', 'last_runs', 'next_runs']:
                        if key not in state:
                            state[key] = {}
                        
                        default_val = getattr(self, '_get_default_state', lambda: {})().get(key, {})
                        if key == 'intervals':
                            default_val = DEFAULT_INTERVALS
                        
                        for task, val in default_val.items():
                            if task not in state[key]:
                                state[key][task] = val
                    return state
            except Exception as e:
                logger.error(f"Error loading state: {e}")

        # Default state
        return {
            'status': 'stopped',  # stopped, running, paused
            'intervals': DEFAULT_INTERVALS.copy(),
            'last_runs': {
                'fetch_master': None,
                'fetch_invoices': None,
                'sync': None,
                'sync_master': None
            },
            'next_runs': {
                'fetch_master': None,
                'fetch_invoices': None,
                'sync': None,
                'sync_master': None
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
                for task in ['fetch_master', 'fetch_invoices', 'sync', 'sync_master']:
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
        """Start automation loops"""
        with self.lock:
            if self.state['status'] == 'running':
                logger.warning("Automation already running")
                return False

            self.state['status'] = 'running'
            self.state['started_at'] = datetime.now().isoformat()
            self._save_state()

        # Start automation threads
        # Thread 1: Daily master sync — fetch + sync customers & products once per day
        self._start_thread('fetch_master', self._daily_master_sync_loop)
        # Thread 2: Invoice fetch + sync — every 20 seconds
        self._start_thread('sync', self._invoice_fetch_sync_loop)

        logger.info("Automation started")
        return True

    def stop(self):
        """Stop automation loops"""
        with self.lock:
            if self.state['status'] != 'running':
                logger.warning("Automation not running")
                return False

            self.state['status'] = 'stopped'
            self.state['stopped_at'] = datetime.now().isoformat()
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

    def _try_acquire_global(self, task):
        if not self._global_op_lock.acquire(blocking=False):
            logger.info(f"[SKIP] {task} blocked by another task running")
            self._log_to_dashboard(f"=== AUTO: {task} SKIPPED (another task running) ===")
            return False
        return True

    def _release_global(self):
        try:
            self._global_op_lock.release()
        except RuntimeError:
            pass

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

        # Attach handler to root logger so all modules' output is captured
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

    def _daily_master_sync_loop(self):
        """
        Daily master data sync (CO_middleware pattern):
        Runs ONCE per day at MASTER_SYNC_TIME (default 02:00).
        1. Fetch ALL customers from Tally → save to SQLite
        2. Fetch ALL products from Tally → save to SQLite
        3. Sync ALL customers to Catalytics (batch)
        4. Sync ALL products to Catalytics (batch)
        """
        from fetch_customers import fetch_customers_from_all_companies
        from fetch_products import fetch_products_from_all_companies
        from sync_to_catalytics import CatalyticsSyncer

        task = 'fetch_master'
        syncer = CatalyticsSyncer()

        # Parse daily sync time from .env (default 02:00)
        sync_time_str = getattr(config, 'MASTER_SYNC_TIME', None) or '02:00'
        try:
            sync_hour, sync_minute = [int(x) for x in sync_time_str.split(':')]
        except Exception:
            sync_hour, sync_minute = 2, 0

        # Run once immediately on first start, then daily (with retry on failure)
        retry_minutes = getattr(config, 'MASTER_SYNC_FAILURE_RETRY_MINUTES', 30)
        first_run = True
        last_run_success = True

        while not self.stop_flags[task].is_set():
            try:
                now = datetime.now()

                if first_run:
                    # Run immediately on startup
                    first_run = False
                    logger.info(f"[DAILY MASTER] First run — starting immediately")
                else:
                    if last_run_success:
                        next_run = now.replace(hour=sync_hour, minute=sync_minute, second=0, microsecond=0)
                        if next_run <= now:
                            next_run += timedelta(days=1)
                        wait_seconds = (next_run - now).total_seconds()
                        wait_desc = f"{int(wait_seconds // 3600)}h {int((wait_seconds % 3600) // 60)}m"
                        logger.info(
                            f"[DAILY MASTER] Next run at {next_run.strftime('%Y-%m-%d %H:%M')} "
                            f"({wait_desc})"
                        )
                    else:
                        next_run = now + timedelta(minutes=retry_minutes)
                        wait_seconds = (next_run - now).total_seconds()
                        logger.warning(
                            f"[DAILY MASTER] Previous run failed — retrying at {next_run.strftime('%Y-%m-%d %H:%M')}"
                            f" (in {retry_minutes}m)"
                        )

                    with self.lock:
                        self.state['next_runs'][task] = next_run.isoformat()
                        self._save_state()

                    # Wait until scheduled time (check stop flag every 60s)
                    while wait_seconds > 0 and not self.stop_flags[task].is_set():
                        sleep_chunk = min(wait_seconds, 60)
                        self.stop_flags[task].wait(timeout=sleep_chunk)
                        wait_seconds -= sleep_chunk

                    if self.stop_flags[task].is_set():
                        return

                # Acquire locks
                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info(f"[SKIP] {task} still running, skipping")
                    continue

                try:
                    if not self._try_acquire_global('daily_master'):
                        continue

                    self._log_to_dashboard("=== DAILY MASTER SYNC STARTED ===")
                    logger.info("[DAILY MASTER] === FETCH + SYNC ALL MASTER DATA ===")
                    success = True

                    # Step 1: Fetch customers from Tally
                    cust_result = {'new_saved': 0, 'updated': 0, 'errors': 0}
                    try:
                        cust_result = fetch_customers_from_all_companies()
                        logger.info(
                            f"[DAILY MASTER] Customers fetched: "
                            f"new={cust_result.get('new_saved', 0)}, "
                            f"updated={cust_result.get('updated', 0)}"
                        )
                    except Exception as e:
                        success = False
                        logger.error(f"[DAILY MASTER] Customer fetch failed: {e}", exc_info=True)

                    # Step 2: Fetch products from Tally
                    prod_result = {'new_saved': 0, 'duplicates_skipped': 0, 'errors': 0}
                    try:
                        prod_result = fetch_products_from_all_companies()
                        logger.info(
                            f"[DAILY MASTER] Products fetched: "
                            f"new={prod_result.get('new_saved', 0)}, "
                            f"updated={prod_result.get('duplicates_skipped', 0)}"
                        )
                    except Exception as e:
                        success = False
                        logger.error(f"[DAILY MASTER] Product fetch failed: {e}", exc_info=True)

                    # Step 3: Sync customers to Catalytics (batch)
                    cust_sync = {'synced': 0, 'failed': 0}
                    try:
                        cust_sync = syncer.sync_customers()
                        logger.info(
                            f"[DAILY MASTER] Customers synced: "
                            f"ok={cust_sync.get('synced', 0)}, "
                            f"failed={cust_sync.get('failed', 0)}"
                        )
                    except Exception as e:
                        success = False
                        logger.error(f"[DAILY MASTER] Customer sync failed: {e}", exc_info=True)

                    # Step 4: Sync products to Catalytics (batch)
                    prod_sync = {'synced': 0, 'failed': 0}
                    try:
                        prod_sync = syncer.sync_products()
                        logger.info(
                            f"[DAILY MASTER] Products synced: "
                            f"ok={prod_sync.get('synced', 0)}, "
                            f"failed={prod_sync.get('failed', 0)}"
                        )
                    except Exception as e:
                        success = False
                        logger.error(f"[DAILY MASTER] Product sync failed: {e}", exc_info=True)

                    self._log_to_dashboard(
                        f"=== DAILY MASTER SYNC COMPLETED "
                        f"(cust fetch={cust_result.get('new_saved', 0)}+{cust_result.get('updated', 0)} "
                        f"sync={cust_sync.get('synced', 0)} | "
                        f"prod fetch={prod_result.get('new_saved', 0)}+{prod_result.get('duplicates_skipped', 0)} "
                        f"sync={prod_sync.get('synced', 0)}) ==="
                    )

                    with self.lock:
                        self.state['last_runs'][task] = datetime.now().isoformat()
                        # Also update sync_master last run
                        self.state['last_runs']['sync_master'] = datetime.now().isoformat()
                        self._save_state()

                    logger.info("[DAILY MASTER] Complete")

                finally:
                    self._release_global()
                    self._running_tasks[task].release()

                last_run_success = success

            except Exception as e:
                last_run_success = False
                logger.error(f"[DAILY MASTER] Error: {e}", exc_info=True)
                self._log_to_dashboard(f"=== DAILY MASTER SYNC ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=60)

    # _fetch_invoices_loop and _sync_loop removed — merged into _invoice_fetch_sync_loop

    def _invoice_fetch_sync_loop(self):
        """
        Combined invoice fetch + sync loop (same pattern as CO middleware).
        1. Fetch invoices from Tally Day Book
        2. Sync any unsynced customers to Catalytics (auto-created by fetch)
        3. Sync any unsynced products to Catalytics (auto-created by fetch)
        4. Sync unsynced invoices/DCs to Catalytics (batch)
        Runs every FETCH_SYNC_INTERVAL_SECONDS (default 10s).
        """
        from fetch_invoices import fetch_invoices_from_all_companies
        from sync_to_catalytics import CatalyticsSyncer

        task = 'sync'
        syncer = CatalyticsSyncer()
        import os
        interval = max(1, int(os.getenv('FETCH_SYNC_INTERVAL_SECONDS', '10')))

        while not self.stop_flags[task].is_set():
            try:
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self.state['next_runs']['fetch_invoices'] = next_run.isoformat()
                    self._save_state()

                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info(f"[SKIP] {task} still running from previous cycle, skipping")
                    self.stop_flags[task].wait(timeout=interval)
                    continue

                try:
                    if not self._try_acquire_global('sync'):
                        self.stop_flags[task].wait(timeout=interval)
                        continue

                    self._log_to_dashboard(f"=== AUTO: INVOICE FETCH + SYNC STARTED ===")

                    # Step 1: Fetch invoices from Tally Day Book
                    fetch_ok, _ = self._run_with_log_capture(fetch_invoices_from_all_companies)

                    # Step 2: Sync any unsynced customers (auto-created by invoice fetch)
                    cust_synced = 0
                    try:
                        cust_result = syncer.sync_customers()
                        cust_synced = cust_result.get('synced', 0)
                        if cust_synced > 0:
                            logger.info(f"Customers synced: ok={cust_synced}, failed={cust_result.get('failed', 0)}")
                    except Exception as e:
                        logger.error(f"Customer sync failed: {e}", exc_info=True)

                    # Step 3: Sync any unsynced products (auto-created by invoice fetch)
                    prod_synced = 0
                    try:
                        prod_result = syncer.sync_products()
                        prod_synced = prod_result.get('synced', 0)
                        if prod_synced > 0:
                            logger.info(f"Products synced: ok={prod_synced}, failed={prod_result.get('failed', 0)}")
                    except Exception as e:
                        logger.error(f"Product sync failed: {e}", exc_info=True)

                    # Step 4: Sync unsynced invoices/DCs to Catalytics
                    sync_ok, _ = self._run_with_log_capture(syncer.sync_invoices_to_dc)

                    if fetch_ok and sync_ok:
                        self._log_to_dashboard(
                            f"=== AUTO: FETCH+SYNC COMPLETED "
                            f"(cust={cust_synced} prod={prod_synced}) ==="
                        )
                    else:
                        self._log_to_dashboard(f"=== AUTO: INVOICE FETCH + SYNC FAILED ===")

                    with self.lock:
                        self.state['last_runs'][task] = datetime.now().isoformat()
                        self.state['last_runs']['fetch_invoices'] = datetime.now().isoformat()
                        self._save_state()

                finally:
                    self._release_global()
                    self._running_tasks[task].release()

                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in invoice fetch+sync: {e}")
                self._log_to_dashboard(f"=== AUTO: INVOICE FETCH+SYNC ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)


# Global automation manager instance
_manager = None

def get_manager():
    """Get or create the global automation manager instance"""
    global _manager
    if _manager is None:
        _manager = AutomationManager()
    return _manager


if __name__ == '__main__':
    # Allow running as a standalone script
    print("=== BOL Middleware Automation Manager (Standalone) ===")
    manager = get_manager()
    success = manager.start()
    if success:
        print("[INFO] Automation loops started. Press Ctrl+C to stop.")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[INFO] Stopping automation...")
            manager.stop()
            print("[INFO] Done.")
    else:
        print("[ERROR] Automation is already running.")
