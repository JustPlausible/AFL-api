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
AFL_RECOVERY_STARTUP_CANDIDATE_LIMIT = _parse_int_env("AFL_RECOVERY_STARTUP_CANDIDATE_LIMIT", 500)

# Diagnostic evidence-capture framework (see diagnostics/framework.py and
# docs/diagnostics_framework.md). Disabled by default; diagnostic profiles never
# feed production scheduler decisions and never become source authority for the
# consumer API. AFL_DIAGNOSTIC_PROFILES only ever selects among profiles already
# checked in and registered in this process -- it is not a generic
# scripting/configuration mechanism for arbitrary URLs or JSON paths.
#
# Backward compatibility: this framework replaces the single-purpose
# AFL_CAPTURE_MATCH_STATE_EVIDENCE / AFL_MATCH_STATE_CAPTURE_* names that
# shipped with PR #175 (Issue #148) before the framework existed. A deployment
# that only sets those legacy names keeps behaving exactly as it did under
# PR #175: AFL_CAPTURE_MATCH_STATE_EVIDENCE=true is treated as
# AFL_DIAGNOSTICS_ENABLED=true with AFL_DIAGNOSTIC_PROFILES defaulting to
# match_clock, and each legacy interval/tolerance value is used whenever its
# AFL_DIAGNOSTIC_MATCH_CLOCK_* replacement is not explicitly set. Whenever a new
# name is explicitly set, it always wins over the legacy one. New deployments
# should set the AFL_DIAGNOSTIC_* names directly -- the legacy names are
# deprecated and may be removed once no deployment still relies on them.
def _bool_env_with_legacy_fallback(name: str, legacy_name: str, default: bool) -> bool:
    if os.getenv(name) is not None:
        return _parse_bool_env(name, default)
    return _parse_bool_env(legacy_name, default)


