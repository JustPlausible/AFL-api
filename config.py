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
AFL_MATCH_WINDOW_PLANNER_ENABLED = os.getenv("AFL_MATCH_WINDOW_PLANNER_ENABLED", "true")
AFL_MATCH_WINDOW_PRE_MATCH_SECONDS = int(os.getenv("AFL_MATCH_WINDOW_PRE_MATCH_SECONDS", "7200"))
AFL_MATCH_WINDOW_POST_HORIZON_SECONDS = int(os.getenv("AFL_MATCH_WINDOW_POST_HORIZON_SECONDS", "43200"))
AFL_MATCH_WINDOW_LEASE_SECONDS = int(os.getenv("AFL_MATCH_WINDOW_LEASE_SECONDS", "900"))
AFL_MATCH_WINDOW_RECONCILE_SECONDS = int(os.getenv("AFL_MATCH_WINDOW_RECONCILE_SECONDS", "1800"))
AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS = os.getenv("AFL_MATCH_WINDOW_SUPPORTED_COMPETITIONS", "")
AFL_MATCH_WINDOW_SUPPORTED_SEASONS = os.getenv("AFL_MATCH_WINDOW_SUPPORTED_SEASONS", "")
AFL_MATCH_WINDOW_POLICY_VERSION = os.getenv("AFL_MATCH_WINDOW_POLICY_VERSION", "cfs_match_stats_v1")
