# logging_sources.py
"""Authoritative registry of known operational log sources (Issue #179).

This is the single place that names the log files written by
``utils.log.setup_logger`` for operational collectors/scheduler tasks.
Call sites that create one of these loggers pull their logger name and
filename from ``LOG_SOURCES`` instead of repeating the filename as a
literal, and the Admin "Logs & diagnostics" view reads
``get_log_source_statuses()`` instead of keeping its own filename mapping.

This module deliberately does not import scraper/scheduler modules: several
of them create their ``RotatingFileHandler`` (and therefore an empty log
file) as an import-time side effect, and importing them here just to read a
profile name would make loading the Admin logs page create log files that
have never actually run. Enablement is instead read directly from
``config``, which is already the parsed/validated source of truth for the
flags involved.

This is a status/registry helper, not a logging redesign: it does not wrap
or replace ``logging``, and does not change what gets written or where.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import config
from utils.log import LOG_DIR

# The RotatingFileHandler settings utils.log.setup_logger applies uniformly
# to every logger it creates. Reported as metadata on available sources;
# not itself a status, since it is the same for every source below.
ROTATION_MAX_BYTES = 1_000_000
ROTATION_BACKUP_COUNT = 3

STATUS_AVAILABLE = "available"
STATUS_NOT_CREATED = "not_created"
STATUS_DISABLED = "disabled"
STATUS_UNAVAILABLE = "path_unavailable"


def _match_state_capture_enabled() -> bool:
    """Mirrors diagnostics.framework.is_profile_selected("match_clock").

    Reimplemented against the parsed config flags directly (rather than
    importing diagnostics.framework) to avoid the import-time side effect
    described in the module docstring above.
    """
    return bool(config.AFL_DIAGNOSTICS_ENABLED) and "match_clock" in config.AFL_DIAGNOSTIC_PROFILES


@dataclass(frozen=True)
class LogSource:
    """One known operational log source.

    ``enabled`` may be a plain bool or a zero-arg callable resolved at
    status-check time, for sources whose availability depends on
    configuration that can change without restarting the admin process.
    """

    id: str
    display_name: str
    description: str
    logger_name: str
    filename: str
    enabled: bool | Callable[[], bool] = True
    disabled_reason: str | None = None

    def is_enabled(self) -> bool:
        return self.enabled() if callable(self.enabled) else self.enabled

    @property
    def path(self) -> Path:
        return Path(LOG_DIR) / self.filename


# Known operational log sources, in the order the Admin UI should present
# them. Adding a new operational log means adding an entry here -- nothing
# else should need a filename literal for it.
_SOURCES: tuple[LogSource, ...] = (
    LogSource(
        id="player_stats",
        display_name="Player Stats",
        description="Per-match AFL player statistics scraper output.",
        logger_name="player_stats_scraper",
        filename="scrape_afl_player_stats.log",
    ),
    LogSource(
        id="injuries",
        display_name="Injuries",
        description="AFL injury list scraper output.",
        logger_name="injury_scraper",
        filename="scrape_afl_injuries.log",
    ),
    LogSource(
        id="lineups",
        display_name="Lineups",
        description="AFL team lineup scraper output.",
        logger_name="lineup_scraper",
        filename="scrape_afl_lineups.log",
    ),
    LogSource(
        id="matches",
        display_name="Matches",
        description="AFL match/fixture scraper output.",
        logger_name="match_scraper",
        filename="scrape_afl_matches.log",
    ),
    LogSource(
        id="scheduler_jobs",
        display_name="Scheduler Jobs",
        description="Scheduled scrape job orchestration output.",
        logger_name="scheduler_jobs",
        filename="scheduler_jobs.log",
    ),
    LogSource(
        id="match_state_capture",
        display_name="Match State Capture",
        description="Diagnostic-only live matchItem evidence capture (Issue #148).",
        logger_name="match_state_capture",
        filename="match_state_capture.log",
        enabled=_match_state_capture_enabled,
        disabled_reason=(
            "The 'match_clock' diagnostic profile is not enabled "
            "(AFL_DIAGNOSTICS_ENABLED / AFL_DIAGNOSTIC_PROFILES)."
        ),
    ),
)

LOG_SOURCES: dict[str, LogSource] = {source.id: source for source in _SOURCES}


@dataclass(frozen=True)
class LogSourceStatus:
    id: str
    display_name: str
    description: str
    enabled: bool
    status: str
    reason: str
    resolved_path: str
    exists: bool
    size_bytes: int | None
    modified_at: datetime | None
    rotation: dict[str, int] | None


def get_log_source_status(source: LogSource) -> LogSourceStatus:
    """Resolve one source's current, safe-to-display status.

    Never raises: any filesystem error resolving or reading the target is
    reported as ``STATUS_UNAVAILABLE`` rather than propagating, since a log
    being unreadable must stay a diagnostic detail, not a request failure.
    """
    resolved_path = str(source.path)
    base = dict(
        id=source.id,
        display_name=source.display_name,
        description=source.description,
        resolved_path=resolved_path,
    )

    if not source.is_enabled():
        return LogSourceStatus(
            **base,
            enabled=False,
            status=STATUS_DISABLED,
            reason=source.disabled_reason or "This log source is not enabled in the current configuration.",
            exists=False,
            size_bytes=None,
            modified_at=None,
            rotation=None,
        )

    try:
        parent_ok = source.path.parent.is_dir()
    except OSError:
        parent_ok = False

    if not parent_ok:
        return LogSourceStatus(
            **base,
            enabled=True,
            status=STATUS_UNAVAILABLE,
            reason=f"Expected log directory '{source.path.parent}' is not accessible.",
            exists=False,
            size_bytes=None,
            modified_at=None,
            rotation=None,
        )

    try:
        exists = source.path.exists()
    except OSError:
        return LogSourceStatus(
            **base,
            enabled=True,
            status=STATUS_UNAVAILABLE,
            reason="Expected log path could not be accessed.",
            exists=False,
            size_bytes=None,
            modified_at=None,
            rotation=None,
        )

    if not exists:
        return LogSourceStatus(
            **base,
            enabled=True,
            status=STATUS_NOT_CREATED,
            reason="Configured correctly; no log has been written yet.",
            exists=False,
            size_bytes=None,
            modified_at=None,
            rotation=None,
        )

    try:
        stat_result = source.path.stat()
    except OSError:
        return LogSourceStatus(
            **base,
            enabled=True,
            status=STATUS_UNAVAILABLE,
            reason="Log file exists but its metadata could not be read.",
            exists=True,
            size_bytes=None,
            modified_at=None,
            rotation=None,
        )

    return LogSourceStatus(
        **base,
        enabled=True,
        status=STATUS_AVAILABLE,
        reason="",
        exists=True,
        size_bytes=stat_result.st_size,
        modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
        rotation={"max_bytes": ROTATION_MAX_BYTES, "backup_count": ROTATION_BACKUP_COUNT},
    )


def get_log_source_statuses() -> list[LogSourceStatus]:
    """Every known source's current status, in registry order."""
    return [get_log_source_status(source) for source in LOG_SOURCES.values()]
