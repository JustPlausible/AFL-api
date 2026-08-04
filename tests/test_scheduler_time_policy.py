import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

import config
from db.connection import get_db_connection
from db.migration_runner import migrate_database
from scheduler import registry
from scheduler.schedule_match_scrapes import _today_match_ids, register_live_match_day_scraper
from scheduler.schedule_stat_scrapes import register_stat_scrape_jobs
from scheduler.time_policy import (
    MetadataTimestampError,
    match_day_bounds,
    parse_metadata_timestamp,
)


def _db(tmp_path, monkeypatch):
    path = tmp_path / "afl.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    migrate_database(path)
    return path


def _insert_match(match_id, start_time, status="UPCOMING"):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO matches (match_id, round_id, start_time_utc, status) VALUES (?, 1, ?, ?)",
            (match_id, start_time, status),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2030-01-02T03:04:05Z", "2030-01-02T03:04:05+00:00"),
        ("2030-01-02T11:04:05+08:00", "2030-01-02T03:04:05+00:00"),
        ("2030-01-01T22:04:05-05:00", "2030-01-02T03:04:05+00:00"),
    ],
)
def test_metadata_timestamp_offsets_are_converted_to_utc(raw, expected):
    assert parse_metadata_timestamp(raw).isoformat() == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "timestamp_missing"),
        ("", "timestamp_missing"),
        ("2030-01-02T03:04:05", "timestamp_naive"),
        ("not-a-date", "timestamp_malformed"),
    ],
)
def test_metadata_timestamp_rejects_ambiguous_or_invalid_values(raw, reason):
    with pytest.raises(MetadataTimestampError) as caught:
        parse_metadata_timestamp(raw)
    assert caught.value.reason_code == reason


def test_match_day_bounds_cross_utc_calendar_day():
    start, end = match_day_bounds(
        datetime(2030, 1, 2, 7, 0, tzinfo=timezone.utc),
        zone=ZoneInfo("Australia/Perth"),
    )
    assert start == datetime(2030, 1, 1, 16, 0, tzinfo=timezone.utc)
    assert end == datetime(2030, 1, 2, 16, 0, tzinfo=timezone.utc)


def test_representative_cfs_fixture_aware_date_has_expected_utc_instant_and_match_day():
    fixture = json.loads(
        Path("tests/fixtures/afl/match_item/match_item_8216_concluded.json").read_text(encoding="utf-8")
    )

    instant = parse_metadata_timestamp(fixture["match"]["date"])
    start, end = match_day_bounds(instant, zone=ZoneInfo("Australia/Perth"))

    assert instant == datetime(2026, 7, 23, 9, 30, tzinfo=timezone.utc)
    assert fixture["match"]["utcStartTime"] == "2026-07-23T09:30:00"
    assert start <= instant < end
    assert instant.astimezone(ZoneInfo("Australia/Perth")).date().isoformat() == "2026-07-23"


def test_match_day_selection_handles_midnight_boundaries_and_offsets(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AFL_MATCH_DAY_TIMEZONE", "Australia/Perth")
    _insert_match(1, "2030-01-01T15:59:59Z")
    _insert_match(2, "2030-01-01T16:00:00Z")
    _insert_match(3, "2030-01-03T00:00:00+08:00")

    conn = get_db_connection()
    try:
        assert _today_match_ids(conn, datetime(2030, 1, 2, 12, tzinfo=timezone.utc)) == [2]
    finally:
        conn.close()


def test_match_day_selection_is_independent_of_host_timezone(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AFL_MATCH_DAY_TIMEZONE", "Australia/Perth")
    _insert_match(1, "2030-01-01T16:30:00Z")
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        conn = get_db_connection()
        try:
            assert _today_match_ids(conn, datetime(2030, 1, 2, tzinfo=timezone.utc)) == [1]
        finally:
            conn.close()
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_valid_offset_one_shot_keeps_job_identity_and_correct_instant(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    _insert_match(42, "2030-01-02T11:00:00+08:00")
    scheduler = BackgroundScheduler(timezone=timezone.utc)

    register_stat_scrape_jobs(scheduler)

    job = scheduler.get_job("stats_match_42")
    assert job is not None
    row = registry.registry_rows()[0]
    assert row["scheduled_run_time"] == "2030-01-02T03:00:10+00:00"
    assert row["status"] == registry.PENDING


@pytest.mark.parametrize(
    ("raw", "reason"),
    [(None, "timestamp_missing"), ("bad", "timestamp_malformed"), ("2030-01-02T03:00:00", "timestamp_naive")],
)
def test_stat_planning_error_is_durable_and_queryable(tmp_path, monkeypatch, raw, reason):
    _db(tmp_path, monkeypatch)
    _insert_match(42, raw)

    register_stat_scrape_jobs(BackgroundScheduler(timezone=timezone.utc))

    row = registry.registry_rows()[0]
    assert row["job_id"] == "stats_match_42"
    assert row["status"] == registry.FAILED
    assert row["attempt_count"] == 0
    assert row["last_error_summary"] == f"planning_failed:{reason}"


def test_match_day_registration_uses_application_selection_not_sqlite_localtime(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "AFL_MATCH_DAY_TIMEZONE", "Australia/Perth")
    _insert_match(7, "2030-01-01T16:00:00Z")
    scheduler = BackgroundScheduler(timezone=timezone.utc)

    register_live_match_day_scraper(scheduler, datetime(2030, 1, 2, tzinfo=timezone.utc))

    assert scheduler.get_job("match_refresh_match_day") is not None
