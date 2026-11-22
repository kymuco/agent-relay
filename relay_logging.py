"""Concise operational logging with bounded local diagnostics, no message bodies."""
from __future__ import annotations
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG = logging.getLogger("agentrelay")
LOG.addHandler(logging.NullHandler())


def configure_logging(level: str = "normal", filename: str = "logs/agentrelay.log",
                      *, root: Path | None = None) -> None:
    console_levels = {"normal": logging.INFO, "quiet": logging.WARNING, "debug": logging.DEBUG}
    level = level.strip().lower()
    if level not in console_levels:
        raise ValueError("LOG_LEVEL must be normal, quiet, or debug")
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
        handler.close()
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(console_levels[level])
    stream.setFormatter(fmt)
    LOG.addHandler(stream)
    if filename and filename.strip().lower() not in ("off", "none"):
        path = Path(filename).expanduser()
        if root is not None and not path.is_absolute():
            path = root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        disk = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        disk.setFormatter(fmt)
        disk.setLevel(logging.DEBUG)
        LOG.addHandler(disk)
