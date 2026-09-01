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
