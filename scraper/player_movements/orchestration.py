from .parser import parse_player_movements_html
from .resolution import MovementResolver
from .persistence import MovementPersistenceAdapter

def import_player_movements(conn,document,*,movement_season_year):
 parsed=parse_player_movements_html(document.html)
 resolved=MovementResolver(conn).resolve(parsed,movement_season_year=movement_season_year)
 return MovementPersistenceAdapter(conn).persist(resolved,document,movement_season_year=movement_season_year,counts_by_type=parsed.counts_by_type)

def reconcile_player_movements(conn,*,movement_season_year,next_season_year,source_archived_at=None):
 """Read-only comparison; editorial evidence never changes membership."""
 params=[movement_season_year]; archive=''
 if source_archived_at is not None: archive=' AND pmo.source_archived_at IS ?'; params.append(source_archived_at)
 rows=conn.execute(f'''SELECT pmo.*, old.team_id old_team_id, new.team_id new_team_id FROM player_movement_observations pmo LEFT JOIN afl_seasons os ON os.year=? LEFT JOIN competition_season_players old ON old.player_id=pmo.canonical_player_id AND old.competition_season_id=os.afl_id LEFT JOIN afl_seasons ns ON ns.year=? LEFT JOIN competition_season_players new ON new.player_id=pmo.canonical_player_id AND new.competition_season_id=ns.afl_id WHERE pmo.movement_season_year=?{archive}''',(movement_season_year,next_season_year,*params)).fetchall()
 out=[]
 for r in rows:
  if r['canonical_player_id'] is None: transition='unresolved'
  elif r['new_team_id'] is None: transition='absent_from_next_population'
  elif r['old_team_id']==r['new_team_id']: transition='same_club'
  else: transition='changed_club'
  out.append({'movement_id':r['id'],'canonical_player_id':r['canonical_player_id'],'transition':transition,'movement_type':r['movement_type']})
 return out
