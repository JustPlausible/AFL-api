import base64
import importlib
import re
import sqlite3
import time

from fastapi.testclient import TestClient

import config
from db.migration_runner import migrate_database


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"admin:password").decode()}


def _client(tmp_path, monkeypatch):
    db_path = tmp_path / "afl.db"
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    migrate_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO rounds(round_id, round_label) VALUES (1, 'Round 1')")
    conn.execute("INSERT INTO matches(match_id, round_id, home_team, away_team) VALUES (10, 1, 'A', 'B')")
    conn.commit(); conn.close()
    import admin
    admin = importlib.reload(admin)
    return TestClient(admin.app), db_path


def _token(client):
    r = client.get("/schedule", headers=_auth())
    assert r.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)


def test_manual_trigger_forms_render_with_csrf(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    html = client.get("/schedule", headers=_auth()).text
    for action in ["injuries", "fixtures_round", "lineups_round", "lineups_match", "player_stats_match"]:
        assert f'action="/scheduler/manual/{action}"' in html
    assert html.count('name="csrf_token"') >= 6
    assert "explicit once-only stats refresh" in html


def test_authenticated_success_for_each_trigger_calls_narrow_scheduler_endpoint(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    calls = []
    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"status": "queued", "job_id": "admin_manual_test", "trigger_source": "admin_manual"}
        return R()
    monkeypatch.setattr("admin.httpx.post", fake_post)
    token = _token(client)
    cases = [
        ("injuries", {}),
        ("fixtures_round", {"round_id": "1"}),
        ("lineups_round", {"round_id": "1"}),
        ("lineups_match", {"match_id": "10"}),
        ("player_stats_match", {"match_id": "10"}),
    ]
    for kind, data in cases:
        resp = client.post(f"/scheduler/manual/{kind}", headers=_auth(), data={**data, "csrf_token": token})
        assert resp.status_code == 200
        assert "queued" in resp.text
    assert [c[0] for c in calls] == [
        "http://afl-scheduler:8000/scheduler/manual/injuries",
        "http://afl-scheduler:8000/scheduler/manual/fixtures/round",
        "http://afl-scheduler:8000/scheduler/manual/lineups/round",
        "http://afl-scheduler:8000/scheduler/manual/lineups/match",
        "http://afl-scheduler:8000/scheduler/manual/player-stats/match",
    ]
    assert calls[-1][1] == {"match_id": 10}


def test_unauthenticated_and_csrf_rejection_for_each_trigger(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    called = False
    def fake_post(*a, **k):
        nonlocal called; called = True
    monkeypatch.setattr("admin.httpx.post", fake_post)
    for kind in ["injuries", "fixtures_round", "lineups_round", "lineups_match", "player_stats_match"]:
        assert client.post(f"/scheduler/manual/{kind}").status_code == 401
        assert client.post(f"/scheduler/manual/{kind}", headers=_auth(), data={}).status_code in (403, 422)
        assert client.post(f"/scheduler/manual/{kind}", headers=_auth(), data={"csrf_token": "bad"}).status_code == 403
    assert called is False


def test_invalid_identifiers_do_not_call_scheduler(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    called = False
    monkeypatch.setattr("admin.httpx.post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    token = _token(client)
    invalid = [
        ("fixtures_round", {}),
        ("fixtures_round", {"round_id": "abc"}),
        ("fixtures_round", {"round_id": "0"}),
        ("fixtures_round", {"round_id": "-1"}),
        ("fixtures_round", {"round_id": "999"}),
        ("lineups_match", {}),
        ("lineups_match", {"match_id": "abc"}),
        ("lineups_match", {"match_id": "0"}),
        ("lineups_match", {"match_id": "-1"}),
        ("lineups_match", {"match_id": "999"}),
    ]
    for kind, data in invalid:
        resp = client.post(f"/scheduler/manual/{kind}", headers=_auth(), data={**data, "csrf_token": token})
        assert resp.status_code == 422


def test_admin_route_returns_promptly_and_does_not_run_scraper_functions(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    def fake_post(url, json, timeout):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"status": "queued", "job_id": "admin_manual_fast"}
        return R()
    monkeypatch.setattr("admin.httpx.post", fake_post)
    token = _token(client)
    start = time.monotonic()
    resp = client.post("/scheduler/manual/player_stats_match", headers=_auth(), data={"match_id": "10", "csrf_token": token})
    assert resp.status_code == 200
    assert time.monotonic() - start < 1


def test_scheduler_unavailable_message_is_sanitized(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    import httpx
    monkeypatch.setattr("admin.httpx.post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("token=secret")))
    resp = client.post("/scheduler/manual/injuries", headers=_auth(), data={"csrf_token": _token(client)})
    assert resp.status_code == 503
    assert "unavailable" in resp.text
    assert "secret" not in resp.text and "Traceback" not in resp.text
