"""Generic diagnostic evidence-capture framework.

This module owns everything that is *common* to diagnostic investigations:
profile registration, global/per-profile enablement, APScheduler job
registration (one interval job per enabled profile, never a shared master
tick), restart-safe re-registration on every scheduler startup, and generic
shutdown of profile resources.

It deliberately does **not** own anything investigation-specific: endpoint
definitions, candidate selection, payload parsing, transition detection, or
raw-retention policy all belong to individual profiles (see
``diagnostics/profiles/``). This is an internal project facility for adding
future diagnostic investigations cheaply, not a general plugin system --
checked-in profiles are the only supported extension point, never
configuration-driven code loading.

Only *approved, checked-in* profiles can ever run: ``AFL_DIAGNOSTIC_PROFILES``
selects among profiles already registered in this process via
``register_profile``, and can never name arbitrary code, URLs, or paths.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import config
from scheduler.registry import add_registered_job, diagnostic_profile_job_id
from utils.log import setup_logger
from logging_sources import LOG_SOURCES

_source = LOG_SOURCES["diagnostics_framework"]
log = setup_logger(_source.logger_name, _source.filename)


class DiagnosticProfile(ABC):
    """The small, explicit contract a diagnostic profile must implement.

    A profile owns investigation-specific behaviour only: what to poll, how
    to select candidates, how to parse/compare payloads, its default
    interval, and its raw-retention policy. It never registers its own
    APScheduler job, implements its own enable/disable check, or manages its
    own restart recovery -- the framework does all of that generically from
    ``name`` and the methods below.
    """

    #: Stable, lower_snake_case identifier. Used verbatim in
    #: ``AFL_DIAGNOSTIC_PROFILES``, the APScheduler job ID
    #: (``diagnostic_<name>``), and status reporting.
    name: str

    @abstractmethod
    def interval_seconds(self) -> int:
        """This profile's configured polling interval, in seconds.

        May raise ``ValueError`` if the profile's own configuration is
        invalid; the framework lets that propagate at registration time so
        misconfiguration is visible at startup rather than silently ignored.
        """

    @abstractmethod
    def run(self, *, now: datetime) -> list[dict[str, Any]]:
        """Execute one poll cycle and return a list of per-candidate outcomes.

        Must not raise for an individual candidate's failure -- a profile is
        expected to catch and log per-candidate errors itself (mirroring
        ``match_clock``'s per-match handling) so one bad candidate never
        aborts the rest of that profile's poll. The framework separately
        guarantees that one *profile* failing (an uncaught exception from
        this method) never affects any other profile, because each enabled
        profile is registered as its own independent APScheduler job.
        """

    def status(self) -> dict[str, Any]:
        """Read-only status for operator reporting; safe to call even when disabled."""
        selected = is_profile_selected(self.name)
        try:
            interval_seconds = self.interval_seconds()
            error = None
        except Exception as exc:  # noqa: BLE001 - reporting must never crash on bad config
            interval_seconds = None
            error = str(exc)
        result: dict[str, Any] = {"name": self.name, "selected": selected, "interval_seconds": interval_seconds}
        if error is not None:
            result["error"] = error
        return result

    def shutdown(self) -> None:
        """Release any pooled resources (for example an HTTP client). Default: nothing to do."""
        return None


_PROFILES: dict[str, DiagnosticProfile] = {}


def register_profile(profile: DiagnosticProfile) -> None:
    """Register a checked-in diagnostic profile. Raises on a duplicate name."""
    if profile.name in _PROFILES:
        raise ValueError(f"Diagnostic profile '{profile.name}' is already registered")
    _PROFILES[profile.name] = profile


def registered_profiles() -> dict[str, DiagnosticProfile]:
    """All checked-in profiles, keyed by name. Registration does not imply enablement."""
    return dict(_PROFILES)


def get_profile(name: str) -> DiagnosticProfile | None:
    return _PROFILES.get(name)


def diagnostics_enabled() -> bool:
    """Global kill switch for the whole diagnostics framework."""
    return bool(config.AFL_DIAGNOSTICS_ENABLED)


def enabled_profile_names() -> frozenset[str]:
    """Names selected via ``AFL_DIAGNOSTIC_PROFILES`` (independent of the global switch)."""
    return frozenset(config.AFL_DIAGNOSTIC_PROFILES)


def is_profile_selected(name: str) -> bool:
    """True only when diagnostics are globally enabled AND this profile is named.

    This is the single source of truth for profile enablement, shared by
    profile-internal code that gates itself when called directly (for
    example ``capture_live_match_state``) and by the framework's own
    scheduler registration below.
    """
    return diagnostics_enabled() and name in enabled_profile_names()


def run_profile(profile: DiagnosticProfile) -> list[dict[str, Any]]:
    """The callable registered with APScheduler for one profile's interval job.

    Re-checks enablement at run time (not just at registration time) so a
    profile disabled after the process started without a restart becomes an
    inert no-op on its next tick rather than continuing to poll.
    """
    if not is_profile_selected(profile.name):
        return []
    now = datetime.now(timezone.utc)
    return profile.run(now=now)


def register_diagnostic_profile_job(scheduler, profile: DiagnosticProfile) -> bool:
    """Register one profile's opt-in interval job. A no-op unless selected.

    This is the entire scheduler-registration surface a new profile needs:
    it owns job-ID naming, restart-safe re-registration via
    ``add_registered_job``, and the persisted registry row -- a profile
    author never writes APScheduler code directly.
    """
    if not is_profile_selected(profile.name):
        log.info(
            "Diagnostic profile '%s' not enabled (AFL_DIAGNOSTICS_ENABLED=%s, "
            "AFL_DIAGNOSTIC_PROFILES=%s); skipping registration.",
            profile.name, diagnostics_enabled(), sorted(enabled_profile_names()),
        )
        return False
    from apscheduler.triggers.interval import IntervalTrigger

    interval_seconds = profile.interval_seconds()
    job_id = diagnostic_profile_job_id(profile.name)

    # The live profile object is bound via closure, not passed as a
    # persisted job argument: scheduler_job_registry.args_json must be
    # JSON-serializable, and a profile instance is not.
    def _execute(profile: DiagnosticProfile = profile) -> list[dict[str, Any]]:
        return run_profile(profile)

    _execute.__name__ = f"run_diagnostic_profile_{profile.name}"
    add_registered_job(
        scheduler, _execute,
        trigger=IntervalTrigger(seconds=interval_seconds), args=[],
        job_id=job_id, job_type=f"diagnostic_{profile.name}",
        name=f"Diagnostic profile: {profile.name}",
        replace_existing=True, trigger_type="interval",
    )
    log.info("✅ Diagnostic profile '%s' enabled at %ss interval.", profile.name, interval_seconds)
    return True


def register_diagnostic_profiles(scheduler) -> dict[str, bool]:
    """Register every checked-in profile's job. Safe to call on every restart.

    One profile failing to register (for example ``interval_seconds()``
    raising ``ValueError`` for bad configuration) is logged and does not
    prevent any other profile from registering.
    """
    if not diagnostics_enabled():
        log.info("Diagnostics disabled (AFL_DIAGNOSTICS_ENABLED=false); skipping all profile registration.")
        return {}
    results: dict[str, bool] = {}
    for name, profile in registered_profiles().items():
        try:
            results[name] = register_diagnostic_profile_job(scheduler, profile)
        except Exception:
            log.exception("Failed to register diagnostic profile '%s'; other profiles unaffected.", name)
            results[name] = False
    return results


def shutdown_diagnostic_profiles() -> None:
    """Shut down every registered profile's resources. One failure does not block the rest."""
    for profile in registered_profiles().values():
        try:
            profile.shutdown()
        except Exception:
            log.exception("Diagnostic profile '%s' shutdown failed", profile.name)
