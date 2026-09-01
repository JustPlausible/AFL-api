# AFL editorial player movements (Issue #240)

## Authority and architecture

AFL/CFS season membership remains **HIGH / authoritative** for player population and club. AFL's Retirements, Delistings and Trades HTML is **SUPPLEMENTAL historical context** only. Importing it never creates players/provider IDs, changes `canonical_players`, or mutates season membership, current team, activity, or eligibility. This follows the #108 authority map and complements #237 statistical history and #238 replay work.

The bounded `scraper/player_movements/` pipeline separates plain-HTTP/local-file acquisition, deterministic HTML parsing, previous-season club-scoped identity resolution, observation persistence, and orchestration. It is deliberately not scheduled or connected to season sync.

## Supplied source investigation

Reduced offline fixtures preserve the factual editorial table from both Issue #240 attachments. In both snapshots clubs are `<strong>` headings in four-column table heading rows; the following row's corresponding cell contains movements. Most entries are article links whose **combined text** is `Player (label)` (not separate fields), separated by `<br>`; the current page also has unlinked paragraph text. Article links contain news article numbers, not player IDs. There are no team links/IDs and no equivalent structured JSON in the saved documents.

The 1 February fixture has 18 clubs and 146 rows: `del` 83, `ret` 29, `trd` 27, `FA` 6 and `DFA` 1. The supplied page legend mentions an asterisk meaning a club commitment to redraft, but no row in that snapshot actually contains the marker. If present, the parser retains that wording in `source_detail`. The current fixture has 18 clubs and 50 rows: `del` 31, `ret` 15, `FA` 1, plus two `TBC` and one `ret/del`; the latter new/non-atomic labels are safely retained as `OTHER`, with the original label. Its notable DOM drift is unlinked `<p>/<br>` entries and empty club cells. Both remain ordinary static HTML; browser automation is unnecessary.

## Acquisition and provenance

Use a saved file or an explicit live/archive URL. Wayback timestamps are parsed from the URL, so `20260201000614` is persisted as `2026-02-01T00:06:14Z`; observation/import time remains separate. A local historical file must be supplied its original URL (and may explicitly supply `--source-archived-at`). Snapshot identity includes URL and archive time, allowing February and later observations to coexist and same-snapshot imports to be idempotent.

```bash
python cli.py --import-player-movements 2025 \
  --movement-source-file tests/fixtures/afl/player_movements/afl_retirements_and_delistings_wayback_2026-02-01.html \
  --movement-source-url https://web.archive.org/web/20260201000614/https://www.afl.com.au/news/retirements-and-delistings
python cli.py --import-player-movements 2026 \
  --movement-source-url https://www.afl.com.au/news/retirements-and-delistings --print-json
```

Resolution is exact-name only within the named canonical club's applicable previous-season membership. Unknown team/player evidence stays unresolved; duplicate exact members stay ambiguous. There is no global or fuzzy fallback and provider crosswalks are read neither written nor invented.

## Consumer and replay

`GET /api/v1/players/{canonical_player_id}/movements` returns resolved supplemental observations, including the originating team, original label/detail, AFL article link, snapshot source URL, archive timestamp, and observation time. A known player with no movement history returns an empty `movements` list; an unknown canonical ID returns the normal 404 application error.

`reconcile_player_movements` is a read-only operator building block for previous/current membership comparison. It reports `same_club`, `changed_club`, `absent_from_next_population`, or `unresolved`; a movement may explain a factual transition but never causes or guesses one.
