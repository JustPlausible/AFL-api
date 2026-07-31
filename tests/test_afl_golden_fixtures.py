"""Offline golden-corpus regression tests for active AFL source contracts."""
import json
import socket
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scraper.afl_selectors import CLUB_SQUAD_SELECTORS, STATS_LEADERS_SELECTORS
from scraper.monitor_match_status import extract_status_for_match
from scraper.scrape_afl_injuries import extract_and_match_club, parse_injuries_html
from utils.club_lookup import get_canonical_club
from scraper.scrape_afl_lineups import parse_lineups_html
from scraper.scrape_afl_player_stats import get_match_status_from_header, parse_live_stats
from merge.helpers import extract_champion_data_id_from_html, extract_club_player_id

ROOT = Path(__file__).parent / "fixtures"
CORPUS = ROOT / "afl_sources"


def fixture(path):
    return (CORPUS / path).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def block_network_and_sleep(monkeypatch):
    """Make an accidental connection or sleep an immediate, explicit failure."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("golden fixture tests must not access the network or sleep")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("time.sleep", forbidden)
    try:
        import requests
        monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    except ImportError:  # requests is optional to these parsers
        pass


def test_manifest_is_complete_machine_readable_and_sanitised():
    manifest = json.loads(fixture("manifest.json"))
    required = {"id", "path", "domain", "parser", "source_type", "original_url_pattern",
                "represented_state", "expected_record_count", "important_expected_fields", "purpose"}
    assert manifest["schema_version"] == 1
    assert len(manifest["fixtures"]) == 12
    assert {item["domain"] for item in manifest["fixtures"]} >= {
        "fixtures_rounds", "match_details_status", "lineups", "injuries", "clubs_players",
        "player_listings_statistics", "player_statistics", "match_rosters",
    }
    for item in manifest["fixtures"]:
        assert required <= item.keys(), item["id"]
        assert (CORPUS / item["path"]).is_file(), item["path"]
        text = (CORPUS / item["path"]).read_text(encoding="utf-8")
        assert "WMCTok" not in text and "x-media-mis-token" not in text


def test_rendered_lineups_published_and_unpublished_states():
    players = parse_lineups_html(fixture("html_rendered/lineups_round_20_published.html"), 20)
    assert len(players) == 2
    assert [(p["match_id"], p["afl_id"], p["team"]) for p in players] == [
        (8216, 2474, "Adelaide Crows"), (8216, 118, "Collingwood")
    ]
    assert all(p["position_group"] == "ONFIELD" for p in players)
    assert parse_lineups_html(fixture("html_rendered/lineups_round_20_unpublished.html"), 20) == []


def test_rendered_lineup_required_container_mutation_fails_visibly():
    html = fixture("html_rendered/lineups_round_20_published.html").replace(
        "team-lineups__item", "team-lineups__item-changed", 1)
    with pytest.raises(ValueError, match="Lineup source contract.*no match blocks"):
        parse_lineups_html(html, 20)


def test_rendered_injuries_populated_and_empty_tables(monkeypatch):
    clubs = {"Adelaide Crows": {"code": "ADEL", "slug": "adelaide"},
             "Carlton": {"code": "CARL", "slug": "carlton"}}
    from merge.helpers import InjuryPlayerResolution
    resolver = lambda name, club, conn: InjuryPlayerResolution(
        "resolved" if name == "Jordan Example" else "unresolved", name, club,
        afl_id=9001 if name == "Jordan Example" else None,
    )
    result = parse_injuries_html(
        fixture("html_rendered/injuries_round_21_populated.html"),
        club_resolver=lambda _src, alt: clubs.get(alt), player_resolver=resolver,
    )
    assert len(result) == 2
    assert result[0] == {"club": "ADEL", "updated": "July 28, 2026", "player_count": 1,
                         "players": [{"name": "Jordan Example", "injury": "Hamstring",
                                      "return": "2-3 weeks", "afl_id": 9001,
                                      "canonical_player_id": None, "resolution_status": "resolved",
                                      "resolution_reason": None}]}
    assert result[1]["club"] == "CARL" and result[1]["players"] == []


@pytest.mark.parametrize(("token", "filename", "expected_code"), [
    ("ADEL", "26_0134_Editorial-GFX_Straps-Badge-Refresh_ADEL_FA-1x.jpg", "ADE"),
    ("BRIS", "26_0134_Editorial-GFX_Straps-Badge-Refresh_BRIS_FA_v2-1x.jpg", "BRI"),
    ("FREM", "26_0134_Editorial-GFX_Straps-Badge-Refresh_FREM_FA-1x.jpg", "FRE"),
    ("NM", "26_0134_Editorial-GFX_Straps-Badge-Refresh_NM_FA-1x.jpg", "NTH"),
    ("PA", "26_0134_Editorial-GFX_Straps-Badge-Refresh_PA_FA-1x.jpg", "PTA"),
])
def test_injury_editorial_filename_resolves_canonical_identifier_token(
        token, filename, expected_code):
    canonical_club = get_canonical_club(token)
    image_club = extract_and_match_club(
        f"https://resources.afl.com.au/photo-resources/2026/03/20/captured/{filename}?width=1511"
    )

    assert canonical_club is not None
    assert canonical_club["code"] == expected_code
    assert image_club is not None
    assert image_club["code"] == expected_code


def test_injury_image_without_recognised_token_raises_source_contract_error():
    html = fixture("html_rendered/injuries_round_21_populated.html").replace(
        'src="/logos/adelaide-logo.jpg" alt="Adelaide Crows"',
        'src="https://resources.afl.com.au/artwork/26_0134_Editorial-GFX_FA-1x.png?width=1511" alt=""',
        1,
    )

    with pytest.raises(ValueError, match="Injury team block 0 has an unrecognised club image"):
        parse_injuries_html(html)


@pytest.mark.parametrize("needle,replacement,message", [
    ('class="article__body"', 'class="article__body-changed"', "missing article body"),
    ('class="table"', 'class="table-changed"', "missing its table"),
])
def test_rendered_injury_required_contract_mutations_fail(needle, replacement, message):
    html = fixture("html_rendered/injuries_round_21_populated.html").replace(needle, replacement, 1)
    with pytest.raises(ValueError, match=message):
        parse_injuries_html(html, club_resolver=lambda _src, alt: {"code": "ADEL", "slug": "adelaide"})


def test_rendered_match_player_statistics_live_partial_mapping():
    html = fixture("html_rendered/player_stats_match_8216_live_partial.html")
    assert get_match_status_from_header(html) == "LIVE"
    rows = parse_live_stats(html, 8216, 20, "LIVE")
    assert len(rows) == 2
    assert (rows[0]["afl_id"], rows[0]["champion_id"], rows[0]["team_code"]) == (2474, "1008230", "ADEL")
    assert rows[0]["disposals"] == 18 and rows[0]["goals"] == 1
    assert rows[1]["disposals"] is None and rows[1]["goals"] == 0


@pytest.mark.parametrize("needle,replacement,message", [
    ("stats-table__table", "stats-table__table-changed", "missing table"),
    ("stats-table__header-row", "stats-table__header-row-changed", "no headers"),
])
def test_rendered_player_stat_contract_mutations_fail(needle, replacement, message):
    html = fixture("html_rendered/player_stats_match_8216_live_partial.html").replace(needle, replacement)
    with pytest.raises(ValueError, match=message):
        parse_live_stats(html, 8216, 20, "LIVE")


def test_manual_match_monitor_uses_existing_completed_match_fixture():
    html = (ROOT / "afl" / "matches_opening_round_completed.html").read_text()
    status, label = extract_status_for_match(html, 8041)
    assert status == "COMPLETED"
    assert "FULL TIME" in label


def test_club_and_leaderboard_rendered_contract_extracts_key_identities_and_stats():
    club = BeautifulSoup(fixture("html_rendered/club_squad_current_partial.html"), "html.parser")
    cards = club.select(CLUB_SQUAD_SELECTORS.SQUAD_CARD)
    assert len(cards) == 2
    assert cards[0].select_one(CLUB_SQUAD_SELECTORS.PLAYER_LINK)["href"].startswith("/players/2474/")
    assert extract_club_player_id(cards[0].select_one(CLUB_SQUAD_SELECTORS.PLAYER_LINK)["href"]) == 2474
    assert extract_champion_data_id_from_html(str(cards[0]))[0] == "1008230"
    assert cards[1].select_one(CLUB_SQUAD_SELECTORS.POSITION) is None

    leaders = BeautifulSoup(fixture("html_rendered/player_leaders_identity_and_stats.html"), "html.parser")
    rows = leaders.select(STATS_LEADERS_SELECTORS.BODY_ROWS)
    assert len(rows) == 1
    assert rows[0].select_one(STATS_LEADERS_SELECTORS.PLAYER_NAME_LINK)["href"].startswith("/players/2474")
    assert extract_champion_data_id_from_html(str(rows[0]))[0] == "1008230"
    stats = {button["title"].split(": ")[1].rstrip("."): button.get_text(strip=True)
             for button in rows[0].select(STATS_LEADERS_SELECTORS.STAT_BUTTONS)}
    assert stats == {"Goals": "2", "Disposals": "18", "Hitouts": "0", "Marks": "6", "Tackles": "4"}


def test_network_guard_detects_accidental_access():
    with pytest.raises(AssertionError, match="must not access"):
        socket.create_connection(("www.afl.com.au", 443))
"""Offline golden-corpus regression tests for active AFL source contracts."""
import json
import socket
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scraper.afl_selectors import CLUB_SQUAD_SELECTORS, STATS_LEADERS_SELECTORS
from scraper.monitor_match_status import extract_status_for_match
from scraper.scrape_afl_injuries import parse_injuries_html
from scraper.scrape_afl_lineups import parse_lineups_html
from scraper.scrape_afl_player_stats import get_match_status_from_header, parse_live_stats

ROOT = Path(__file__).parent / "fixtures"
CORPUS = ROOT / "afl_sources"


def fixture(path):
    return (CORPUS / path).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def block_network_and_sleep(monkeypatch):
    """Make an accidental connection or sleep an immediate, explicit failure."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("golden fixture tests must not access the network or sleep")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("time.sleep", forbidden)
    try:
        import requests
        monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    except ImportError:  # requests is optional to these parsers
        pass


