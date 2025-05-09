# src/config.py

import os
from dotenv import load_dotenv
from pathlib import Path
import random

# Load .env from project root
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

# General Settings
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Fixture Scraper Settings
AFL_COMPETITION_ID = int(os.getenv("AFL_COMPETITION_ID", 1))
AFL_SEASON_ID = int(os.getenv("AFL_SEASON_ID", 73))
AFL_SEASON_PID = os.getenv("AFL_SEASON_PID", "CD_S2025014")
AFL_BASE_URL = os.getenv("AFL_BASE_URL", "https://www.afl.com.au")

# Scraper Behaviour
SCRAPER_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT", 10))

USER_AGENTS_RAW = os.getenv("USER_AGENTS", "")
USER_AGENTS = USER_AGENTS_RAW.split("|")

def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)

def get_scraper_headers() -> dict:
    return {
        "User-Agent": get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-AU,en;q=0.9"
    }

# Database Settings
DB_PATH = os.getenv("DB_PATH", "data/afl_players.db")