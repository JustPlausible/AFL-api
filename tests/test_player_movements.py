from pathlib import Path
from scraper.player_movements.acquisition import archived_at_from_url
from scraper.player_movements.parser import parse_player_movements_html
F=Path(__file__).parent/'fixtures/afl/player_movements'
def test_historical_fixture_contract():
 r=parse_player_movements_html((F/'afl_retirements_and_delistings_wayback_2026-02-01.html').read_text())
 assert r.team_count==18 and len(r.records)==146
 assert r.counts_by_type=={'DELISTED':83,'DELISTED_FREE_AGENT':1,'FREE_AGENT':6,'RETIRED':29,'TRADED':27}
 assert archived_at_from_url('https://web.archive.org/web/20260201000614/https://www.afl.com.au/news/retirements-and-delistings')=='2026-02-01T00:06:14Z'
def test_current_fixture_contract_and_safe_unknowns():
 r=parse_player_movements_html((F/'AFL Retirements, Delistings, Trades - AFL.com.au.html').read_text())
 assert r.team_count==18 and len(r.records)==50 and r.counts_by_type['OTHER']==3
 assert {(x.source_label,x.movement_type) for x in r.records if x.movement_type=='OTHER'}=={('TBC','OTHER'),('ret/del','OTHER')}
 assert any(x.player_name=='Callum Coleman-Jones' and x.article_url is None for x in r.records)

import sqlite3
from scraper.player_movements.acquisition import LIVE_URL, MovementAcquirer
from scraper.player_movements.resolution import MovementResolver
from scraper.player_movements.persistence import MovementPersistenceAdapter