def test_manifest_is_complete_machine_readable_and_sanitised():
    manifest = json.loads(fixture("manifest.json"))
    required = {"id", "path", "domain", "parser", "source_type", "original_url_pattern",
                "represented_state", "expected_record_count", "important_expected_fields", "purpose"}
    assert manifest["schema_version"] == 1
    assert len(manifest["fixtures"]) == 12
    assert {item["domain"] for item in manifest["fixtures"]} >= {
        "fixtures_rounds", "match_details_status", "lineups", "injuries", "clubs_players",
        "player_listings_statistics", "player_statistics", "match_rosters",
    }
    for item in manifest["fixtures"]:
        assert required <= item.keys(), item["id"]
        assert (CORPUS / item["path"]).is_file(), item["path"]
        text = (CORPUS / item["path"]).read_text(encoding="utf-8")
        assert "WMCTok" not in text and "x-media-mis-token" not in text


def test_rendered_lineups_published_and_unpublished_states():
    players = parse_lineups_html(fixture("html_rendered/lineups_round_20_published.html"), 20)
    assert len(players) == 2
    assert [(p["match_id"], p["afl_id"], p["team"]) for p in players] == [
        (8216, 2474, "Adelaide Crows"), (8216, 118, "Collingwood")
    ]
    assert all(p["position_group"] == "ONFIELD" for p in players)
    assert parse_lineups_html(fixture("html_rendered/lineups_round_20_unpublished.html"), 20) == []


