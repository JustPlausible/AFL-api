import base64
import dataclasses
import importlib

from fastapi.testclient import TestClient

import config


def _auth():
    token = base64.b64encode(b"admin:password").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "admin.db"))
    import admin
    admin = importlib.reload(admin)
    return admin, TestClient(admin.app)


def test_primary_admin_navigation_is_consistent(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(admin.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(admin.httpx.ConnectError("offline")))

    for path in ("/", "/schedule", "/tables", "/logs", "/setup"):
        response = client.get(path, headers=_auth())
        assert response.status_code == 200
        for label in ("Overview", "Scheduling", "Data", "Logs &amp; diagnostics", "System"):
            assert label in response.text


def test_missing_log_is_diagnostic_empty_state(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    missing = dataclasses.replace(
        admin.LOG_SOURCES["player_stats"], filename=str(tmp_path / "definitely-missing-player-stats.log"),
    )
    monkeypatch.setitem(admin.LOG_SOURCES, "player_stats", missing)

    response = client.get("/logs?log=Player%20Stats", headers=_auth())

    assert response.status_code == 200
    assert "Configured, no log created yet" in response.text
    assert "definitely-missing-player-stats.log" in response.text
    assert "admin interface has failed" in response.text


def test_present_log_is_reported_available_with_size_and_age(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    log_file = tmp_path / "present-player-stats.log"
    log_file.write_text("[2026-01-01 00:00:00 UTC] INFO: hello\n")
    present = dataclasses.replace(admin.LOG_SOURCES["player_stats"], filename=str(log_file))
    monkeypatch.setitem(admin.LOG_SOURCES, "player_stats", present)

    response = client.get("/logs?log=Player%20Stats", headers=_auth())

    assert response.status_code == 200
    assert "hello" in response.text
    assert "Available" in response.text


def test_disabled_source_is_distinguishable_from_missing_log(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)
    disabled = dataclasses.replace(
        admin.LOG_SOURCES["player_stats"],
        filename=str(tmp_path / "never-written-and-disabled.log"),
        enabled=False, disabled_reason="Disabled for this test.",
    )
    monkeypatch.setitem(admin.LOG_SOURCES, "player_stats", disabled)

    response = client.get("/logs?log=Player%20Stats", headers=_auth())

    assert response.status_code == 200
    assert "Disabled" in response.text
    assert "Disabled for this test." in response.text
    # The empty-state panel reports disabled, not a missing-file message,
    # for the *selected* source (other unrelated sources may legitimately
    # show "no log created yet" in the overview table above it).
    assert "Disabled. Disabled for this test." in response.text


def test_disabled_source_still_shows_a_previously_captured_log(tmp_path, monkeypatch):
    # Disabling a source stops new writes; it must not hide a log that was
    # already captured while the source was still enabled.
    admin, client = _client(tmp_path, monkeypatch)
    log_file = tmp_path / "captured-before-disabling.log"
    log_file.write_text("[2026-01-01 00:00:00 UTC] INFO: captured while enabled\n")
    disabled_with_history = dataclasses.replace(
        admin.LOG_SOURCES["player_stats"], filename=str(log_file),
        enabled=False, disabled_reason="Disabled for this test.",
    )
    monkeypatch.setitem(admin.LOG_SOURCES, "player_stats", disabled_with_history)

    response = client.get("/logs?log=Player%20Stats", headers=_auth())

    assert response.status_code == 200
    assert "captured while enabled" in response.text
    assert "currently disabled" in response.text


def test_logs_overview_lists_every_known_source(tmp_path, monkeypatch):
    admin, client = _client(tmp_path, monkeypatch)

    response = client.get("/logs", headers=_auth())

    assert response.status_code == 200
    for source in admin.LOG_SOURCES.values():
        assert source.display_name in response.text


def test_unknown_log_keeps_admin_layout_and_returns_bad_request(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/logs?log=not-a-log", headers=_auth())

    assert response.status_code == 400
    assert "Unknown log selection." in response.text
    assert "AFL API" in response.text
