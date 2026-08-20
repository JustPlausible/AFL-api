import base64
import importlib

from fastapi.testclient import TestClient

import config


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:password").decode()}


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "afl.db"
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    import admin
    admin = importlib.reload(admin)
    return admin, TestClient(admin.app)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_schedule_returns_200_with_persisted_only_null_func_job(tmp_path, monkeypatch):
    """Regression for #173: a persisted-only CFS polling attempt row with
    func_ref/func NULL must not raise TypeError when grouping/rendering."""
    admin, client = _client(tmp_path, monkeypatch)

    persisted_only_job = {
        "id": "cfs_poll_attempt_8230",
        "func": None,
        "next_run_time": None,
        "trigger": None,
        "apscheduler_state": "absent",
        "persisted": {
            "job_type": "cfs_player_stats_poll",
            "match_id": 8230,
            "round_id": None,
            "func_ref": None,
        },
        "persisted_status": "running",
        "persisted_job_type": "cfs_player_stats_poll",
        "persisted_last_attempt_time": "2026-08-15T10:00:00+00:00",
        "persisted_last_success_time": None,
        "persisted_attempt_count": 1,
        "persisted_last_error_summary": None,
    }

    monkeypatch.setattr(
        "admin.httpx.get",
        lambda *a, **k: _FakeResponse([persisted_only_job]),
    )

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert "cfs_poll_attempt_8230" in response.text
    # Persisted match metadata should drive grouping, not a func substring parse.
    assert "Match 8230" in response.text
    assert 'action="/scheduler/refresh" method="post"' in response.text
    assert 'name="csrf_token"' in response.text
    assert 'action="/scheduler/manual/injuries"' in response.text
    assert 'action="/scheduler/manual/fixtures_round"' in response.text
    assert 'action="/scheduler/manual/lineups_round"' in response.text
    assert 'action="/scheduler/manual/lineups_match"' in response.text
    assert 'action="/scheduler/manual/player_stats_match"' in response.text


def test_schedule_missing_args_and_no_func_does_not_raise(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)

    jobs = [
        {"id": "no_func_no_args", "apscheduler_state": "absent"},
        {"id": "empty_args", "func": "scraper.scrape_afl_fixtures:run_scraper", "args": []},
        {"id": "non_list_args", "func": "scraper.scrape_afl_fixtures:run_scraper", "args": "not-a-list"},
    ]
    monkeypatch.setattr("admin.httpx.get", lambda *a, **k: _FakeResponse(jobs))

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    for job in jobs:
        assert job["id"] in response.text


def test_schedule_unavailable_scheduler_still_renders_empty_page(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)

    def fake_get(*a, **k):
        raise admin.httpx.ConnectError("connection refused")

    monkeypatch.setattr("admin.httpx.get", fake_get)

    response = client.get("/schedule", headers=_auth())

    assert response.status_code == 200
    assert 'action="/scheduler/refresh" method="post"' in response.text
    assert 'name="csrf_token"' in response.text
