"""``match_clock`` diagnostic profile: Issue #148 live matchItem evidence capture.

This is the framework's reference profile and the first checked-in
investigation. It is a thin adapter only: every investigation-specific
behaviour -- the diagnostic ``matchItem`` endpoint definition, live/kickoff/
post-live candidate selection, payload parsing, transition detection, and
selective raw-response retention -- remains in ``collection.match_state_evidence``
and ``scheduler.match_state_capture``, unchanged from the live-tested PR #175
implementation. This module exists only to satisfy the framework's
``DiagnosticProfile`` contract so the framework can own scheduling,
enablement, and restart-safe registration generically.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from diagnostics.framework import DiagnosticProfile
from scheduler.match_state_capture import (
    MATCH_CLOCK_PROFILE_NAME,
    MatchStateCaptureSettings,
    capture_live_match_state,
    shutdown_match_state_capture_client,
)


class MatchClockProfile(DiagnosticProfile):
    name = MATCH_CLOCK_PROFILE_NAME

    def interval_seconds(self) -> int:
        return MatchStateCaptureSettings.from_config().interval_seconds

    def run(self, *, now: datetime) -> list[dict[str, Any]]:
        return capture_live_match_state(clock=lambda: now)

    def status(self) -> dict[str, Any]:
        result = super().status()
        if "error" in result:
            # super().status() already tried to load settings (via
            # interval_seconds()) and caught the failure; don't call
            # from_config() again here and let the same exception escape
            # unguarded -- that would 500 GET /scheduler/diagnostics right
            # when an operator needs it to explain the bad configuration.
            return result
        settings = MatchStateCaptureSettings.from_config()
        result.update({
            "kickoff_tolerance_seconds": settings.kickoff_tolerance_seconds,
            "post_live_grace_seconds": settings.post_live_grace_seconds,
        })
        return result

    def shutdown(self) -> None:
        shutdown_match_state_capture_client()
