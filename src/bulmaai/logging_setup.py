import logging
import sys
from collections import deque
from datetime import datetime, timezone
from typing import Any

import colorlog


class RingBufferHandler(logging.Handler):
    """Keeps the most recent log records in memory for the admin panel's Logs page."""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.last_id = 0
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.last_id += 1  # emit() runs under self.lock
        self.records.append(
            {
                "id": self.last_id,
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": message,
            }
        )

    def since(self, after: int = 0, min_level: int = logging.NOTSET) -> tuple[list[dict[str, Any]], int]:
        """Records newer than `after` at or above `min_level`, plus the newest id."""
        with self.lock:  # other threads may log while we copy
            snapshot = list(self.records)
            last_id = self.last_id
        if after > last_id:  # client saw a previous process; start over
            after = 0
        return [r for r in snapshot if r["id"] > after and r["levelno"] >= min_level], last_id


LOG_BUFFER = RingBufferHandler()


def setup_logging(level: str = "INFO") -> None:
    handler = colorlog.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "white",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    )

    root = logging.getLogger()
    for existing_handler in list(root.handlers):
        root.removeHandler(existing_handler)
        if existing_handler is not LOG_BUFFER:
            existing_handler.close()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    root.addHandler(LOG_BUFFER)
