# v0.5.0 live and operational validation (Issue #78)

## Validation identity and disposition

| Item | Value |
| --- | --- |
| Release-candidate source revision | `1a21a13f3b899756faf19acbd22d22e7d7364a74` |
| Validation date | 1 August 2026 (UTC) |
| Environment | Codex Linux workspace; CPython 3.14.4; disposable SQLite databases under `/tmp/afl-issue-78` |
| Credentials | No CFS/WMCTok credentials were available; no token, cookie, authorization header, or raw upstream payload was captured |
| Docker/browser availability | Docker was not installed; Playwright 1.61.0 was installed but no Chromium, Firefox, or WebKit executable was present |
| Disposition | **Incomplete — do not tag v0.5.0.** The live CFS, injury, operational routing, and deployed-image gates below remain manual release blockers. |

The revision above is the exact default-branch application source tested. This
record is a documentation-only descendant of that revision; it does not claim
that an untested application change is the release candidate.

## Completed evidence

The commands below were run from a clean branch created at the recorded source
revision. Output shown here is deliberately summarised and sanitised.

### Live public metadata

```bash
timeout 120 python cli.py --collect-afl-metadata --afl-season 2026
```

The live public phase succeeded and resolved `Toyota AFL Premiership`, season
`2026 Toyota AFL Premiership`, **30 rounds, 18 teams, and 218 matches**. This was
read-only and therefore does not establish bootstrap persistence.

### Disposable database migration and integrity

```console
rm -rf /tmp/afl-issue-78 && mkdir -p /tmp/afl-issue-78
export DB_PATH=/tmp/afl-issue-78/validation.db
python -m db.migrate
python -m db.migrate
python - <<'PY'
import os, sqlite3
conn = sqlite3.connect(os.environ["DB_PATH"])
for table in ("schema_migrations", "clubs", "afl_teams", "canonical_players",
              "player_provider_ids", "competition_season_players", "cfs_player_stats"):
    print(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
print("foreign_key_check", conn.execute("PRAGMA foreign_key_check").fetchall())
print("integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0])
PY
```

All migrations `0001` through `0011` applied; the second run was already up to
date. The fresh database contained **11 migration records and 18 canonical club
seed rows**. The not-yet-bootstrapped AFL team, canonical player, provider-map,
season-membership, and CFS-stat tables each contained zero rows.
`PRAGMA foreign_key_check` returned no rows and `PRAGMA integrity_check`
returned `ok`.

### Attempted full bootstrap

```bash
export DB_PATH=/tmp/afl-issue-78/bootstrap.db
python -m db.migrate
timeout 180 python cli.py --bootstrap-afl-season 2026
```

The public phase completed, but CFS token acquisition at the environment's
outbound proxy failed with HTTP 403. The command exited non-zero. The enclosing
operation did not leave partial metadata behind: competition, season, team,
team-season, round, match, canonical-player, provider-map, and season-membership
row counts remained zero; the database still passed foreign-key and integrity
checks. This is useful rollback evidence, **not** a successful live bootstrap.

### Automated source-policy evidence

The full automated suite and focused operational tests named in the final
validation commit were run after this record was added. They validate the
repository contract that Scheduler/Admin match-stat work selects CFS JSON and
`cfs_player_stats`, exposes source/collector/persistence/count/fallback fields,
and has no implicit fallback. Mocked/fixture-backed tests do not substitute for
the required live Scheduler/Admin operation.

## Remaining manual release gates

Run the following from the recorded release SHA in the intended deployment
environment. Use a disposable database or a verified backup, set credentials
through the deployment secret mechanism, and do not add `--print-json` or raw
capture unless its output is separately sanitised and kept out of Git.

Set identifiers to a currently **published** 2026 round and concluded/live
match. Provider identifiers are deliberately placeholders here so that stale
IDs are not mistaken for guaranteed-current resources.

```console
export RELEASE_SHA=1a21a13f3b899756faf19acbd22d22e7d7364a74
export DB_PATH=/absolute/path/to/disposable/afl-v0.5.0-validation.db
export ROUND_PROVIDER_ID=CD_R...
export MATCH_PROVIDER_ID=CD_M...
export AFL_MATCH_ID=...
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
python -m db.migrate
python cli.py --bootstrap-afl-season 2026
python cli.py --bootstrap-afl-season 2026
python cli.py --collect-match-rosters "$ROUND_PROVIDER_ID"
python cli.py --collect-match-player-stats "$MATCH_PROVIDER_ID" --afl-match-id "$AFL_MATCH_ID"
python cli.py --scrape-injuries
```

Record both bootstrap summaries and verify that the second run is
non-destructive/idempotent. Then run these sanitized database checks:

