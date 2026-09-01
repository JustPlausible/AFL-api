from .models import MovementImportOutcome
class MovementPersistenceAdapter:
 def __init__(self,conn): self.conn=conn
 def persist(self,resolved,document,*,movement_season_year,counts_by_type):
  inserted=updated=unchanged=0
  source_snapshot_at = (document.source_archived_at if document.source_archived_at is not None
                        else document.observed_at)
  try:
   for r in resolved.records:
    s=r.source; key=(movement_season_year,document.source_url,source_snapshot_at,s.team_name,s.player_name,s.source_label)
    old=self.conn.execute('SELECT id,canonical_player_id,from_team_id,movement_type,source_detail,article_url,resolution_status,resolution_reason FROM player_movement_observations WHERE movement_season_year=? AND source_url=? AND source_snapshot_at=? AND source_team_name=? AND source_player_name=? AND source_label=?',key).fetchone()
    values=(r.canonical_player_id,r.from_team_id,s.movement_type,s.source_detail,s.article_url,r.status,r.reason)
    if old is None:
     self.conn.execute('''INSERT INTO player_movement_observations(canonical_player_id,movement_season_year,from_team_id,movement_type,source_label,source_detail,source_player_name,source_team_name,article_url,source_url,source_archived_at,source_snapshot_at,observed_at,resolution_status,resolution_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(r.canonical_player_id,movement_season_year,r.from_team_id,s.movement_type,s.source_label,s.source_detail,s.player_name,s.team_name,s.article_url,document.source_url,document.source_archived_at,source_snapshot_at,document.observed_at,r.status,r.reason,document.observed_at,document.observed_at)); inserted+=1
    elif tuple(old[1:])==values: unchanged+=1
    else:
     self.conn.execute('UPDATE player_movement_observations SET canonical_player_id=?,from_team_id=?,movement_type=?,source_detail=?,article_url=?,resolution_status=?,resolution_reason=?,observed_at=?,updated_at=? WHERE id=?',(*values,document.observed_at,document.observed_at,old[0])); updated+=1
   self.conn.commit()
  except Exception: self.conn.rollback(); raise
  statuses=[r.status for r in resolved.records]
  return MovementImportOutcome(movement_season_year,document.source_url,document.source_archived_at,document.observed_at,len(statuses),statuses.count('resolved'),statuses.count('unresolved'),statuses.count('ambiguous'),inserted,updated,unchanged,counts_by_type)
