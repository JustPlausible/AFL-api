from datetime import datetime

LOG_LEVELS = ["DEBUG", "INFO", "WARN", "ERROR", "SUCCESS"]

def log(message: str, level: str = "INFO", show_time: bool = True):
    if level not in LOG_LEVELS:
        raise ValueError(f"Invalid log level: {level}")

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
