# utils/log.py

from datetime import datetime
import os

LOG_LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "SUCCESS"]

# Set the minimum log level (can override with env var)
CURRENT_LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG").upper()

def log(message: str, level: str = "INFO", show_time: bool = True):
    if level not in LOG_LEVELS:
        raise ValueError(f"Invalid log level: {level}")

    # Only print if this level is >= current level
    if LOG_LEVELS.index(level) < LOG_LEVELS.index(CURRENT_LOG_LEVEL):
        return

    emoji = {
        "DEBUG": "🐞",
        "INFO": "ℹ️ ",
        "WARN": "⚠️ ",
        "ERROR": "❌",
        "SUCCESS": "✅"
    }.get(level, "")

    timestamp = datetime.now().strftime("%H:%M:%S") if show_time else ""
    prefix = f"[{timestamp}] {emoji} {level}" if show_time else f"{emoji} {level}"
    print(f"{prefix}: {message}")
