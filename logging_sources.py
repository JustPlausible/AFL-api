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
#
# Deliberately not registered: scraper/scrape_afl_lineups-early2025.py (dead
# code -- unimportable module name, referenced nowhere, and writes into the
# same scrape_afl_lineups.log as the real "lineups" source above rather than
# a distinct file); scraper/monitor_match_status.py (a one-off manual debug
# script targeting a single hard-coded match ID, not a running collector);
# utils.log's own "default" fallback logger (a catch-all used ad hoc across
# many unrelated modules, not one discrete operational source).
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
        id="scheduler_start",
        display_name="Scheduler Start",
        description="Scheduler process startup/shutdown output.",
        logger_name="scheduler_start",
        filename="scheduler_start.log",
    ),
    LogSource(
        id="scheduler_registry",
        display_name="Scheduler Registry",
        description="Persistent scheduler job registry and restart reconciliation output.",
        logger_name="scheduler_registry",
        filename="scheduler_registry.log",
    ),
    LogSource(
        id="scheduled_tasks",
        display_name="Scheduled Tasks",
        description="APScheduler bootstrap and broken-job cleanup output.",
        logger_name="scheduled_tasks",
        filename="scheduled_tasks.log",
    ),
    LogSource(
        id="refresh_live_matches",
        display_name="Live Match Refresh",
        description="Recurring live-match status refresh job output.",
        logger_name="refresh_live_matches",
        filename="refresh_live_matches.log",
    ),
    LogSource(
        id="refresh_afl_lineups",
        display_name="Lineup Refresh",
        description="Recurring lineup refresh scheduling output.",
        logger_name="refresh_afl_lineups",
        filename="refresh_afl_lineups.log",
    ),
    LogSource(
        id="diagnostics_framework",
        display_name="Diagnostics Framework",
        description=(
            "Diagnostic evidence-capture framework registration/runtime output "
            "(Issue #148). Written whenever the scheduler starts, whether or not "
            "any diagnostic profile is enabled."
        ),
        logger_name="diagnostics_framework",
        filename="diagnostics_framework.log",
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


@dataclass(frozen=True)
class _Probe:
    """Raw filesystem facts about a resolved log path, independent of whether
    the owning source is enabled. Kept separate from LogSourceStatus so a
    disabled source's historical file (size/modified time) is never silently
    discarded just because the source itself is currently disabled."""

    exists: bool
    size_bytes: int | None
    modified_at: datetime | None
    unavailable_reason: str | None


def _probe_path(path: Path) -> _Probe:
    try:
        parent_ok = path.parent.is_dir()
    except OSError:
        parent_ok = False
    if not parent_ok:
        return _Probe(
            exists=False, size_bytes=None, modified_at=None,
            unavailable_reason=f"Expected log directory '{path.parent}' is not accessible.",
        )

    try:
        exists = path.exists()
    except OSError:
        return _Probe(
            exists=False, size_bytes=None, modified_at=None,
            unavailable_reason="Expected log path could not be accessed.",
        )

    if not exists:
        return _Probe(exists=False, size_bytes=None, modified_at=None, unavailable_reason=None)

    if not path.is_file():
        return _Probe(
            exists=True, size_bytes=None, modified_at=None,
            unavailable_reason="Expected log path exists but is not a regular file.",
        )

    try:
        stat_result = path.stat()
    except OSError:
        return _Probe(
            exists=True, size_bytes=None, modified_at=None,
            unavailable_reason="Log file exists but its metadata could not be read.",
        )

    return _Probe(
        exists=True,
        size_bytes=stat_result.st_size,
        modified_at=datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
        unavailable_reason=None,
    )


def get_log_source_status(source: LogSource) -> LogSourceStatus:
    """Resolve one source's current, safe-to-display status.

    Never raises: any filesystem error resolving or reading the target is
    reported as ``STATUS_UNAVAILABLE`` rather than propagating, since a log
    being unreadable must stay a diagnostic detail, not a request failure.

    The filesystem is probed unconditionally, even for a disabled source: a
    diagnostic source that was previously enabled and wrote a log keeps its
    size/modified-time here (and stays viewable in the Admin log viewer)
    after being disabled, rather than having that history discarded.
    """
    probe = _probe_path(source.path)
    base = dict(
        id=source.id,
        display_name=source.display_name,
        description=source.description,
        resolved_path=str(source.path),
    )
    rotation = (
        {"max_bytes": ROTATION_MAX_BYTES, "backup_count": ROTATION_BACKUP_COUNT}
        if probe.size_bytes is not None else None
    )

    if not source.is_enabled():
        reason = source.disabled_reason or "This log source is not enabled in the current configuration."
        if probe.size_bytes is not None:
            reason = f"{reason} A previously captured log is still available below."
        return LogSourceStatus(
            **base, enabled=False, status=STATUS_DISABLED, reason=reason,
            exists=probe.exists, size_bytes=probe.size_bytes, modified_at=probe.modified_at, rotation=rotation,
        )

    if probe.unavailable_reason is not None:
        return LogSourceStatus(
            **base, enabled=True, status=STATUS_UNAVAILABLE, reason=probe.unavailable_reason,
            exists=probe.exists, size_bytes=probe.size_bytes, modified_at=probe.modified_at, rotation=rotation,
        )

    if not probe.exists:
        return LogSourceStatus(
            **base, enabled=True, status=STATUS_NOT_CREATED,
            reason="Configured correctly; no log has been written yet.",
            exists=False, size_bytes=None, modified_at=None, rotation=None,
        )

    return LogSourceStatus(
        **base, enabled=True, status=STATUS_AVAILABLE, reason="",
        exists=True, size_bytes=probe.size_bytes, modified_at=probe.modified_at, rotation=rotation,
    )


def get_log_source_statuses() -> list[LogSourceStatus]:
    """Every known source's current status, in registry order."""
    return [get_log_source_status(source) for source in LOG_SOURCES.values()]
