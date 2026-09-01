from .parser import parse_player_movements_html
from .resolution import MovementResolver, resolve_canonical_afl_season_id
from .persistence import MovementPersistenceAdapter

def import_player_movements(conn,document,*,movement_season_year):
 parsed=parse_player_movements_html(document.html)
 resolved=MovementResolver(conn).resolve(parsed,movement_season_year=movement_season_year)
 return MovementPersistenceAdapter(conn).persist(resolved,document,movement_season_year=movement_season_year,counts_by_type=parsed.counts_by_type)

def reconcile_player_movements(conn,*,movement_season_year,next_season_year,source_archived_at=None):
 """Read-only AFL membership comparison; editorial evidence never changes membership."""
 old_season_id = resolve_canonical_afl_season_id(conn, movement_season_year)
 new_season_id = resolve_canonical_afl_season_id(conn, next_season_year)
 params=[old_season_id,new_season_id,movement_season_year]; archive=''
 if source_archived_at is not None: archive=' AND pmo.source_archived_at IS ?'; params.append(source_archived_at)
 rows=conn.execute(f'''SELECT pmo.*, old.team_id old_team_id, new.team_id new_team_id FROM player_movement_observations pmo LEFT JOIN competition_season_players old ON old.player_id=pmo.canonical_player_id AND old.competition_season_id=? LEFT JOIN competition_season_players new ON new.player_id=pmo.canonical_player_id AND new.competition_season_id=? WHERE pmo.movement_season_year=?{archive}''',params).fetchall()
 out=[]
 for r in rows:
  if r['canonical_player_id'] is None: transition='unresolved'
  elif r['new_team_id'] is None: transition='absent_from_next_population'
  elif r['old_team_id']==r['new_team_id']: transition='same_club'
  else: transition='changed_club'
  out.append({'movement_id':r['id'],'canonical_player_id':r['canonical_player_id'],'transition':transition,'movement_type':r['movement_type']})
 return out
