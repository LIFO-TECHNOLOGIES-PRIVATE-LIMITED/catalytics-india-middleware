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

# Default intervals (in seconds) - Read from config
DEFAULT_INTERVALS = {
    'fetch_master': config.FETCH_MASTER_INTERVAL_MINUTES * 60,  # minutes -> seconds
    'fetch_invoices': config.FETCH_INVOICES_INTERVAL_MINUTES * 60,
    'sync': config.SYNC_INVOICES_INTERVAL_SECONDS,
    'sync_master': config.SYNC_MASTER_INTERVAL_MINUTES * 60
}

# Debug: Log the DEFAULT_INTERVALS on module load
print(f"[DEBUG] automation_manager.py loaded with DEFAULT_INTERVALS: {DEFAULT_INTERVALS}")


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
            logger.info("Invoice fetch will use today's date (no start date configured)")

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
        self._start_thread('fetch_master', self._fetch_master_loop)
        self._start_thread('fetch_invoices', self._fetch_invoices_loop)
        self._start_thread('sync', self._sync_loop)
        self._start_thread('sync_master', self._sync_master_loop)

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

    def _fetch_master_loop(self):
        """Continuous loop for fetching master data (customers + products)"""
        from fetch_customers import fetch_customers_from_all_companies
        from fetch_products import fetch_products_from_all_companies

        task = 'fetch_master'
        while not self.stop_flags[task].is_set():
            try:
                # Update next run time
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # Skip if previous run still active
                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info(f"[SKIP] {task} still running from previous cycle, skipping")
                    self.stop_flags[task].wait(timeout=interval)
                    continue

                try:
                    if not self._try_acquire_global('fetch_master'):
                        self.stop_flags[task].wait(timeout=interval)
                        continue
                    # Run fetch
                    logger.info(f"Running {task}...")
                    self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA STARTED ===")

                    ok1, _ = self._run_with_log_capture(fetch_customers_from_all_companies)
                    ok2, _ = self._run_with_log_capture(fetch_products_from_all_companies)

                    if ok1 and ok2:
                        self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA COMPLETED ===")
                    else:
                        self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA FAILED ===")

                    # Update last run time
                    with self.lock:
                        self.state['last_runs'][task] = datetime.now().isoformat()
                        self._save_state()

                    logger.info(f"{task} completed")
                finally:
                    self._release_global()
                    self._running_tasks[task].release()

                # Wait for interval or stop signal
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in {task}: {e}")
                self._log_to_dashboard(f"=== AUTO: FETCH MASTER DATA ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)

    def _fetch_invoices_loop(self):
        """Continuous loop for fetching invoices"""
        from fetch_invoices import fetch_invoices_from_all_companies

        task = 'fetch_invoices'
        while not self.stop_flags[task].is_set():
            try:
                # Update next run time
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # Skip if previous run still active
                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info(f"[SKIP] {task} still running from previous cycle, skipping")
                    self.stop_flags[task].wait(timeout=interval)
                    continue

                try:
                    if not self._try_acquire_global('fetch_invoices'):
                        self.stop_flags[task].wait(timeout=interval)
                        continue
                    # Run fetch
                    logger.info(f"Running {task}...")
                    self._log_to_dashboard(f"=== AUTO: FETCH INVOICES STARTED ===")

                    ok, _ = self._run_with_log_capture(fetch_invoices_from_all_companies)

                    if ok:
                        self._log_to_dashboard(f"=== AUTO: FETCH INVOICES COMPLETED ===")
                    else:
                        self._log_to_dashboard(f"=== AUTO: FETCH INVOICES FAILED ===")

                    # Update last run time
                    with self.lock:
                        self.state['last_runs'][task] = datetime.now().isoformat()
                        self._save_state()

                    logger.info(f"{task} completed")
                finally:
                    self._release_global()
                    self._running_tasks[task].release()

                # Wait for interval or stop signal
                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in {task}: {e}")
                self._log_to_dashboard(f"=== AUTO: FETCH INVOICES ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)

    def _sync_master_loop(self):
        """Continuous loop for syncing customers and products"""
        from sync_to_catalytics import CatalyticsSyncer

        task = 'sync_master'
        syncer = CatalyticsSyncer()
        while not self.stop_flags[task].is_set():
            try:
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info(f"[SKIP] {task} still running from previous cycle, skipping")
                    self.stop_flags[task].wait(timeout=interval)
                    continue

                try:
                    if not self._try_acquire_global(task):
                        self.stop_flags[task].wait(timeout=interval)
                        continue

                    logger.info(f"Running {task}...")
                    self._log_to_dashboard(f"=== AUTO: SYNC MASTER STARTED ===")

                    ok1, _ = self._run_with_log_capture(syncer.sync_customers)
                    ok2, _ = self._run_with_log_capture(syncer.sync_products)

                    if ok1 and ok2:
                        self._log_to_dashboard(f"=== AUTO: SYNC MASTER COMPLETED ===")
                    else:
                        self._log_to_dashboard(f"=== AUTO: SYNC MASTER FAILED ===")

                    with self.lock:
                        self.state['last_runs'][task] = datetime.now().isoformat()
                        self._save_state()

                    logger.info(f"{task} completed")
                finally:
                    self._release_global()
                    self._running_tasks[task].release()

                self.stop_flags[task].wait(timeout=interval)

            except Exception as e:
                logger.error(f"Error in {task}: {e}")
                self._log_to_dashboard(f"=== AUTO: SYNC MASTER ERROR: {e} ===")
                self.stop_flags[task].wait(timeout=10)

    def _sync_loop(self):
        """Continuous loop for syncing to Catalytics"""
        from sync_to_catalytics import CatalyticsSyncer

        task = 'sync'
        syncer = CatalyticsSyncer()  # Reuse single instance (single DB connection)
        while not self.stop_flags[task].is_set():
            try:
                # Update next run time
                interval = self.state['intervals'][task]
                next_run = datetime.now() + timedelta(seconds=interval)
                with self.lock:
                    self.state['next_runs'][task] = next_run.isoformat()
                    self._save_state()

                # Skip if previous run still active
                if not self._running_tasks[task].acquire(blocking=False):
                    logger.info(f"[SKIP] {task} still running from previous cycle, skipping")
                    self.stop_flags[task].wait(timeout=interval)
                    continue

                try:
                    if not self._try_acquire_global('sync'):
                        self.stop_flags[task].wait(timeout=interval)
                        continue
                    # Run sync
                    logger.info(f"Running {task}...")
                    self._log_to_dashboard(f"=== AUTO: SYNC INVOICES STARTED ===")

                    ok, _ = self._run_with_log_capture(syncer.sync_invoices_simple)

                    if ok:
                        self._log_to_dashboard(f"=== AUTO: SYNC INVOICES COMPLETED ===")
                    else:
                        self._log_to_dashboard(f"=== AUTO: SYNC INVOICES FAILED ===")

                    # Update last run time
                    with self.lock:
                        self.state['last_runs'][task] = datetime.now().isoformat()
                        self._save_state()

                    logger.info(f"{task} completed")
                finally:
                    self._release_global()
                    self._running_tasks[task].release()

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