def _int_env_with_legacy_fallback(name: str, legacy_name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is not None and raw != "":
        return _parse_int_env(name, default)
    return _parse_int_env(legacy_name, default)


def _diagnostic_profiles_with_legacy_fallback(name: str, legacy_enabled_name: str) -> tuple[str, ...]:
    explicit = _parse_csv_env(name)
    if explicit:
        return explicit
    if _parse_bool_env(legacy_enabled_name, False):
        return ("match_clock",)
    return ()


AFL_DIAGNOSTICS_ENABLED = _bool_env_with_legacy_fallback(
    "AFL_DIAGNOSTICS_ENABLED", "AFL_CAPTURE_MATCH_STATE_EVIDENCE", False
)
AFL_DIAGNOSTIC_PROFILES = _diagnostic_profiles_with_legacy_fallback(
    "AFL_DIAGNOSTIC_PROFILES", "AFL_CAPTURE_MATCH_STATE_EVIDENCE"
)

# match_clock profile (Issue #148): diagnostic-only live matchItem evidence capture
# investigating score.matchClock.periods/periodCompleted/periodSeconds and
# match.status/score.status behaviour around quarter/half/three-quarter/full time.
# See diagnostics/profiles/match_clock.py and scheduler/match_state_capture.py.
AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS = _int_env_with_legacy_fallback(
    "AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 15
)
# Bounded windows that widen candidate selection slightly beyond a strict
# matches.status='LIVE' snapshot, to avoid missing evidence at either edge of
# the local ~5 minute match-status refresh cadence (see
# scheduler/schedule_match_scrapes.py): capturing shortly after kickoff even
# if matches.status has not yet flipped to LIVE, and continuing shortly after
# it flips away from LIVE so a Q4/full-time transition near that boundary is
# not missed. See scheduler/match_state_capture.py for details.
AFL_DIAGNOSTIC_MATCH_CLOCK_KICKOFF_TOLERANCE_SECONDS = _int_env_with_legacy_fallback(
    "AFL_DIAGNOSTIC_MATCH_CLOCK_KICKOFF_TOLERANCE_SECONDS", "AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS", 600
)
AFL_DIAGNOSTIC_MATCH_CLOCK_POST_LIVE_GRACE_SECONDS = _int_env_with_legacy_fallback(
    "AFL_DIAGNOSTIC_MATCH_CLOCK_POST_LIVE_GRACE_SECONDS", "AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS", 600
)

# Deprecated PR #175 names, retained read-only for introspection/back-compat only
# -- runtime behaviour is driven by the AFL_DIAGNOSTIC_* names above via the
# fallback helpers, not by these attributes directly.
AFL_CAPTURE_MATCH_STATE_EVIDENCE = _parse_bool_env("AFL_CAPTURE_MATCH_STATE_EVIDENCE", False)
AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS = _parse_int_env("AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 15)
AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS = _parse_int_env("AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS", 600)
AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS = _parse_int_env("AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS", 600)

# interchange profile (Issue #193): diagnostic-only live matchInterchange evidence
# capture investigating homeInterchange[]/awayInterchange[] array-position semantics,
# interchangeCount, benchReason, timeOnGround/timeOnBench and team-level interchange
# count totals. A second, independently selectable/schedulable checked-in profile on
# the same diagnostics framework as match_clock -- no legacy names, since this is a
# new investigation. See diagnostics/profiles/interchange.py and
# scheduler/match_interchange_capture.py.
AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS = _parse_int_env("AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS", 15)
AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS = _parse_int_env("AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS", 600)
AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS = _parse_int_env("AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS", 600)

# commentary profile (Issue #196): diagnostic-only live commentaryFeed evidence
# capture investigating the accumulated commentaryEvent[] feed -- quarter-start/
# quarter-end markers, scoreEvent/playerId/teamId attribution, and correlation
# with match_clock's periodNumber/periodSeconds. A third, independently
# selectable/schedulable checked-in profile on the same diagnostics framework as
# match_clock and interchange -- no legacy names, since this is a new
# investigation. See diagnostics/profiles/commentary.py and
# scheduler/match_commentary_capture.py.
AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS = _parse_int_env("AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS", 15)
AFL_DIAGNOSTIC_COMMENTARY_KICKOFF_TOLERANCE_SECONDS = _parse_int_env("AFL_DIAGNOSTIC_COMMENTARY_KICKOFF_TOLERANCE_SECONDS", 600)
AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS = _parse_int_env("AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS", 600)

# Production match-commentary collection (Issue #201). Independent of
# AFL_DIAGNOSTICS_ENABLED/AFL_DIAGNOSTIC_PROFILES entirely -- this is a normal
# production collector, registered unconditionally in
# scheduler/scheduled_tasks.py, and is only gated by its own enabled flag here
# (default on). See afl_json/match_commentary.py and
# scheduler/match_commentary_production.py.
#
# The interval defaults slightly slower than the commentary diagnostic's
# proven 15s cadence: Round 24 evidence (docs/investigation/afl-json/ENDPOINT_CATALOG.md)
# shows commentary events arrive far less often than every 15s (dozens of
# events across a ~2 hour match), so 20s keeps consumer-visible latency low
# without polling meaningfully more often than the feed actually changes.
AFL_COMMENTARY_PRODUCTION_ENABLED = _parse_bool_env("AFL_COMMENTARY_PRODUCTION_ENABLED", True)
AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS = _parse_int_env("AFL_COMMENTARY_PRODUCTION_INTERVAL_SECONDS", 20)
AFL_COMMENTARY_PRODUCTION_KICKOFF_TOLERANCE_SECONDS = _parse_int_env("AFL_COMMENTARY_PRODUCTION_KICKOFF_TOLERANCE_SECONDS", 600)
# Kept generous relative to the diagnostic's 600s post-LIVE grace: Round 24
# evidence confirmed a genuine official score-review correction
# (CD_M20260142406) reaching the feed as a new accumulated entry, and a
# review can plausibly take longer than 10 minutes after full-time to
# resolve. This window covers POSTGAME (always polled -- see
# recently_active_match_provider_ids) plus a bounded grace after that.
AFL_COMMENTARY_PRODUCTION_POSTGAME_GRACE_SECONDS = _parse_int_env("AFL_COMMENTARY_PRODUCTION_POSTGAME_GRACE_SECONDS", 1800)
