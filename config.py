# src/config.py

import os
import random
from dotenv import load_dotenv
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent

# Load .env from project root (does not override real environment variables)
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

# General Settings
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Fixture / AFL Site Settings
AFL_COMPETITION_ID = int(os.getenv("AFL_COMPETITION_ID", 1))
AFL_SEASON_ID = int(os.getenv("AFL_SEASON_ID", 73))
AFL_SEASON_PID = os.getenv("AFL_SEASON_PID", "CD_S2025014")
AFL_COMPETITION_CODE = os.getenv("AFL_COMPETITION_CODE", "AFL")
AFL_COMPETITION_PROVIDER_ID = os.getenv("AFL_COMPETITION_PROVIDER_ID", "CD_C014")
AFL_SEASON_YEAR = os.getenv("AFL_SEASON_YEAR")
AFL_BASE_URL = os.getenv("AFL_BASE_URL", "https://www.afl.com.au")
AFL_MATCH_DAY_TIMEZONE = os.getenv("AFL_MATCH_DAY_TIMEZONE", "Australia/Perth")

# Scraper Behaviour
SCRAPER_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT", 10))

# User agents
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

USER_AGENTS_RAW = os.getenv("USER_AGENTS", "")
USER_AGENTS = [ua.strip() for ua in USER_AGENTS_RAW.split("|") if ua.strip()] or [DEFAULT_USER_AGENT]

def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)

def get_scraper_headers() -> dict:
    return {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
        "Connection": "keep-alive",
    }

# Database Settings
DB_PATH_RAW = os.getenv("DB_PATH", "data/afl_players.db")
DB_PATH = str((PROJECT_ROOT / DB_PATH_RAW).resolve()) if not os.path.isabs(DB_PATH_RAW) else DB_PATH_RAW


# Durable match-window planner (Issue #132). Reconciliation only; repeated polling is disabled until Issue #133.
def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_csv_env(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


AFL_MATCH_WINDOW_PLANNER_ENABLED = _parse_bool_env("AFL_MATCH_WINDOW_PLANNER_ENABLED", True)
AFL_MATCH_WINDOW_PRE_MATCH_SECONDS = _parse_int_env("AFL_MATCH_WINDOW_PRE_MATCH_SECONDS", 7200)
AFL_MATCH_WINDOW_POST_HORIZON_SECONDS = _parse_int_env("AFL_MATCH_WINDOW_POST_HORIZON_SECONDS", 43200)
AFL_MATCH_WINDOW_LEASE_SECONDS = _parse_int_env("AFL_MATCH_WINDOW_LEASE_SECONDS", 900)
AFL_MATCH_WINDOW_RECONCILE_SECONDS = _parse_int_env("AFL_MATCH_WINDOW_RECONCILE_SECONDS", 1800)
AFL_MATCH_WINDOW_EXPECTED_MATCH_SECONDS = _parse_int_env("AFL_MATCH_WINDOW_EXPECTED_MATCH_SECONDS", 10800)
AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS = _parse_csv_env("AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS")
AFL_MATCH_WINDOW_SUPPORTED_SEASONS = _parse_csv_env("AFL_MATCH_WINDOW_SUPPORTED_SEASONS")
AFL_MATCH_WINDOW_POLICY_VERSION = os.getenv("AFL_MATCH_WINDOW_POLICY_VERSION", "cfs_match_stats_v1")

# Conservative CFS player-stat polling pilot (Issue #133). Safe default is disabled.
AFL_PLAYER_STAT_POLLING_ENABLED = _parse_bool_env("AFL_PLAYER_STAT_POLLING_ENABLED", False)
AFL_PLAYER_STAT_POLLING_KILL_SWITCH = _parse_bool_env("AFL_PLAYER_STAT_POLLING_KILL_SWITCH", False)
AFL_PLAYER_STAT_POLLING_DRAIN = _parse_bool_env("AFL_PLAYER_STAT_POLLING_DRAIN", False)
AFL_PLAYER_STAT_POLLING_MAX_WORKERS = _parse_int_env("AFL_PLAYER_STAT_POLLING_MAX_WORKERS", 2)
AFL_PLAYER_STAT_POLLING_NETWORK_CONCURRENCY = _parse_int_env("AFL_PLAYER_STAT_POLLING_NETWORK_CONCURRENCY", 2)
AFL_PLAYER_STAT_POLLING_CLAIM_LIMIT = _parse_int_env("AFL_PLAYER_STAT_POLLING_CLAIM_LIMIT", 2)
AFL_PLAYER_STAT_POLLING_LIVE_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_LIVE_SECONDS", 60)
AFL_PLAYER_STAT_POLLING_PRE_MATCH_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_PRE_MATCH_SECONDS", 300)
AFL_PLAYER_STAT_POLLING_POST_MATCH_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_POST_MATCH_SECONDS", 120)
AFL_PLAYER_STAT_POLLING_UNAVAILABLE_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_UNAVAILABLE_SECONDS", 300)
AFL_PLAYER_STAT_POLLING_PARTIAL_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_PARTIAL_SECONDS", 120)
AFL_PLAYER_STAT_POLLING_TRANSIENT_BACKOFF_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_TRANSIENT_BACKOFF_SECONDS", 300)
AFL_PLAYER_STAT_POLLING_RATE_LIMIT_BACKOFF_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_RATE_LIMIT_BACKOFF_SECONDS", 600)
AFL_PLAYER_STAT_POLLING_AUTH_PAUSE_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_AUTH_PAUSE_SECONDS", 1800)
AFL_PLAYER_STAT_POLLING_MAX_BACKOFF_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_MAX_BACKOFF_SECONDS", 3600)
AFL_PLAYER_STAT_POLLING_JITTER_SECONDS = _parse_int_env("AFL_PLAYER_STAT_POLLING_JITTER_SECONDS", 5)
AFL_PLAYER_STAT_POLLING_ALLOWED_COMPETITIONS = _parse_csv_env("AFL_PLAYER_STAT_POLLING_ALLOWED_COMPETITIONS")
AFL_PLAYER_STAT_POLLING_ALLOWED_SEASONS = _parse_csv_env("AFL_PLAYER_STAT_POLLING_ALLOWED_SEASONS")
AFL_PLAYER_STAT_POLLING_ALLOWED_MATCHES = _parse_csv_env("AFL_PLAYER_STAT_POLLING_ALLOWED_MATCHES")

# Interrupted polling recovery (Issue #134). Lease expiry remains primary.
AFL_RECOVERY_MAX_ATTEMPT_SECONDS = _parse_int_env("AFL_RECOVERY_MAX_ATTEMPT_SECONDS", 1800)
AFL_RECOVERY_REGISTRY_STALE_SECONDS = _parse_int_env("AFL_RECOVERY_REGISTRY_STALE_SECONDS", 1800)
AFL_RECOVERY_SCRAPE_RUN_STALE_SECONDS = _parse_int_env("AFL_RECOVERY_SCRAPE_RUN_STALE_SECONDS", 1800)
AFL_RECOVERY_SHUTDOWN_GRACE_SECONDS = _parse_int_env("AFL_RECOVERY_SHUTDOWN_GRACE_SECONDS", 120)
AFL_SCHEDULER_HEARTBEAT_SECONDS = _parse_int_env("AFL_SCHEDULER_HEARTBEAT_SECONDS", 15)