def test_rendered_lineup_required_container_mutation_fails_visibly():
    html = fixture("html_rendered/lineups_round_20_published.html").replace(
        "team-lineups__item", "team-lineups__item-changed", 1)
    with pytest.raises(ValueError, match="Lineup source contract.*no match blocks"):
        parse_lineups_html(html, 20)


def test_rendered_injuries_populated_and_empty_tables(monkeypatch):
    clubs = {"Adelaide Crows": {"code": "ADEL", "slug": "adelaide"},
             "Carlton": {"code": "CARL", "slug": "carlton"}}
    from merge.helpers import InjuryPlayerResolution
    resolver = lambda name, club, conn: InjuryPlayerResolution(
        "resolved" if name == "Jordan Example" else "unresolved", name, club,
        afl_id=9001 if name == "Jordan Example" else None,
    )
    result = parse_injuries_html(
        fixture("html_rendered/injuries_round_21_populated.html"),
        club_resolver=lambda _src, alt: clubs.get(alt), player_resolver=resolver,
    )
    assert len(result) == 2
    assert result[0] == {"club": "ADEL", "updated": "July 28, 2026", "player_count": 1,
                         "players": [{"name": "Jordan Example", "injury": "Hamstring",
                                      "return": "2-3 weeks", "afl_id": 9001,
                                      "canonical_player_id": None, "resolution_status": "resolved",
                                      "resolution_reason": None}]}
    assert result[1]["club"] == "CARL" and result[1]["players"] == []


