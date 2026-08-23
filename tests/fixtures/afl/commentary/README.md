# AFL Match Commentary Fixtures

## Purpose

These fixtures support investigation, parser development and regression testing for the AFL Match Feed and its authenticated CFS commentary source.

The fixture set contains:

- the canonical CFS JSON commentary response; and
- the rendered HTML produced by the AFL Match Feed widget.

The JSON should be treated as the preferred structured source. The HTML is retained to verify how the AFL website enriches and displays those events.

## Fixture files

### `match_8216_commentary_concluded.json`

Raw response from:

```text
GET https://api.afl.com.au/cfs/commentaryFeed/CD_M20260142001
```

Authentication requires the temporary token supplied in `x-media-mis-token`. The token can be obtained from:

```text
POST https://api.afl.com.au/cfs/afl/WMCTok
```

This fixture was captured on 29 July 2026 using the token-based capture script.

### `match_8216_match_feed_concluded_rendered.html`

Rendered outer HTML captured from the AFL Match Feed widget using Chrome DevTools `Copy outerHTML`.

The capture contains the entire widget as presented in DevTools Elements, including:

- the commentary event list;
- quarter markers;
- scoring-event cards;
- the score worm or match timeline;
- quarter scores;
- player links and headshots;
- team badges and presentation metadata.

This is a rendered HTML fragment, not a complete HTML document.

## Source page

```text
https://www.afl.com.au/afl/matches/8216#timeline
```

The JSON and HTML were captured during the same browser session after the match had concluded.

## Match context

| Field | Value |
|---|---|
| Season | 2026 |
| Round | 20 |
| AFL match ID | `8216` |
| Match provider ID | `CD_M20260142001` |
| Competition season provider ID | `CD_S2026014` |
| Home team | Adelaide Crows |
| Home team provider ID | `CD_T10` |
| Away team | Collingwood |
| Away team provider ID | `CD_T40` |
| Match status | `CONCLUDED` |
| Capture date | `2026-07-29` |

## JSON structure

The response contains:

```json
{
  "matchId": "CD_M20260142001",
  "lastUpdated": "2026-07-23T12:25:15.324+0000",
  "commentaryEvent": []
}
```

Each commentary event contains:

```json
{
  "comment": "GOAL - Crows (Toby Murray)",
  "periodNumber": 4,
  "periodSeconds": 1943,
  "playerId": "CD_I1020950",
  "teamId": "CD_T10",
  "scoreEvent": true
}
```

Observed event fields:

- `comment`
- `periodNumber`
- `periodSeconds`
- `playerId`
- `teamId`
- `scoreEvent`

## Event types

The feed includes goals, behinds, rushed behinds, statistical commentary, quarter start and end messages, interchange and injury notes, pre-match team changes and general pre-match commentary.

The source does not provide a formal event-type field beyond `scoreEvent`. Consumers may derive a more specific type from the commentary text, but the raw comment must always be preserved.

Suggested derived values:

```text
goal
behind
rushed_behind
quarter_start
quarter_end
interchange
injury_note
team_change
statistical_commentary
general_commentary
```

Derived classifications should be treated as parser output rather than source facts.

## Period handling

Observed `periodNumber` values include `0` for pre-match commentary and `1` to `4` for regulation quarters. `periodSeconds` represents elapsed time within the period.

Examples:

```text
1950 seconds -> 32:30
1943 seconds -> 32:23
513 seconds  -> 08:33
```

The parser should not assume AFL quarters end at a fixed duration because time-on produces variable quarter lengths.

## Ordering

The concluded JSON response is ordered newest-first. Applications requiring chronological playback should explicitly sort by `periodNumber` ascending and `periodSeconds` ascending.

Where multiple events share the same period and second, preserve original array position as a stable tie-breaker.

## Identifier behaviour

The top-level `matchId` is the Champion Data match provider ID. Scoring records may include Champion Data player and team IDs, but `playerId` and `teamId` can be null for neutral commentary, quarter markers, rushed behinds and some interchange or injury notes.

Player names mentioned only in narrative text should not automatically be treated as authoritative player links.

## JSON and HTML comparison

The rendered HTML adds display information that is not present directly in the commentary JSON, including:

- formatted `MM:SS` times;
- player first name and surname;
- AFL player profile URL;
- AFL numeric player ID in the profile URL;
- Champion Data player ID as a data attribute;
- player headshot URL;
- team badge and watermark;
- cumulative player goals and behinds at that moment;
- quarter start and end presentation;
- goal, behind and rushed-behind styling;
- the score worm or match timeline;
- quarter-by-quarter scores.

