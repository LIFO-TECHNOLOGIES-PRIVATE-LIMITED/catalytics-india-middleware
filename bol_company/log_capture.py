"""
Capture terminal output for display in dashboard
"""
import os
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path

class DashboardLogCapture:
    """Capture stdout/stderr and save to file for dashboard display"""

    def __init__(self, log_file=None):
        from config import BASE_DIR
        if log_file is None:
            log_file = str(BASE_DIR / 'logs' / 'dashboard_operations.log')
        self.log_file = log_file
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        self.log_buffer = StringIO()

        # Ensure logs directory exists
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def write_log(self, message):
        """Write message to log file with timestamp"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f'[{timestamp}] {message}\n'

        # Write to file
        try:
            with open(self.log_file, 'a') as f:
                f.write(log_entry)
        except Exception as e:
            print(f"Error writing to log: {e}", file=self.original_stderr)

        # Also write to buffer for real-time display
        self.log_buffer.write(log_entry)

    def write_raw(self, line):
        """Write a raw line to log file (no extra timestamp added)"""
        entry = line.rstrip('\n') + '\n'
        try:
            with open(self.log_file, 'a') as f:
                f.write(entry)
        except Exception as e:
            print(f"Error writing to log: {e}", file=self.original_stderr)
        self.log_buffer.write(entry)

    def get_recent_logs(self, lines=100):
        """Get last N lines from log file"""
        try:
            with open(self.log_file, 'r') as f:
                all_lines = f.readlines()
                return ''.join(all_lines[-lines:])
        except FileNotFoundError:
            return "No logs yet..."
        except Exception as e:
            return f"Error reading logs: {e}"

    def clear_logs(self):
        """Clear the log file"""
        try:
            with open(self.log_file, 'w') as f:
                f.write('')
            return True
        except Exception as e:
            print(f"Error clearing logs: {e}", file=self.original_stderr)
            return False


# Global instance
dashboard_logger = DashboardLogCapture()
