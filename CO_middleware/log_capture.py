"""
Log capture utility for CO Middleware Dashboard
Provides real-time log streaming to dashboard terminal
"""

import threading
from collections import deque
from datetime import datetime


class DashboardLogger:
    """Captures log messages for display in dashboard terminal"""
    
    def __init__(self, max_lines=500):
        self.max_lines = max_lines
        self.lines = deque(maxlen=max_lines)
        self.lock = threading.Lock()
    
    def write_log(self, message):
        """Write a log message with timestamp"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted = f"[{timestamp}] {message}"
        with self.lock:
            self.lines.append(formatted)
    
    def write_raw(self, message):
        """Write a raw message without timestamp"""
        with self.lock:
            self.lines.append(message)
    
    def get_logs(self, lines=100):
        """Get recent log lines"""
        with self.lock:
            return list(self.lines)[-lines:]
    
    def clear(self):
        """Clear all logs"""
        with self.lock:
            self.lines.clear()


# Global dashboard logger instance
dashboard_logger = DashboardLogger()