def _movement_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE afl_competitions (
            afl_id INTEGER PRIMARY KEY, provider_id TEXT, code TEXT
        );
        CREATE TABLE afl_seasons (
            afl_id INTEGER PRIMARY KEY, competition_id INTEGER, year INTEGER
        );
        CREATE TABLE afl_teams (afl_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE canonical_players (
            id INTEGER PRIMARY KEY, display_name TEXT, given_name TEXT, family_name TEXT
        );
        CREATE TABLE competition_season_players (
            player_id INTEGER, competition_season_id INTEGER, team_id INTEGER
        );
    """)
    # Deliberately insert AFLW first: the former year-only query selected it.
    conn.executemany(
        "INSERT INTO afl_competitions VALUES(?,?,?)",
        ((2, "CD_C999", "AFLW"), (1, "CD_C014", "AFL")),
    )
    conn.executemany(
        "INSERT INTO afl_seasons VALUES(?,?,2025)", ((200, 2), (100, 1))
    )
    conn.execute("INSERT INTO afl_teams VALUES(1,'Adelaide Crows')")
    conn.executemany(
        "INSERT INTO canonical_players VALUES(?,?,?,?)",
        ((20, "Chris Burgess", "Chris", "Burgess"),
         (10, "Chris Burgess", "Chris", "Burgess")),
    )
    conn.executemany(
        "INSERT INTO competition_season_players VALUES(?,?,1)",
        ((20, 200), (10, 100)),
    )
    import importlib
    importlib.import_module("db.migrations.0029_player_movements").migrate(conn)
    return conn


def test_resolution_is_scoped_to_afl_competition_for_duplicate_year():
    parsed = parse_player_movements_html(
        (F / "afl_retirements_and_delistings_wayback_2026-02-01.html").read_text()
    )
    conn = _movement_connection()
    try:
        result = MovementResolver(conn).resolve(parsed, movement_season_year=2025)
    finally:
        conn.close()
    burgess = next(r for r in result.records if r.source.player_name == "Chris Burgess")
    assert burgess.status == "resolved"
    assert burgess.canonical_player_id == 10


def test_saved_fixture_and_archived_url_persist_original_provenance():
    url = ("https://web.archive.org/web/20260201000614/"
           "https://www.afl.com.au/news/retirements-and-delistings")
    document = MovementAcquirer().acquire_file(
        F / "afl_retirements_and_delistings_wayback_2026-02-01.html",
        source_url=url,
    )
    assert document.source_url == url
    assert document.source_archived_at == "2026-02-01T00:06:14Z"

    parsed = parse_player_movements_html(document.html)
    conn = _movement_connection()
    try:
        resolved = MovementResolver(conn).resolve(parsed, movement_season_year=2025)
        MovementPersistenceAdapter(conn).persist(
            resolved, document, movement_season_year=2025,
            counts_by_type=parsed.counts_by_type,
        )
        stored = conn.execute(
            "SELECT source_url,source_archived_at FROM player_movement_observations "
            "WHERE source_player_name='Chris Burgess'"
        ).fetchone()
    finally:
        conn.close()
    assert tuple(stored) == (url, "2026-02-01T00:06:14Z")


class _Response:
    text = "<html>retirements-and-delistings</html>"


class _HttpClient:
    def get(self, url, timeout=None):
        self.url = url
        return _Response()


def test_direct_wayback_and_live_url_archive_semantics():
    client = _HttpClient()
    acquirer = MovementAcquirer(client)
    wayback = ("https://web.archive.org/web/20260201000614/"
               "https://www.afl.com.au/news/retirements-and-delistings")
    archived = acquirer.acquire_url(wayback, observed_at="2026-09-01T00:00:00Z")
    live = acquirer.acquire_url(LIVE_URL, observed_at="2026-09-01T00:00:00Z")
    assert (archived.source_url, archived.source_archived_at) == (
        wayback, "2026-02-01T00:06:14Z"
    )
    assert (live.source_url, live.source_archived_at) == (LIVE_URL, None)


def test_url_acquisition_prefers_explicit_archive_timestamp():
    explicit = "2026-02-02T03:04:05Z"
    acquirer = MovementAcquirer(_HttpClient())
    inferable = ("https://web.archive.org/web/20260201000614/"
                 "https://www.afl.com.au/news/retirements-and-delistings")
    unrecognised_archive = "https://archive.example.test/afl/movements"
    assert acquirer.acquire_url(
        inferable, source_archived_at=explicit
    ).source_archived_at == explicit
    assert acquirer.acquire_url(
        unrecognised_archive, source_archived_at=explicit
    ).source_archived_at == explicit


def test_reconciliation_uses_only_afl_seasons_and_preserves_archive_filter():
    from scraper.player_movements.orchestration import reconcile_player_movements

    conn = _movement_connection()
    try:
        conn.executemany(
            "INSERT INTO afl_seasons VALUES(?,?,?)",
            ((101, 1, 2026), (201, 2, 2026)),
        )
        conn.executemany(
            "INSERT INTO afl_teams VALUES(?,?)",
            ((2, "Brisbane Lions"), (3, "AFLW Club")),
        )
        # AFL changes club. Conflicting AFLW membership stays at a third club in
        # 2025 and is absent in 2026; neither may duplicate/change the result.
        conn.executemany(
            "INSERT INTO competition_season_players VALUES(?,?,?)",
            ((10, 101, 2), (10, 200, 3)),
        )
        common = (
            10, 2025, 1, "TRADED", "trd", "Chris Burgess", "Adelaide",
            "AFL_EDITORIAL", "https://example.test/source", "resolved",
            "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z",
            "2026-09-01T00:00:00Z",
        )
        conn.executemany("""
            INSERT INTO player_movement_observations(
                canonical_player_id,movement_season_year,from_team_id,movement_type,
                source_label,source_player_name,source_team_name,source_family,
                source_url,source_archived_at,resolution_status,observed_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, ((*common[:9], "2026-02-01T00:06:14Z", *common[9:]),
               (*common[:9], "2026-09-01T00:00:00Z", *common[9:])))
        rows = reconcile_player_movements(
            conn, movement_season_year=2025, next_season_year=2026,
            source_archived_at="2026-02-01T00:06:14Z",
        )
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["canonical_player_id"] == 10
    assert rows[0]["transition"] == "changed_club"


def test_url_explicit_archive_timestamp_matches_persisted_outcome():
    class FixtureHttpClient:
        def get(self, url, timeout=None):
            response = _Response()
            response.text = (
                F / "afl_retirements_and_delistings_wayback_2026-02-01.html"
            ).read_text()
            return response

    explicit = "2026-02-01T00:06:14Z"
    url = "https://archive.example.test/afl/retirements-and-delistings"
    document = MovementAcquirer(FixtureHttpClient()).acquire_url(
        url, source_archived_at=explicit,
        observed_at="2026-09-01T00:00:00Z",
    )
    parsed = parse_player_movements_html(document.html)
    conn = _movement_connection()
    try:
        resolved = MovementResolver(conn).resolve(parsed, movement_season_year=2025)
        outcome = MovementPersistenceAdapter(conn).persist(
            resolved, document, movement_season_year=2025,
            counts_by_type=parsed.counts_by_type,
        )
        stored = conn.execute(
            "SELECT source_archived_at FROM player_movement_observations "
            "WHERE source_player_name='Chris Burgess'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert outcome.source_archived_at == explicit
    assert stored == explicit
