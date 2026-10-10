"""Minimal structured logging: `event key=value ...` lines with UTC timestamps."""

import logging
import time


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        t = time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created))
        return f"{t}.{int(record.msecs):03d}Z"


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_UtcFormatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def kv(event: str, **fields) -> str:
    """Format a structured log message. Never pass secrets in here."""
    parts = [event]
    for key, value in fields.items():
        if isinstance(value, float):
            value = f"{value:.6g}"
        parts.append(f"{key}={value}")
    return " ".join(parts)