The score worm is present inside the captured widget. Its exact upstream source has not yet been independently confirmed and may come from the same broader match data used by the Match Feed component.

The HTML should not replace the JSON as the canonical event source because much of this information is presentation-oriented or derived from other AFL datasets.

## Canonical event model

```json
{
  "match_provider_id": "CD_M20260142001",
  "source_index": 3,
  "period_number": 4,
  "period_seconds": 1943,
  "event_type": "goal",
  "comment_raw": "GOAL - Crows (Toby Murray)",
  "score_event": true,
  "team_provider_id": "CD_T10",
  "player_provider_id": "CD_I1020950",
  "collected_at": "2026-07-29",
  "source_last_updated": "2026-07-23T12:25:15.324+0000"
}
```

## Parsing requirements

A commentary collector should:

1. preserve the raw response;
2. preserve every event in source order;
3. retain nullable player and team IDs;
4. validate period values without assuming only periods 1 to 4;
5. validate numeric seconds without imposing a fixed quarter duration;
6. preserve raw commentary text unchanged;
7. distinguish source fields from derived classifications;
8. record the source `lastUpdated` value;
9. record collection time separately from source update time;
10. allow later live collections to be superseded by concluded data.

## Fixture assertions

Useful regression assertions include:

- top-level `matchId` matches the requested provider ID;
- `commentaryEvent` is an array;
- all events contain the six observed event fields;
- period `0` pre-match records are retained;
- scoring events may have null player IDs for rushed behinds;
- neutral commentary may have null player and team IDs;
- elapsed seconds convert correctly to displayed HTML times;
- JSON player IDs match HTML `data-player-id` values where present;
- JSON team IDs map to the correct rendered team;
- raw ordering is preserved;
- chronological ordering can be derived deterministically;
- the captured HTML widget exposes the expected match and competition IDs.

## Production fixtures (Issue #201)

Two additional real/evidence-backed fixture sets support the production
persistence and consumer API added in Issue #201 (see
`afl_json/match_commentary.py`, `scheduler/match_commentary_production.py`,
`api/routes_v1.py`):

* `commentary_CD_M20260142409_full.json` / `commentary_CD_M20260142409_reduced.json`
  (metadata: `commentary_CD_M20260142409.metadata.json`) -- a real, verbatim
  Round 24 Bruno capture (`.response.json`) supplied by the user for
  `CD_M20260142409` (West Coast Eagles v Hawthorn), POSTGAME/CONCLUDED, plus
  a hand-reduced subset preserving the exact structure of several edge
  cases: two genuine same-`(periodNumber, periodSeconds)` pairs, a
  team-only `Rushed` behind, player+team-linked goals/behinds, and
  pre-match commentary.
* `commentary_CD_M20260142406_full.json` (real, verbatim final
  concluded-match capture, supplied directly by the repository owner) plus
  `commentary_CD_M20260142406_score_review_poll1.json` /
  `_poll2.json` (metadata: `commentary_CD_M20260142406_score_review.metadata.json`)
  -- evidence for a genuine **same-slot scoring-outcome change**
  (`GOAL -> BEHIND` for the same player at the same match-clock second)
  found in a *different* Round 24 match (`CD_M20260142406`) from the same
  capture set. `poll1` remains a clearly-labelled reconstruction of the
  earlier (pre-change) state; `poll2` is a reduced, verbatim subset of the
  real full capture, which also revealed that the upstream feed itself no
  longer contains the earlier `GOAL` text -- only AFL-api's own append-only
  persistence retains both. Deliberately not called a "review" or
  "reversal" anywhere in this fixture set: the feed never states why the
  outcome changed. `CD_M20260142409` itself shows no such sequence in
  either originally-supplied file -- see the metadata file's `provenance`
  for the full explanation of where the real evidence was actually found.

## Potential future use

This endpoint may support optional fantasy-league features such as live match commentary, scoring-event notifications, player-linked fantasy alerts, translated commentary, automated match summaries, injury or interchange warnings and live commentary filtering by fantasy squad.

These are secondary uses. The initial implementation should focus on reliable capture, persistence and event normalisation.

## Known limitations

- Narrative commentary frequently mentions players without supplying `playerId`.
- Event type must often be inferred from free text.
- The endpoint does not include cumulative match scores in each JSON event.
- The rendered HTML includes derived data from other sources.
- Multiple records can share an identical period and second.
- Commentary wording may change without a schema change.
- Live response behaviour and update cadence have not yet been documented.
- The score worm is included in the HTML fixture, but its precise source endpoint has not yet been confirmed.
