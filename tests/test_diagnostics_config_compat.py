"""Tests for the diagnostics framework's backward compatibility with the
PR #175 (Issue #148) configuration names that predate the framework.

These exercise the pure resolution helpers in ``config.py`` directly against
a monkeypatched environment, rather than reloading the ``config`` module --
the helpers read ``os.getenv`` at call time (like the rest of config.py's
parsers), so no module reload is needed and no shared module state leaks
between tests.
"""
from __future__ import annotations

import config


def test_new_diagnostics_enabled_name_wins_when_both_set(monkeypatch):
    monkeypatch.setenv("AFL_DIAGNOSTICS_ENABLED", "false")
    monkeypatch.setenv("AFL_CAPTURE_MATCH_STATE_EVIDENCE", "true")
    assert config._bool_env_with_legacy_fallback(
        "AFL_DIAGNOSTICS_ENABLED", "AFL_CAPTURE_MATCH_STATE_EVIDENCE", False
    ) is False


def test_legacy_capture_flag_enables_diagnostics_when_new_name_unset(monkeypatch):
    monkeypatch.delenv("AFL_DIAGNOSTICS_ENABLED", raising=False)
    monkeypatch.setenv("AFL_CAPTURE_MATCH_STATE_EVIDENCE", "true")
    assert config._bool_env_with_legacy_fallback(
        "AFL_DIAGNOSTICS_ENABLED", "AFL_CAPTURE_MATCH_STATE_EVIDENCE", False
    ) is True


def test_diagnostics_enabled_defaults_false_when_neither_name_set(monkeypatch):
    monkeypatch.delenv("AFL_DIAGNOSTICS_ENABLED", raising=False)
    monkeypatch.delenv("AFL_CAPTURE_MATCH_STATE_EVIDENCE", raising=False)
    assert config._bool_env_with_legacy_fallback(
        "AFL_DIAGNOSTICS_ENABLED", "AFL_CAPTURE_MATCH_STATE_EVIDENCE", False
    ) is False


def test_legacy_capture_flag_selects_match_clock_profile_by_default(monkeypatch):
    monkeypatch.delenv("AFL_DIAGNOSTIC_PROFILES", raising=False)
    monkeypatch.setenv("AFL_CAPTURE_MATCH_STATE_EVIDENCE", "true")
    assert config._diagnostic_profiles_with_legacy_fallback(
        "AFL_DIAGNOSTIC_PROFILES", "AFL_CAPTURE_MATCH_STATE_EVIDENCE"
    ) == ("match_clock",)


def test_explicit_diagnostic_profiles_wins_over_legacy_capture_flag(monkeypatch):
    monkeypatch.setenv("AFL_DIAGNOSTIC_PROFILES", "some_other_profile")
    monkeypatch.setenv("AFL_CAPTURE_MATCH_STATE_EVIDENCE", "true")
    assert config._diagnostic_profiles_with_legacy_fallback(
        "AFL_DIAGNOSTIC_PROFILES", "AFL_CAPTURE_MATCH_STATE_EVIDENCE"
    ) == ("some_other_profile",)


def test_diagnostic_profiles_empty_when_neither_name_set(monkeypatch):
    monkeypatch.delenv("AFL_DIAGNOSTIC_PROFILES", raising=False)
    monkeypatch.delenv("AFL_CAPTURE_MATCH_STATE_EVIDENCE", raising=False)
    assert config._diagnostic_profiles_with_legacy_fallback(
        "AFL_DIAGNOSTIC_PROFILES", "AFL_CAPTURE_MATCH_STATE_EVIDENCE"
    ) == ()


def test_new_interval_name_wins_when_both_set(monkeypatch):
    monkeypatch.setenv("AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", "99")
    assert config._int_env_with_legacy_fallback(
        "AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 15
    ) == 30


def test_legacy_interval_used_when_new_name_unset(monkeypatch):
    monkeypatch.delenv("AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv("AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", "99")
    assert config._int_env_with_legacy_fallback(
        "AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 15
    ) == 99


def test_interval_falls_back_to_default_when_neither_name_set(monkeypatch):
    monkeypatch.delenv("AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", raising=False)
    assert config._int_env_with_legacy_fallback(
        "AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS", "AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS", 15
    ) == 15


def test_module_level_diagnostics_defaults_are_disabled():
    """A fresh checkout with no diagnostics env vars set resolves to disabled."""
    import os
    assert "AFL_DIAGNOSTICS_ENABLED" not in os.environ
    assert "AFL_CAPTURE_MATCH_STATE_EVIDENCE" not in os.environ
    assert config.AFL_DIAGNOSTICS_ENABLED is False
    assert config.AFL_DIAGNOSTIC_PROFILES == ()
