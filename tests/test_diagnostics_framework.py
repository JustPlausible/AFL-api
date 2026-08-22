"""Offline tests for the generic diagnostic evidence-capture framework.

Exercises profile registration, global/per-profile enablement, APScheduler
registration, execution isolation, and restart-safe re-registration using
small fake profiles (proving a new profile needs no bespoke scheduler code)
as well as the real ``match_clock`` profile (proving PR #175's Issue #148
investigation is registered generically through the framework).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from db.migration_runner import migrate_database
from diagnostics import framework
from diagnostics.framework import (
    DiagnosticProfile,
    register_diagnostic_profile_job,
    register_diagnostic_profiles,
    run_profile,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setenv("DB_PATH", str(path))
    import config
    monkeypatch.setattr(config, "DB_PATH", str(path), raising=False)
    migrate_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn, path
    conn.close()


class FakeProfile(DiagnosticProfile):
    """The minimal shape a new diagnostic profile needs: name, interval, run.

    No APScheduler imports, no job-ID construction, no persistence-lifecycle
    or restart code -- that is the point of this fixture.
    """

    def __init__(self, name, *, interval=10, on_run=None, on_interval=None):
        self.name = name
        self._interval = interval
        self._on_run = on_run
        self._on_interval = on_interval
        self.run_calls: list[datetime] = []

    def interval_seconds(self) -> int:
        if self._on_interval is not None:
            return self._on_interval()
        return self._interval

    def run(self, *, now: datetime):
        self.run_calls.append(now)
        if self._on_run is not None:
            return self._on_run(now)
        return []


class FakeScheduler:
    def __init__(self):
        self.jobs: list[dict] = []

    def add_job(self, func, *, trigger=None, args=None, id=None, name=None, replace_existing=True, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, "args": args, "id": id, "name": name})
        return SimpleNamespace(id=id)


def _enable(monkeypatch, *profile_names):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", tuple(profile_names), raising=False)


def _disable(monkeypatch):
    import config
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", False, raising=False)


@pytest.fixture
def isolated_registry(monkeypatch):
    """Fresh profile registry so registration tests don't collide with the
    real checked-in profiles (e.g. match_clock) registered elsewhere."""
    monkeypatch.setattr(framework, "_PROFILES", {})
    yield


# --- Profile registration ---------------------------------------------------

def test_register_profile_and_lookup(isolated_registry):
    profile = FakeProfile("widget")
    framework.register_profile(profile)
    assert framework.registered_profiles() == {"widget": profile}
    assert framework.get_profile("widget") is profile
    assert framework.get_profile("missing") is None


def test_register_profile_rejects_duplicate_name(isolated_registry):
    framework.register_profile(FakeProfile("widget"))
    with pytest.raises(ValueError):
        framework.register_profile(FakeProfile("widget"))


# --- Global diagnostics disabled behaviour ----------------------------------

def test_globally_disabled_blocks_a_named_profile(db, monkeypatch):
    _disable(monkeypatch)
    profile = FakeProfile("widget")
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, profile) is False
    assert scheduler.jobs == []
    assert run_profile(profile) == []
    assert profile.run_calls == []


def test_globally_disabled_blocks_register_diagnostic_profiles(db, monkeypatch, isolated_registry):
    framework.register_profile(FakeProfile("widget"))
    _disable(monkeypatch)
    scheduler = FakeScheduler()
    assert register_diagnostic_profiles(scheduler) == {}
    assert scheduler.jobs == []


# --- Individual profile enable/disable behaviour ----------------------------

def test_profile_not_selected_is_not_registered_even_when_diagnostics_enabled(db, monkeypatch):
    _enable(monkeypatch, "some_other_profile")
    profile = FakeProfile("widget")
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, profile) is False
    assert scheduler.jobs == []


def test_profile_selected_and_enabled_is_registered(db, monkeypatch):
    _enable(monkeypatch, "widget")
    profile = FakeProfile("widget", interval=30)
    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, profile) is True
    assert len(scheduler.jobs) == 1
    assert scheduler.jobs[0]["id"] == "diagnostic_widget"


def test_run_profile_only_calls_run_when_selected(db, monkeypatch):
    profile = FakeProfile("widget", on_run=lambda now: [{"observed_at": now.isoformat()}])

    _disable(monkeypatch)
    assert run_profile(profile) == []
    assert profile.run_calls == []

    _enable(monkeypatch, "widget")
    results = run_profile(profile)
    assert len(results) == 1
    assert len(profile.run_calls) == 1


# --- Independently configurable profile interval ----------------------------

def test_two_profiles_use_independent_intervals(db, monkeypatch, isolated_registry):
    fast = FakeProfile("fast_profile", interval=5)
    slow = FakeProfile("slow_profile", interval=300)
    framework.register_profile(fast)
    framework.register_profile(slow)
    _enable(monkeypatch, "fast_profile", "slow_profile")
    scheduler = FakeScheduler()
    results = register_diagnostic_profiles(scheduler)
    assert results == {"fast_profile": True, "slow_profile": True}
    by_id = {job["id"]: job for job in scheduler.jobs}
    assert by_id["diagnostic_fast_profile"]["trigger"].interval.total_seconds() == 5
    assert by_id["diagnostic_slow_profile"]["trigger"].interval.total_seconds() == 300


# --- One profile failing does not break others ------------------------------

def test_one_profile_failing_registration_does_not_block_another(db, monkeypatch, isolated_registry):
    def _boom():
        raise ValueError("bad interval config")

    broken = FakeProfile("broken_profile", on_interval=_boom)
    good = FakeProfile("good_profile", interval=15)
    framework.register_profile(broken)
    framework.register_profile(good)
    _enable(monkeypatch, "broken_profile", "good_profile")
    scheduler = FakeScheduler()
    results = register_diagnostic_profiles(scheduler)
    assert results == {"broken_profile": False, "good_profile": True}
    assert [job["id"] for job in scheduler.jobs] == ["diagnostic_good_profile"]


def test_one_profile_failing_at_run_time_does_not_affect_another(db, monkeypatch):
    def _explode(now):
        raise RuntimeError("upstream boom")

    failing = FakeProfile("failing_profile", on_run=_explode)
    fine = FakeProfile("fine_profile", on_run=lambda now: [{"ok": True}])
    _enable(monkeypatch, "failing_profile", "fine_profile")

    with pytest.raises(RuntimeError):
        run_profile(failing)
    # Each profile is its own independent APScheduler job/callable; the other
    # profile's run is completely unaffected by the first one raising.
    assert run_profile(fine) == [{"ok": True}]


# --- Restart-safe operation --------------------------------------------------

def test_reregistering_the_same_profile_does_not_duplicate_the_registry_row(db, monkeypatch):
    conn, _ = db
    _enable(monkeypatch, "widget")
    profile = FakeProfile("widget", interval=10)
    scheduler = FakeScheduler()

    assert register_diagnostic_profile_job(scheduler, profile) is True
    assert register_diagnostic_profile_job(scheduler, profile) is True  # simulated restart

    count = conn.execute(
        "SELECT COUNT(*) FROM scheduler_job_registry WHERE job_id=?", ("diagnostic_widget",)
    ).fetchone()[0]
    assert count == 1


def test_register_diagnostic_profiles_is_idempotent_across_simulated_restarts(db, monkeypatch, isolated_registry):
    framework.register_profile(FakeProfile("widget", interval=10))
    _enable(monkeypatch, "widget")
    scheduler = FakeScheduler()
    assert register_diagnostic_profiles(scheduler) == {"widget": True}
    assert register_diagnostic_profiles(scheduler) == {"widget": True}


# --- shutdown isolation -------------------------------------------------

def test_shutdown_diagnostic_profiles_calls_every_profile_even_if_one_raises(isolated_registry):
    shut_down: list[str] = []

    class ExplodingProfile(FakeProfile):
        def shutdown(self) -> None:
            raise RuntimeError("resource cleanup boom")

    class TrackingProfile(FakeProfile):
        def shutdown(self) -> None:
            shut_down.append(self.name)

    framework.register_profile(ExplodingProfile("broken_profile"))
    framework.register_profile(TrackingProfile("good_profile"))

    framework.shutdown_diagnostic_profiles()  # must not raise
    assert shut_down == ["good_profile"]


# --- match_clock registers generically through the framework ----------------

def test_match_clock_profile_registers_via_generic_framework(db, monkeypatch):
    import config
    _enable(monkeypatch, "match_clock")
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", 20, raising=False)
    from diagnostics.profiles.match_clock import MatchClockProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is True
    job = scheduler.jobs[0]
    assert job["id"] == "diagnostic_match_clock"
    assert job["trigger"].interval.total_seconds() == 20


def test_match_clock_profile_not_registered_when_diagnostics_disabled(db, monkeypatch):
    _disable(monkeypatch)
    from diagnostics.profiles.match_clock import MatchClockProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is False
    assert scheduler.jobs == []


def test_checked_in_profiles_include_match_clock():
    import diagnostics.profiles  # noqa: F401 - registers checked-in profiles
    assert "match_clock" in framework.registered_profiles()


# --- interchange registers generically through the framework, independently
# of match_clock (Issue #193) -------------------------------------------

def test_checked_in_profiles_include_interchange():
    import diagnostics.profiles  # noqa: F401 - registers checked-in profiles
    assert "interchange" in framework.registered_profiles()


def test_interchange_profile_registers_via_generic_framework(db, monkeypatch):
    import config
    _enable(monkeypatch, "interchange")
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS", 25, raising=False)
    from diagnostics.profiles.interchange import InterchangeProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is True
    job = scheduler.jobs[0]
    assert job["id"] == "diagnostic_interchange"
    assert job["trigger"].interval.total_seconds() == 25


def test_interchange_profile_not_registered_when_diagnostics_disabled(db, monkeypatch):
    _disable(monkeypatch)
    from diagnostics.profiles.interchange import InterchangeProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is False
    assert scheduler.jobs == []


def test_match_clock_and_interchange_both_register_when_both_selected(db, monkeypatch):
    """Both checked-in profiles are independently selectable/schedulable
    together -- neither one's registration depends on the other."""
    _enable(monkeypatch, "match_clock", "interchange")
    from diagnostics.profiles.interchange import InterchangeProfile
    from diagnostics.profiles.match_clock import MatchClockProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is True
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is True
    assert {job["id"] for job in scheduler.jobs} == {"diagnostic_match_clock", "diagnostic_interchange"}


