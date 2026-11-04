"""
utils/logger.py — Centralized logging setup.

WHY THIS FILE EXISTS:
  NautilusTrader has its own internal logging system (the `log` property on
  strategies and actors). We use Python's standard `logging` module for the
  outer application layer: config loading, main.py, and test output.

  This module sets up a consistent format with timestamps, log levels, and
  readable output — no credentials are ever logged.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Create and return a configured logger.

    Parameters
    ----------
    name : str
        Logger name (typically __name__ of the calling module).
    level : int
        Logging level (default: INFO).

    Returns
    -------
    logging.Logger
        Configured logger instance.
    """
    # Create logs directory if it doesn't exist
    LOG_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    # --- Console handler (stdout) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_format = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    # --- File handler ---
    log_file = LOG_DIR / "btc_straddle.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # Save everything to file
    file_format = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S.%f",
    )
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    return logger