```console
python - <<'PY'
import os, sqlite3
conn = sqlite3.connect(os.environ["DB_PATH"])
queries = {
    "canonical_players": "SELECT COUNT(*) FROM canonical_players",
    "afl_provider_ids": "SELECT COUNT(*) FROM player_provider_ids WHERE provider='afl'",
    "champion_data_provider_ids": "SELECT COUNT(*) FROM player_provider_ids WHERE provider='champion_data'",
    "season_memberships": "SELECT COUNT(*) FROM competition_season_players",
    "team_seasons": "SELECT COUNT(*) FROM afl_team_seasons",
    "match_stats": "SELECT COUNT(*) FROM cfs_player_stats WHERE match_provider_id=?",
}
for label, sql in queries.items():
    args = (os.environ["MATCH_PROVIDER_ID"],) if "?" in sql else ()
    print(label, conn.execute(sql, args).fetchone()[0])
print("provider_namespace_collisions", conn.execute("""
    SELECT COUNT(*) FROM player_provider_ids a
    JOIN player_provider_ids c ON c.player_id=a.player_id
    WHERE a.provider='afl' AND c.provider='champion_data'
      AND a.provider_player_id=c.provider_player_id
""").fetchone()[0])
print("foreign_key_check", conn.execute("PRAGMA foreign_key_check").fetchall())
print("integrity_check", conn.execute("PRAGMA integrity_check").fetchone()[0])
PY
```

Confirm the roster output reports `published`, `unavailable`, or a genuine
failure accurately, with `source_family=cfs_json`,
`collector=MatchRosterCollector`, read-only persistence, and
`fallback_occurred=false`. Confirm the stat output reports
`collector=MatchPlayerStatsCollector`, `persistence_target=cfs_player_stats`,
`rows_written`, its result status, and `fallback_occurred=false`, and that its
database count is non-zero when the selected resource is published.

For injuries, retain only the structured summary: `rows_parsed`,
`rows_resolved`, `rows_persisted`, `rows_unresolved`, and `rows_ambiguous`.
Check that parsed equals resolved plus unresolved plus ambiguous, and persisted
does not exceed resolved. Compare with the prior 155/155/155/0/0 observation
only as live evidence, never as a fixed assertion. If the result is partial,
verify unresolved identities were skipped and previously current injuries were
not expired.

### Scheduler/Admin routing gate

With Scheduler running against the bootstrapped database, submit one known AFL
numeric match ID and retain the sanitized response:

```bash
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  -d "{\"match_id\":${AFL_MATCH_ID}}" \
  http://127.0.0.1:8002/scheduler/manual/player-stats/match
```

The enqueue response must show `selected_source=cfs_json`, the CFS player-stat
collector, persistent behavior, pending counts, and
`fallback_occurred=false`. After completion, inspect sanitized service logs and
the correlated `scrape_runs`/registry row; confirm completed status, collected
and persisted counts, a CFS-stat row-count increase, and no legacy
`player_stats` write. Treat a merely queued response as insufficient evidence.

### Release-image and service gate

Docker was unavailable in the workspace, so no image identifier, packaged
browser check, service startup, persistent-volume check, or health result can
be claimed. The repository's example Compose file is development-oriented and
must not be used for this gate because it has source bind mounts and API reload
mode. From a secret-populated deployment environment file that is outside Git:

```bash
git checkout --detach "$RELEASE_SHA"
export IMAGE="afl-api:v0.5.0-${RELEASE_SHA:0:12}"
docker build --pull --label org.opencontainers.image.revision="$RELEASE_SHA" \
  -t "$IMAGE" .
docker image inspect "$IMAGE" \
  --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
docker run --rm "$IMAGE" \
  python -c "from pathlib import Path; import playwright; assert Path('/app/db/migrations').is_dir(); assert Path('/app/bootstrap/clubs.json').is_file()"
docker volume create afl-v050-validation-data
docker run --rm --env-file /secure/path/afl-v050.env \
  -e DB_PATH=/app/data/validation.db -v afl-v050-validation-data:/app/data \
  "$IMAGE" python -m db.migrate
docker run -d --name afl-v050-api --env-file /secure/path/afl-v050.env \
  -e DB_PATH=/app/data/validation.db -v afl-v050-validation-data:/app/data \
  -p 127.0.0.1:8000:8000 "$IMAGE"
docker run -d --name afl-v050-scheduler --env-file /secure/path/afl-v050.env \
  -e DB_PATH=/app/data/validation.db -v afl-v050-validation-data:/app/data \
  -p 127.0.0.1:8002:8000 "$IMAGE" \
  uvicorn scheduler.start:app --host 0.0.0.0 --port 8000
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
curl --fail http://127.0.0.1:8002/healthz
curl --fail http://127.0.0.1:8002/readyz
docker logs afl-v050-scheduler 2>&1 | tail -n 200
```

Confirm the containers have no source bind mount, neither command contains
`--reload`, both use the intended named persistent volume/configuration, the
Scheduler registry has no duplicate jobs and no missing required jobs, and the
image's Playwright package can launch its bundled Chromium. Record only the
image ID/digest, health responses, registry summary, row counts, and sanitized
collector diagnostics. Review all evidence for secrets before attaching it.

## Release decision

Completed evidence is limited to the exact source revision, live public
metadata, clean migrations/idempotency, database integrity, bootstrap atomicity
on CFS transport failure, and automated contracts. All CFS-authenticated,
browser-backed injury, deployed operational-routing, Docker packaging/startup,
and service health/readiness criteria remain unresolved manual gates. Issue #78
must therefore be referenced rather than closed, and `v0.5.0` must not be
tagged from this evidence alone.
