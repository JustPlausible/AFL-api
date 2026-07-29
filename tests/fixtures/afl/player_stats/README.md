# AFL Player Statistics Fixtures

This directory contains captured AFL match-centre player-stat fixtures used to test the rendered HTML player-stat parser.

## Purpose

These fixtures support regression testing of the HTML fallback and diagnostic parser for AFL match player statistics.

The preferred structured source remains the authenticated CFS endpoint:

```text
GET /cfs/afl/playerStats/match/{matchProviderId}
```

The rendered HTML table is produced from the same underlying CFS data and should not be treated as an independent fallback when the CFS service or token system is unavailable.

The HTML parser remains useful for:

* validating parity between CFS JSON and rendered match-centre values;
* regression testing the existing scraper;
* recovering data from saved rendered pages;
* diagnosing changes to AFL match-centre markup;
* supporting historical-gap workflows where rendered HTML is available.

## Fixture inventory

### `match_8216_completed_rendered.html`

A sanitised rendered player-stat table captured from:

```text
https://www.afl.com.au/afl/matches/8216#player-stats
```

Capture state:

```text
AFL match ID: 8216
Champion Data match ID: CD_M20260142001
Competition season: CD_S2026014
Home team: Adelaide, CD_T10
Away team: Collingwood, CD_T40
Match state: completed
Expected player rows: 46
```

The fixture contains the rendered React component only. Scripts and unrelated page content have been omitted.

### `match_8216_completed.metadata.json`

Machine-readable provenance and expectations for the associated HTML fixture.

This file records:

* source URL;
* capture date;
* match state;
* AFL and Champion Data identifiers;
* expected row count;
* sanitisation details;
* whether authentication material is present.

No token, cookie or other authentication value should ever be stored in fixture metadata.

## Parser contract

The parser should discover statistic columns from the rendered table header rather than relying solely on fixed column positions.

The captured basic-stat headers are:

```text
# | Player | AF | G | B | D | K | H | M | T | HO | CLR | MG | GA | ToG%
```

Individual statistic cells use a generic class:

```html
<td role="cell" class="stats-table__cell">
```

The meaning of each value is therefore determined by its position relative to the parsed header row.

The parser should:

1. locate the player-stat table;
2. parse and normalise the header labels;
3. construct a header-to-column index map;
4. validate the number of values in each row;
5. map recognised columns into canonical statistic fields;
6. preserve or report unrecognised columns;
7. produce diagnostics when required headings are absent.

## Player identity

The rendered table exposes multiple identity signals.

### AFL player ID

The AFL numeric player ID can be extracted from the player profile link.

Example:

```text
/players/1080/jordan-dawson
```

produces:

```text
AFL player ID: 1080
```

### Champion Data player ID

The Champion Data numeric player identifier can currently be recovered from the headshot filename.

Example:

```text
.../ChampIDImages/AFL/2026014/992242.png
```

produces:

```text
Champion Data player ID: CD_I992242
```

The season directory is not part of the player ID.

This is a markup convention rather than an explicit semantic field. If the headshot path format changes or the image is absent, the parser should return a missing Champion Data ID and emit a diagnostic rather than constructing an ID from other player attributes.

### Team identification

The row identifies home or away team membership through the jumper-number element and team abbreviation class.

The table-level metadata also includes:

```text
data-home-pid
data-away-pid
data-home-abbr
data-away-abbr
```

These values should be preferred when resolving row team context.

## Required canonical statistics

The HTML parser should support the following core fields:

```text
goals
behinds
disposals
kicks
handballs
marks
tackles
hitouts
```

The captured fixture also supports:

```text
dream team points
clearances
metres gained
goal assists
time on ground percentage
```

Missing values should remain missing or null. They should not be silently converted to zero unless the source explicitly supplies zero.

## Test expectations

Tests using the completed fixture should verify:

* the match metadata is extracted correctly;
* 46 player records are produced;
* both home and away players are present;
* AFL player IDs are extracted from profile links;
* Champion Data IDs are extracted from headshot filenames;
* team membership is resolved correctly;
* header labels map to the correct statistic values;
* required BBBFL statistics are present;
* additional recognised statistics are preserved;
* row and header length mismatches produce diagnostics;
* unknown columns do not shift known statistic mappings.

A representative player assertion may use Jordan Dawson:

```text
AFL player ID: 1080
Champion Data player ID: CD_I992242
Team: Adelaide
Jumper number: 12
AF: 99
Goals: 1
Behinds: 2
Disposals: 22
Kicks: 15
Handballs: 7
Marks: 2
Tackles: 7
Hitouts: 0
Clearances: 5
Metres gained: 492
Goal assists: 0
Time on ground percentage: 87
```

## Limitations

This fixture represents one completed match only.

It does not define expected behaviour for:

* unpublished pre-match statistics;
* live partial statistics;
* players with missing headshots;
* substitutes who have not entered play;
* abandoned or postponed matches;
* user-configured stat columns;
* changed column ordering;
* malformed rows;
* alternate table layouts.

Those cases should be represented by additional focused fixtures rather than modifying this completed-match fixture.

## Fixture maintenance

Fixtures should be treated as immutable once tests depend on them.

When AFL markup changes:

1. retain the original fixture where it represents a historical contract;
2. capture a new sanitised fixture;
3. add a corresponding metadata file;
4. document the observed markup change;
5. update parser tests to support both structures where practical.

Do not commit:

* `x-media-mis-token` values;
* cookies;
* authorization headers;
* full browser request dumps containing credentials;
* unrelated scripts or advertising markup;
* personal browser data.