@pytest.mark.parametrize("needle,replacement,message", [
    ('class="article__body"', 'class="article__body-changed"', "missing article body"),
    ('class="table"', 'class="table-changed"', "missing its table"),
])
def test_rendered_injury_required_contract_mutations_fail(needle, replacement, message):
    html = fixture("html_rendered/injuries_round_21_populated.html").replace(needle, replacement, 1)
    with pytest.raises(ValueError, match=message):
        parse_injuries_html(html, club_resolver=lambda _src, alt: {"code": "ADEL", "slug": "adelaide"})


def test_rendered_match_player_statistics_live_partial_mapping():
    html = fixture("html_rendered/player_stats_match_8216_live_partial.html")
    assert get_match_status_from_header(html) == "LIVE"
    rows = parse_live_stats(html, 8216, 20, "LIVE")
    assert len(rows) == 2
    assert (rows[0]["afl_id"], rows[0]["champion_id"], rows[0]["team_code"]) == (2474, "1008230", "ADEL")
    assert rows[0]["disposals"] == 18 and rows[0]["goals"] == 1
    assert rows[1]["disposals"] is None and rows[1]["goals"] == 0


@pytest.mark.parametrize("needle,replacement,message", [
    ("stats-table__table", "stats-table__table-changed", "missing table"),
    ("stats-table__header-row", "stats-table__header-row-changed", "no headers"),
])
def test_rendered_player_stat_contract_mutations_fail(needle, replacement, message):
    html = fixture("html_rendered/player_stats_match_8216_live_partial.html").replace(needle, replacement)
    with pytest.raises(ValueError, match=message):
        parse_live_stats(html, 8216, 20, "LIVE")


def test_manual_match_monitor_uses_existing_completed_match_fixture():
    html = (ROOT / "afl" / "matches_opening_round_completed.html").read_text()
    status, label = extract_status_for_match(html, 8041)
    assert status == "COMPLETED"
    assert "FULL TIME" in label


def test_club_and_leaderboard_rendered_contract_extracts_key_identities_and_stats():
    club = BeautifulSoup(fixture("html_rendered/club_squad_current_partial.html"), "html.parser")
    cards = club.select(CLUB_SQUAD_SELECTORS.SQUAD_CARD)
    assert len(cards) == 2
    assert cards[0].select_one(CLUB_SQUAD_SELECTORS.PLAYER_LINK)["href"].startswith("/players/2474/")
    assert cards[1].select_one(CLUB_SQUAD_SELECTORS.POSITION) is None

    leaders = BeautifulSoup(fixture("html_rendered/player_leaders_identity_and_stats.html"), "html.parser")
    rows = leaders.select(STATS_LEADERS_SELECTORS.BODY_ROWS)
    assert len(rows) == 1
    assert rows[0].select_one(STATS_LEADERS_SELECTORS.PLAYER_NAME_LINK)["href"].startswith("/players/2474")
    stats = {button["title"].split(": ")[1].rstrip("."): button.get_text(strip=True)
             for button in rows[0].select(STATS_LEADERS_SELECTORS.STAT_BUTTONS)}
    assert stats == {"Goals": "2", "Disposals": "18", "Hitouts": "0", "Marks": "6", "Tackles": "4"}


def test_network_guard_detects_accidental_access():
    with pytest.raises(AssertionError, match="must not access"):
        socket.create_connection(("www.afl.com.au", 443))
