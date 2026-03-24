'''
Log capture utility for CO Middleware Dashboard
Provides real-time log streaming to dashboard terminal
'''

import os
import threading
from datetime import datetime
from io import StringIO


class DashboardLogCapture:
    '''Capture stdout/stderr and save to file for dashboard display.'''

    def __init__(self, log_file=None):
        from config import BASE_DIR
        if log_file is None:
            log_file = str(BASE_DIR / 'logs' / 'dashboard_operations.log')
        self.log_file = log_file
        self.original_stdout = None
        self.original_stderr = None
        self.log_buffer = StringIO()
        self._lock = threading.Lock()

        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def _append_line(self, entry):
        line = entry.rstrip('\n') + '\n'
        with self._lock:
            try:
                with open(self.log_file, 'a', encoding='utf-8') as handle:
                    handle.write(line)
            except Exception:
                pass
            self.log_buffer.write(line)

    def write_log(self, message):
        '''Write message to log file with timestamp.'''
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._append_line(f'[{timestamp}] {message}')

    def write_raw(self, line):
        '''Write a raw line to log file (no extra timestamp added).'''
        self._append_line(line)

    def get_recent_logs(self, lines=100):
        '''Get last N lines from log file as a string.'''
        try:
            with open(self.log_file, 'r', encoding='utf-8', errors='ignore') as handle:
                all_lines = handle.readlines()
                return ''.join(all_lines[-lines:])
        except FileNotFoundError:
            return 'No logs yet...'
        except Exception as exc:
            return f'Error reading logs: {exc}'

    def get_logs(self, lines=100):
        '''Get recent log lines as list (compatibility).'''
        text = self.get_recent_logs(lines)
        return text.splitlines()

    def clear_logs(self):
        '''Clear the log file.'''
        try:
            with self._lock:
                with open(self.log_file, 'w', encoding='utf-8') as handle:
                    handle.write('')
                self.log_buffer = StringIO()
            return True
        except Exception:
            return False

    def clear(self):
        '''Backward-compatible alias.'''
        return self.clear_logs()


# Global dashboard logger instance
dashboard_logger = DashboardLogCapture()
