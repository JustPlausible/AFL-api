import re, unicodedata
from utils.club_lookup import get_canonical_club
from .models import MovementResolutionResult, ResolvedMovementRecord

def _name(value): return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC",value).casefold())

class MovementResolver:
    def __init__(self, conn): self.conn=conn
    def resolve(self, parsed, *, movement_season_year: int):
        season=self.conn.execute("SELECT afl_id FROM afl_seasons WHERE year=?",(movement_season_year,)).fetchone()
        season_id=season[0] if season else None
        output=[]
        for source in parsed.records:
            club=get_canonical_club(source.team_name)
            if not club or season_id is None:
                output.append(ResolvedMovementRecord(source,"unresolved",reason="source team or movement season is not canonical")); continue
            rows=self.conn.execute("SELECT cp.id,cp.display_name,cp.given_name,cp.family_name FROM competition_season_players csp JOIN canonical_players cp ON cp.id=csp.player_id WHERE csp.competition_season_id=? AND csp.team_id=?",(season_id,club['teamId'])).fetchall()
            matches=[]
            for row in rows:
                values=[row[1]," ".join(x for x in (row[2],row[3]) if x)]
                if any(v and _name(v)==_name(source.player_name) for v in values): matches.append(row[0])
            if len(matches)==1: output.append(ResolvedMovementRecord(source,"resolved",matches[0],club['teamId']))
            elif len(matches)>1: output.append(ResolvedMovementRecord(source,"ambiguous",from_team_id=club['teamId'],reason="multiple exact players in previous-season club membership"))
            else: output.append(ResolvedMovementRecord(source,"unresolved",from_team_id=club['teamId'],reason="no exact player in previous-season club membership"))
        return MovementResolutionResult(tuple(output))
