import sqlite3

from db.import_to_db import save_rounds_to_db
from scraper.scrape_afl_fixtures import parse_fixtures_metadata, parse_round_list


FIXTURE_HTML = """
<div class="js-react-fixtures"
     data-season-pid="CD_S2025014"
     data-season-id="73"
     data-competition-id="1"
     data-no-filter-round="1155"
     data-special-round="false"></div>
<ul class="competition-nav__round-list">
  <li data-round-id="1154"><button>Opening Round</button></li>
  <li data-round-id="1155"><button>Round 1</button></li>
</ul>
"""


def test_parse_fixtures_metadata_extracts_season_and_competition_ids():
    metadata = parse_fixtures_metadata(FIXTURE_HTML)

    assert metadata == {
        "season_pid": "CD_S2025014",
        "season_id": 73,
        "competition_id": 1,
        "default_round_id": 1155,
        "special_round": "false",
    }


def test_parse_round_list_extracts_round_ids_and_labels():
    rounds = parse_round_list(FIXTURE_HTML)

    assert rounds == [
        {"round_id": 1154, "round_label": "Opening Round"},
        {"round_id": 1155, "round_label": "Round 1"},
    ]


def test_save_rounds_to_db_upserts_round_metadata():
    conn = sqlite3.connect(":memory:")
    rounds = parse_round_list(FIXTURE_HTML)
    metadata = parse_fixtures_metadata(FIXTURE_HTML)

    save_rounds_to_db(rounds, metadata, conn)
    save_rounds_to_db([{"round_id": 1155, "round_label": "Round One"}], metadata, conn)

    rows = conn.execute(
        "SELECT round_id, round_label, season_id, competition_id FROM rounds ORDER BY round_id"
    ).fetchall()

    assert rows == [
        (1154, "Opening Round", 73, 1),
        (1155, "Round One", 73, 1),
    ]
