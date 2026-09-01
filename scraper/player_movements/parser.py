import re
from collections import Counter
from bs4 import BeautifulSoup, NavigableString
from .models import MovementParseResult, ParsedMovementRecord

TAXONOMY = {"ret":"RETIRED", "del":"DELISTED", "trd":"TRADED", "FA":"FREE_AGENT", "DFA":"DELISTED_FREE_AGENT"}
_ROW = re.compile(r"^\s*(.+?)\s*\(([^()]+)\)(\*)?\s*$")

def parse_player_movements_html(html: str) -> MovementParseResult:
    soup = BeautifulSoup(html, "html.parser")
    candidates = [t for t in soup.find_all("table") if len(t.find_all("strong")) >= 10]
    if not candidates:
        raise ValueError("Player-movement source contains no club movement table")
    table = max(candidates, key=lambda t: len(t.get_text(" ", strip=True)))
    records=[]; teams=set(); headings=None
    rows=table.find_all("tr")
    for row in rows:
        cells=row.find_all("td", recursive=False)
        names=[c.get_text(" ",strip=True) if c.find("strong") else "" for c in cells]
        if any(names):
            headings=names; teams.update(n for n in names if n); continue
        if not headings:
            continue
        for team, cell in zip(headings,cells):
            if not team: continue
            # Links are normally rows; current HTML also has unlinked <p>/<br> text.
            pieces=[]
            for node in cell.descendants:
                if isinstance(node,NavigableString):
                    value=str(node).strip()
                    if value: pieces.append((value, node.parent.find_parent("a") or (node.parent if node.parent.name=="a" else None)))
            for text, link in pieces:
                m=_ROW.match(text)
                if not m: continue
                name,label,star=m.groups()
                movement=TAXONOMY.get(label, "OTHER")
                detail="Club has committed to redrafting the player in the AFL and Rookie Drafts" if star else None
                records.append(ParsedMovementRecord(name.strip(),team,movement,label,detail,link.get("href") if link else None))
    if not records: raise ValueError("Player-movement table contains no recognised movement rows")
    counts=dict(sorted(Counter(r.movement_type for r in records).items()))
    return MovementParseResult(tuple(records),len(teams),counts)
