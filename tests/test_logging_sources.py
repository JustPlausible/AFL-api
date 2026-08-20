import dataclasses

import config
import logging_sources
from logging_sources import (
    STATUS_AVAILABLE, STATUS_DISABLED, STATUS_NOT_CREATED, STATUS_UNAVAILABLE,
    LogSource, get_log_source_status, get_log_source_statuses,
)


def _source(tmp_path, **overrides):
    base = dict(
        id="test_source",
        display_name="Test Source",
        description="A source used only in tests.",
        logger_name="test_source_logger",
        filename=str(tmp_path / "test_source.log"),
    )
    base.update(overrides)
    return LogSource(**base)


def test_configured_and_present_reports_available_with_metadata(tmp_path):
    log_file = tmp_path / "present.log"
    log_file.write_text("line one\nline two\n")
    source = _source(tmp_path, filename=str(log_file))

    status = get_log_source_status(source)

    assert status.status == STATUS_AVAILABLE
    assert status.enabled is True
    assert status.exists is True
    assert status.size_bytes == log_file.stat().st_size
    assert status.modified_at is not None
    assert status.rotation == {
        "max_bytes": logging_sources.ROTATION_MAX_BYTES,
        "backup_count": logging_sources.ROTATION_BACKUP_COUNT,
    }


def test_configured_and_missing_is_not_created_not_disabled(tmp_path):
    source = _source(tmp_path, filename=str(tmp_path / "never-written.log"))

    status = get_log_source_status(source)

    assert status.status == STATUS_NOT_CREATED
    assert status.enabled is True
    assert status.exists is False
    assert status.size_bytes is None
    assert status.modified_at is None
    # Distinguishable from disabled: this source is enabled, just empty.
    assert status.status != STATUS_DISABLED


def test_disabled_source_does_not_report_a_missing_file_error(tmp_path):
    source = _source(tmp_path, enabled=False, disabled_reason="Turned off for testing.")

    status = get_log_source_status(source)

    assert status.status == STATUS_DISABLED
    assert status.enabled is False
    assert status.reason == "Turned off for testing."
    assert status.exists is False


def test_disabled_source_preserves_metadata_for_a_previously_captured_log(tmp_path):
    # Disabling a source stops future writes; it must not discard the size
    # and modified-time of a log the source already wrote while enabled.
    log_file = tmp_path / "captured-before-disabling.log"
    log_file.write_text("captured while enabled\n")
    source = _source(
        tmp_path, filename=str(log_file), enabled=False, disabled_reason="Turned off for testing.",
    )

    status = get_log_source_status(source)

    assert status.status == STATUS_DISABLED
    assert status.exists is True
    assert status.size_bytes == log_file.stat().st_size
    assert status.modified_at is not None
    assert "previously captured log is still available" in status.reason


def test_expected_log_path_that_is_a_directory_is_unavailable_not_available(tmp_path):
    a_directory = tmp_path / "not-a-file.log"
    a_directory.mkdir()
    source = _source(tmp_path, filename=str(a_directory))

    status = get_log_source_status(source)

    assert status.status == STATUS_UNAVAILABLE
    assert status.size_bytes is None
    assert "not a regular file" in status.reason


def test_disabled_via_callable_is_reevaluated_each_call(tmp_path):
    flag = {"on": False}
    source = _source(tmp_path, enabled=lambda: flag["on"])

    assert get_log_source_status(source).status == STATUS_DISABLED

    flag["on"] = True
    assert get_log_source_status(source).status == STATUS_NOT_CREATED


def test_path_resolution_joins_log_dir_and_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_sources, "LOG_DIR", str(tmp_path))
    source = LogSource(
        id="x", display_name="X", description="", logger_name="x", filename="x.log",
    )

    assert str(source.path) == str(tmp_path / "x.log")


def test_expected_log_directory_unavailable_is_distinguishable_from_missing_file(tmp_path):
    missing_dir = tmp_path / "does-not-exist"
    source = _source(tmp_path, filename=str(missing_dir / "x.log"))

    status = get_log_source_status(source)

    assert status.status == STATUS_UNAVAILABLE
    assert "not accessible" in status.reason


def test_match_state_capture_source_is_disabled_by_default():
    assert config.AFL_DIAGNOSTICS_ENABLED is False
    source = logging_sources.LOG_SOURCES["match_state_capture"]

    status = get_log_source_status(source)

    assert status.status == STATUS_DISABLED
    # A real match_state_capture.log may already exist in this shared "logs"
    # directory from another test/process importing the module; either way
    # the reason must start with the static disabled explanation.
    assert status.reason.startswith(source.disabled_reason)


def test_match_state_capture_source_reflects_enabled_diagnostics(monkeypatch):
    monkeypatch.setattr(config, "AFL_DIAGNOSTICS_ENABLED", True)
    monkeypatch.setattr(config, "AFL_DIAGNOSTIC_PROFILES", ("match_clock",))
    source = logging_sources.LOG_SOURCES["match_state_capture"]

    status = get_log_source_status(source)

    assert status.status in (STATUS_NOT_CREATED, STATUS_AVAILABLE)
    assert status.status != STATUS_DISABLED


def test_registry_does_not_leak_arbitrary_paths():
    # Every registered source resolves under LOG_DIR; nothing in the registry
    # accepts an operator/user-supplied filename.
    for source in logging_sources.LOG_SOURCES.values():
        assert ".." not in source.filename
        assert not source.filename.startswith("/")


def test_get_log_source_statuses_covers_every_registered_source():
    statuses = get_log_source_statuses()
    assert {s.id for s in statuses} == set(logging_sources.LOG_SOURCES.keys())


def test_registry_includes_actively_written_scheduler_and_diagnostics_sources():
    # These modules write their log unconditionally whenever the scheduler
    # process runs (scheduler/start.py, scheduler/registry.py,
    # scheduler/scheduled_tasks.py + job_cleaner.py, schedule_match_scrapes,
    # schedule_lineup_scrapes, diagnostics/framework.py) -- the registry
    # should not silently omit them alongside the scraper-triggered ones.
    assert set(logging_sources.LOG_SOURCES.keys()) >= {
        "player_stats", "injuries", "lineups", "matches", "scheduler_jobs",
        "scheduler_start", "scheduler_registry", "scheduled_tasks",
        "refresh_live_matches", "refresh_afl_lineups", "diagnostics_framework",
        "match_state_capture",
    }
