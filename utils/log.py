# utils/log.py

import logging
from logging.handlers import RotatingFileHandler
import os
import time

LOG_LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "SUCCESS"]

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

EMOJI = {
    "DEBUG": "🐞",
    "INFO": "ℹ️",
    "WARN": "⚠️",
    "ERROR": "❌",
    "SUCCESS": "✅"
}

def setup_logger(name: str, filename: str, level=logging.DEBUG, use_emoji: bool = True):
    """Create an isolated logger instance with emoji + UTC formatting."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        class UTCEmojiFormatter(logging.Formatter):
            def format(self, record):
                emoji = EMOJI.get(record.levelname, "")
                record.msg = f"{emoji} {record.msg}" if use_emoji else record.msg
                return super().format(record)

        file_formatter = UTCEmojiFormatter('[%(asctime)s UTC] %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        file_formatter.converter = time.gmtime  # Use UTC in file

        console_formatter = UTCEmojiFormatter('[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
        console_formatter.converter = time.localtime  # Local time in console

        ch = logging.StreamHandler()
        ch.setFormatter(console_formatter)
        logger.addHandler(ch)

        fh = RotatingFileHandler(os.path.join(LOG_DIR, filename), maxBytes=1_000_000, backupCount=3)
        fh.setFormatter(file_formatter)
        logger.addHandler(fh)

        logger.propagate = False  # 🛑 Prevent cross-handler bleed

    return logger


# Optional fallback logger (for CLI and scripts that just `from utils.log import log`)
_default_logger = setup_logger("default", "default.log")

def log(message: str, level: str = "INFO", show_time: bool = True):
    level = level.upper()
    if level not in LOG_LEVELS:
        raise ValueError(f"Invalid log level: {level}")

    msg = message if show_time else f"{EMOJI.get(level, '')} {message}"
    getattr(_default_logger, level.lower(), _default_logger.info)(msg)
