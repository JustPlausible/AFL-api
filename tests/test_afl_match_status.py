from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from afl_json.client import AflJsonHttpError
from afl_json.match_status import (
    later_match_status, normalise_match_status, reconcile_match_status,
)


FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"
PROVIDER_ID = "CD_M20260142007"


class DetailClient:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.payload)


def database(status="POSTGAME", match_id=8207):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE matches (match_id INTEGER, match_provider_id TEXT, status TEXT, updated_at TEXT, scraped_at TEXT)")
    conn.execute("INSERT INTO matches VALUES (?, ?, ?, 'old-update', 'metadata-scrape')",
                 (match_id, PROVIDER_ID, status))
    return conn


def concluded_detail():
    return json.loads((FIXTURES / "match_detail_concluded.json").read_text())


def test_stored_postgame_advances_from_direct_detail_and_refreshes_canonical_row():
    conn, client = database(), DetailClient(concluded_detail())
    result = reconcile_match_status(conn, client, match_provider_id=PROVIDER_ID)
    assert result.afl_match_id == 8207
    assert (result.stored_status, result.direct_status, result.resolved_status) == (
        "POSTGAME", "CONCLUDED", "CONCLUDED",
    )
    assert result.resolution_source == "direct_match_detail"
    assert result.canonical_refreshed is True
    assert conn.execute("SELECT status, scraped_at FROM matches").fetchone() == (
        "CONCLUDED", "metadata-scrape",
    )
    assert conn.execute("SELECT updated_at FROM matches").fetchone()[0] != "old-update"


def test_stale_live_advances_to_direct_concluded():
    result = reconcile_match_status(database("LIVE"), DetailClient(concluded_detail()),
                                    match_provider_id=PROVIDER_ID)
    assert result.resolved_status == "CONCLUDED"


def test_stored_concluded_skips_direct_request():
    client = DetailClient(concluded_detail())
    result = reconcile_match_status(database("CONCLUDED"), client,
                                    match_provider_id=PROVIDER_ID)
    assert result.resolved_status == "CONCLUDED"
    assert result.direct_status is None and client.calls == []


def test_direct_detail_cannot_downgrade_concluded_lifecycle():
    assert later_match_status("CONCLUDED", "LIVE") == "CONCLUDED"
    assert later_match_status("CONCLUDED", "POSTGAME") == "CONCLUDED"


def test_direct_unavailable_falls_back_to_database_with_diagnostic():
    error = AflJsonHttpError("offline", endpoint="match_detail", status_code=503)
    result = reconcile_match_status(database(), DetailClient(error=error),
                                    match_provider_id=PROVIDER_ID)
    assert result.resolved_status == "POSTGAME"
    assert {item.code for item in result.diagnostics} == {"direct_match_detail_unavailable"}


def test_missing_numeric_id_falls_back_without_request():
    client = DetailClient(concluded_detail())
    result = reconcile_match_status(database(match_id=None), client,
                                    match_provider_id=PROVIDER_ID)
    assert result.resolved_status == "POSTGAME" and client.calls == []
    assert {item.code for item in result.diagnostics} == {"missing_afl_match_id"}


def test_unrecognised_direct_status_falls_back_with_diagnostic():
    payload = concluded_detail()
    payload["matches"][0]["status"] = "ABANDONED"
    result = reconcile_match_status(database(), DetailClient(payload),
                                    match_provider_id=PROVIDER_ID)
    assert result.resolved_status == "POSTGAME"
    assert {item.code for item in result.diagnostics} == {"unrecognised_direct_status"}


def test_lifecycle_is_explicit_and_monotonic():
    assert [normalise_match_status(value) for value in (
        "SCHEDULED", "LIVE", "POSTGAME", "CONCLUDED"
    )] == ["SCHEDULED", "LIVE", "POSTGAME", "CONCLUDED"]
    progression = "SCHEDULED"
    for observation in ("LIVE", "POSTGAME", "CONCLUDED", "LIVE"):
        progression = later_match_status(progression, observation)
    assert progression == "CONCLUDED"