# --- commentary registers generically through the framework, independently
# of match_clock and interchange (Issue #196) --------------------------------

def test_checked_in_profiles_include_commentary():
    import diagnostics.profiles  # noqa: F401 - registers checked-in profiles
    assert "commentary" in framework.registered_profiles()


def test_commentary_profile_registers_via_generic_framework(db, monkeypatch):
    import config
    _enable(monkeypatch, "commentary")
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_COMMENTARY_INTERVAL_SECONDS", 25, raising=False)
    from diagnostics.profiles.commentary import CommentaryProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is True
    job = scheduler.jobs[0]
    assert job["id"] == "diagnostic_commentary"
    assert job["trigger"].interval.total_seconds() == 25


def test_commentary_profile_not_registered_when_diagnostics_disabled(db, monkeypatch):
    _disable(monkeypatch)
    from diagnostics.profiles.commentary import CommentaryProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is False
    assert scheduler.jobs == []


def test_all_three_checked_in_profiles_register_together(db, monkeypatch):
    """match_clock, interchange and commentary are independently selectable
    and independently schedulable, all together -- the configuration shown
    in docs/diagnostics_framework.md and Issue #196."""
    _enable(monkeypatch, "match_clock", "interchange", "commentary")
    from diagnostics.profiles.commentary import CommentaryProfile
    from diagnostics.profiles.interchange import InterchangeProfile
    from diagnostics.profiles.match_clock import MatchClockProfile

    scheduler = FakeScheduler()
    assert register_diagnostic_profile_job(scheduler, MatchClockProfile()) is True
    assert register_diagnostic_profile_job(scheduler, InterchangeProfile()) is True
    assert register_diagnostic_profile_job(scheduler, CommentaryProfile()) is True
    assert {job["id"] for job in scheduler.jobs} == {
        "diagnostic_match_clock", "diagnostic_interchange", "diagnostic_commentary",
    }


