"""``interchange`` diagnostic profile: Issue #193 live matchInterchange evidence capture.

Following the ``match_clock`` reference profile's split, this module is a
thin adapter only: every investigation-specific behaviour -- the diagnostic
``matchInterchange`` endpoint definition, candidate selection, payload
parsing, transition detection, and raw-response retention -- lives in
``collection.match_interchange_evidence`` and
``scheduler.match_interchange_capture``. This module exists only to satisfy
the framework's ``DiagnosticProfile`` contract so the framework can own
scheduling, enablement, and restart-safe registration generically, the same
way it already does for ``match_clock``. Registering this profile here (and
in ``diagnostics/profiles/__init__.py``) is the entire scheduler-integration
surface it needs -- there is no interchange-specific APScheduler code
anywhere else in the project.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from diagnostics.framework import DiagnosticProfile
from scheduler.match_interchange_capture import (
    MATCH_INTERCHANGE_PROFILE_NAME,
    MatchInterchangeCaptureSettings,
    capture_live_match_interchange,
    shutdown_match_interchange_capture_client,
)


class InterchangeProfile(DiagnosticProfile):
    name = MATCH_INTERCHANGE_PROFILE_NAME

    def interval_seconds(self) -> int:
        return MatchInterchangeCaptureSettings.from_config().interval_seconds

    def run(self, *, now: datetime) -> list[dict[str, Any]]:
        return capture_live_match_interchange(clock=lambda: now)

    def status(self) -> dict[str, Any]:
        result = super().status()
        if "error" in result:
            # super().status() already tried to load settings (via
            # interval_seconds()) and caught the failure; don't call
            # from_config() again here and let the same exception escape
            # unguarded -- see MatchClockProfile.status() for the same guard.
            return result
        settings = MatchInterchangeCaptureSettings.from_config()
        result.update({
            "kickoff_tolerance_seconds": settings.kickoff_tolerance_seconds,
            "post_live_grace_seconds": settings.post_live_grace_seconds,
        })
        return result

    def shutdown(self) -> None:
        shutdown_match_interchange_capture_client()
