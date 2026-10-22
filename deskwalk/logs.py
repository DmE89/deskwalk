"""Logging: rotating file plus the last lines in memory for the status view."""

import logging
import logging.handlers
import threading
from collections import deque
from pathlib import Path


class MemoryLog(logging.Handler):
    def __init__(self, size=50):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.seq = 0
        self._lines_lock = threading.Lock()

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lines_lock:
            self.lines.append(line)
            self.seq += 1

    def since(self, seq):
        """New lines since seq (all kept lines for a new client), and the current seq."""
        with self._lines_lock:
            if seq >= self.seq:
                return [], self.seq
            return list(self.lines)[-(self.seq - seq):], self.seq


def setup_logging(log_file: Path) -> MemoryLog:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.namer = lambda name: name.replace(".log.", ".") + ".log"  # deskwalk.1.log stays *.log
    memory = MemoryLog()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (file_handler, memory, logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger("bleak").setLevel(logging.WARNING)
    return memory