def test_commentary_profile_status_reports_error_instead_of_raising_for_bad_settings(monkeypatch):
    import config
    from diagnostics.profiles.commentary import CommentaryProfile
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_COMMENTARY_POST_LIVE_GRACE_SECONDS", -1, raising=False)

    status = CommentaryProfile().status()
    assert status["name"] == "commentary"
    assert status["interval_seconds"] is None
    assert "error" in status
    assert "kickoff_tolerance_seconds" not in status
    assert "post_live_grace_seconds" not in status


def test_interchange_profile_status_reports_error_instead_of_raising_for_bad_settings(monkeypatch):
    import config
    from diagnostics.profiles.interchange import InterchangeProfile
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS", -1, raising=False)

    status = InterchangeProfile().status()
    assert status["name"] == "interchange"
    assert status["interval_seconds"] is None
    assert "error" in status
    assert "kickoff_tolerance_seconds" not in status
    assert "post_live_grace_seconds" not in status


def test_match_clock_profile_status_reports_error_instead_of_raising_for_bad_settings(monkeypatch):
    """Regression test: MatchClockProfile.status() used to call
    MatchStateCaptureSettings.from_config() a second time, unguarded, after
    the base status() had already caught the same ValueError -- so a bad
    setting (e.g. a negative grace window) 500'd GET /scheduler/diagnostics
    instead of reporting the error, exactly when an operator needs it."""
    import config
    from diagnostics.profiles.match_clock import MatchClockProfile
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_MATCH_CLOCK_POST_LIVE_GRACE_SECONDS", -1, raising=False)

    status = MatchClockProfile().status()
    assert status["name"] == "match_clock"
    assert status["interval_seconds"] is None
    assert "error" in status
    assert "kickoff_tolerance_seconds" not in status
    assert "post_live_grace_seconds" not in status


# --- Status reporting never crashes on bad profile config -------------------

def test_status_reports_error_instead_of_raising_for_bad_profile_settings(db, monkeypatch):
    def _boom():
        raise ValueError("bad interval config")

    profile = FakeProfile("broken_profile", on_interval=_boom)
    status = profile.status()
    assert status["name"] == "broken_profile"
    assert status["interval_seconds"] is None
    assert "bad interval config" in status["error"]
