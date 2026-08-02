# AFL-api v0.5.0 operator release, backup, restore, and rollback runbook

This is the authoritative operator sequence for v0.5.0. Complete it in order and
retain the release record in section 21.

For day-to-day command selection and the concise clean-install sequence, start
with the [operator command guide](../cli.md#which-command-should-i-run). This
runbook remains authoritative for production backup, migration, release,
verification, restore, and rollback gates.

## 1. Purpose and scope

This runbook releases AFL-api v0.5.0 as the API and Scheduler processes backed
by one persistent SQLite database. It applies to a controlled production-like
Docker deployment built from this repository; `compose.example.yaml` is the
repository's only Compose definition, but is explicitly development-oriented
(`--reload` and a source bind mount), so an operator's production override must
remove those settings.

It does not provision hosts, proxies, TLS, secrets, monitoring, or a production
Compose override, and it does not replace the detailed guides in section 22.
Review every command against the actual deployment, secret store, volume,
ownership, and retention policy before execution. Placeholders and assumptions
are called out; never paste a production secret into the release record.

## 2. Command notation and safety conventions

Every command is preceded by one of these execution labels:

* **HOST** — repository/deployment host, from `$REPO_DIR` unless stated.
* **API CONTAINER** — shell in service `afl-api`.
* **SCHEDULER CONTAINER** — shell in service `afl-scheduler`.
* **SQLITE** — SQLite CLI connected to the explicitly shown file.
* **GITHUB** — authenticated Git/GitHub CLI on the host.

**PRODUCTION-SENSITIVE** means a command changes availability or persistent
state. **DESTRUCTIVE** means it can replace/delete state. These words, not
colour or icons, are the safety signal.

* **Success:** the stated output/exit status is observed and recorded.
* **Warning:** investigate a difference before continuing; do not assume it is
  harmless.
* **STOP:** do not tag or continue; keep writers stopped where data safety is in
  doubt, preserve evidence, and use sections 19–20.
* **Rollback decision point:** decide whether only the application changed or
  whether migration/bootstrap/writes require the verified database backup.

Use `set -euo pipefail`. Read a whole block before running it. Commands that
contact AFL/CFS need working credentials/network access and may expose upstream
data in output; do not enable raw capture or `--print-json` in release evidence.

## 3. Release variables

**HOST** — fill and verify these values. Defaults come from this repository;
`DB_PATH` must be the host-visible path to the active database, not the default
path inside a container. `COMPOSE_FILE` may instead name a reviewed production
override. `AFL_SEASON=2026` is the v0.5.0 bootstrap target; change it only when
the approved release scope says otherwise.

```bash
set -euo pipefail
export REPO_DIR=/absolute/path/to/AFL-api
export RELEASE_VERSION=0.5.0
export RELEASE_TAG=v0.5.0
export RELEASE_SHA=REPLACE_WITH_FULL_40_CHARACTER_SHA
export PREVIOUS_REF=REPLACE_WITH_KNOWN_GOOD_IMMUTABLE_TAG_OR_SHA
export DB_PATH=/absolute/host/path/to/afl_players.db
export BACKUP_DIR=/absolute/host/path/to/backups
export RELEASE_UTC="$(date -u +%Y%m%dT%H%M%SZ)"
export BACKUP_FILE="$BACKUP_DIR/afl_players-${RELEASE_TAG}-${RELEASE_UTC}.sqlite3"
export COMPOSE_FILE=compose.example.yaml
export COMPOSE_PROJECT_NAME=afl-api
export IMAGE_NAME="afl-api:${RELEASE_VERSION}-${RELEASE_SHA:0:12}"
export API_BASE_URL=http://127.0.0.1:8000
export AFL_SEASON=2026
export API_KEY=REPLACE_WITH_RUNTIME_API_KEY
cd "$REPO_DIR"
```

The configured application default is `data/afl_players.db`, resolved from the
repository root. Compose mounts volume `afl-api-data` at `/app/data`, so its
default database is `/app/data/afl_players.db`; determine the host/volume path
rather than assuming the named-volume mountpoint.

## 4. Release prerequisites

**HOST** — read-only checks (apart from creating the backup directory):

```bash
set -euo pipefail
cd "$REPO_DIR"
test -z "$(git status --porcelain)"
test "$(git branch --show-current)" = main
git fetch --prune origin main --tags
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
git fsck --no-dangling
python --version
python -m pytest -q
python -m compileall -q afl_json collection db merge scraper scheduler cli.py tests
python cli.py --help
docker version
docker compose version
df -h "$REPO_DIR" "$(dirname "$DB_PATH")" "$BACKUP_DIR"
test -r "$DB_PATH" && test -w "$DB_PATH"
mkdir -p "$BACKUP_DIR" && test -w "$BACKUP_DIR"
docker compose -f "$COMPOSE_FILE" config --services
docker compose -f "$COMPOSE_FILE" ps -a
gh auth status
git ls-remote --exit-code origin >/dev/null
```

Success requires a clean `main` equal to `origin/main`, all tests/checks passing,
Docker Engine and Compose v2 usable, ample free space for at least two database
copies plus image/build growth, an accessible database and backup destination,
and authenticated push/release access. `config --services` must list exactly
`afl-api`, `afl-admin`, and `afl-scheduler`. Record current state. **STOP** on a
test failure, inaccessible path, unexpected service, or insufficient capacity.

Check for out-of-band writers before the outage. The repository-controlled
writers are: API requests that invoke persistence, Admin-triggered Scheduler
jobs, the Scheduler's injury/fixture/match/lineup/stat/refresh jobs, `cli.py`
persistent collectors/bootstrap/imports, direct scraper entry points, and
`python -m db.migrate`/`db.init_db`. Also inspect host processes and database
holders; an empty result is expected after shutdown in section 6.

```bash
pgrep -af 'cli\.py|db\.(migrate|init_db)|scheduler\.start|uvicorn|scrape' || true
command -v lsof >/dev/null && lsof "$DB_PATH" || true
```

## 5. Exact release SHA verification

**HOST** — resolve once, then use the full SHA or the immutable image name:

```bash
set -euo pipefail
cd "$REPO_DIR"
git fetch --prune origin main --tags
test -z "$(git status --porcelain)"
export RELEASE_SHA="$(git rev-parse --verify 'origin/main^{commit}')"
test "${#RELEASE_SHA}" -eq 40
git switch --detach "$RELEASE_SHA"
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain)"
git show --no-patch --format=fuller "$RELEASE_SHA"
git branch -r --contains "$RELEASE_SHA"
printf 'RELEASE_SHA=%s\n' "$RELEASE_SHA" | tee "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}.sha"
export IMAGE_NAME="afl-api:${RELEASE_VERSION}-${RELEASE_SHA:0:12}"
```

Success is one unambiguous 40-character SHA, a detached clean checkout at that
SHA, and `origin/main` shown as containing it. **STOP** if the approved commit
differs. Never resume later using a newly moved `main` or `latest`.

## 6. Stop database writers safely

**PRODUCTION-SENSITIVE — HOST.** Announce the write outage, disable external
traffic/manual Admin actions at the deployment boundary, terminate any running
CLI/import/migration process gracefully, then stop all three repository
services. `docker compose stop` sends SIGTERM; Scheduler shutdown waits for its
executor, so wait for completion rather than killing it.

```bash
docker compose -f "$COMPOSE_FILE" stop -t 120 afl-admin afl-scheduler afl-api
docker compose -f "$COMPOSE_FILE" ps -a
pgrep -af 'cli\.py|db\.(migrate|init_db)|scheduler\.start|uvicorn|scrape' || true
command -v lsof >/dev/null && lsof "$DB_PATH" || true
```

Success: every service is stopped, no relevant host process remains, and `lsof`
shows no database, `-wal`, or `-shm` holder (when available). **STOP** and locate
the writer if any remains. Do not use `kill -9` unless graceful shutdown has
failed and the incident is recorded; re-check SQLite integrity afterwards.

## 7. SQLite checkpoint and backup

**PRODUCTION-SENSITIVE — HOST/SQLITE.** This uses SQLite's online backup API
through the CLI after writers are stopped. It is safe for WAL databases and is
preferred to `cp`.

```bash
set -euo pipefail
test -f "$DB_PATH"
stat "$DB_PATH"
sqlite3 "$DB_PATH" 'PRAGMA journal_mode; PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(TRUNCATE);'
umask 077
mkdir -p "$BACKUP_DIR"
test ! -e "$BACKUP_FILE"
sqlite3 "$DB_PATH" ".timeout 5000" ".backup '$BACKUP_FILE'"
chmod --reference="$DB_PATH" "$BACKUP_FILE"
if test "$(id -u)" -eq 0; then chown --reference="$DB_PATH" "$BACKUP_FILE"; fi
sha256sum "$BACKUP_FILE" | tee "$BACKUP_FILE.sha256"
printf 'source_sha=%s\ncreated_utc=%s\nsource_db=%s\n' \
  "$RELEASE_SHA" "$(date -u +%FT%TZ)" "$DB_PATH" | tee "$BACKUP_FILE.metadata"
sqlite3 -readonly "$BACKUP_FILE" 'PRAGMA integrity_check;'
stat "$BACKUP_FILE" "$BACKUP_FILE.sha256" "$BACKUP_FILE.metadata"
```

`journal_mode` may report `wal` or another valid configured mode. A checkpoint
row is `busy|log|checkpointed`; success requires `busy=0`. Backup creation must
not overwrite an existing file, checksum output must be retained, ownership and
mode must match the source (run `chown --reference` with the deployment's
privilege mechanism if the operator is not root), and integrity must be exactly
`ok`. **STOP** otherwise. A plain file copy is safe only after every writer and
reader is closed and a successful truncating checkpoint has removed/emptied WAL
state; even then, prefer `.backup`.

## 8. Backup restoration rehearsal

**HOST/SQLITE — isolated and non-destructive.** This never names production:

```bash
set -euo pipefail
sha256sum --check "$BACKUP_FILE.sha256"
export RESTORE_DIR="$(mktemp -d "$BACKUP_DIR/restore-rehearsal.XXXXXX")"
export RESTORE_DB="$RESTORE_DIR/restored.sqlite3"
sqlite3 "$BACKUP_FILE" ".backup '$RESTORE_DB'"
sqlite3 -readonly "$RESTORE_DB" <<'SQL'
PRAGMA integrity_check;
PRAGMA foreign_key_check;
SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name;
SELECT migration_id, description, applied_at FROM schema_migrations ORDER BY migration_id;
SELECT 'clubs', COUNT(*) FROM clubs
UNION ALL SELECT 'competitions', COUNT(*) FROM afl_competitions
UNION ALL SELECT 'seasons', COUNT(*) FROM afl_seasons
UNION ALL SELECT 'players', COUNT(*) FROM canonical_players
UNION ALL SELECT 'memberships', COUNT(*) FROM competition_season_players;
SELECT COUNT(*) AS broken_memberships
FROM competition_season_players sp
LEFT JOIN canonical_players p ON p.id=sp.player_id
LEFT JOIN afl_seasons s ON s.afl_id=sp.competition_season_id
WHERE p.id IS NULL OR s.afl_id IS NULL;
SQL
rm -f "$RESTORE_DB"
rmdir "$RESTORE_DIR"
```

Success: checksum passes, integrity is exactly `ok`, foreign-key check prints no
rows, section 11's expected tables/migrations appear, `clubs` is 18, production
baseline counts are plausible/non-decreasing, and `broken_memberships` is 0.
Keep the original backup. **STOP** on any mismatch; do not clean up a failed
rehearsal until evidence is captured.

## 9. Migration execution

There is no reverse migration mechanism. The repository runner discovers
ordered `db/migrations/NNNN_*.py`, validates recorded checksums, and records
them in `schema_migrations`.

**HOST/SQLITE — inspect before migrating while API and Scheduler remain stopped:**

```bash
sqlite3 -readonly "$DB_PATH" \
  'SELECT migration_id, description, checksum, applied_at FROM schema_migrations ORDER BY migration_id;'
find db/migrations -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]_*.py' -printf '%f\n' | sort
```

Compare recorded IDs with filenames. v0.5.0 expects `0001` through `0011` after
migration. **PRODUCTION-SENSITIVE — HOST** (uses configured `DB_PATH`):

```bash
export DB_PATH
python -m db.migrate 2>&1 | tee "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-migration.log"
python -m db.migrate
sqlite3 -readonly "$DB_PATH" \
  'SELECT migration_id, description, applied_at FROM schema_migrations ORDER BY migration_id; PRAGMA integrity_check; PRAGMA foreign_key_check;'
```

Expected first output lists applied IDs or says the database is already up to
date; the idempotent second run is up to date. Success is 11 ordered records
(`0001`–`0011`), integrity `ok`, no FK rows, and exit zero. **STOP** on failure;
keep writers stopped, preserve logs/database, and decide on verified-backup
restoration in section 20. Do not retry blindly.

> Never simulate or fake a rollback by manually editing the `schema_migrations` table.

Do not reverse migrations: the repository has no tested down mechanism.

## 10. Full-season bootstrap

The supported v0.5.0 operation is JSON-first `--bootstrap-afl-season SEASON`.
For `2026`, it collects AFL public JSON competition/season/round/team/match
metadata, then authenticated CFS players, and atomically persists canonical
players, provider IDs, team seasons, and season membership. Competition defaults
are `AFL` and `CD_C014`; override them only with approved deployment values.
There is no source flag or HTML fallback in this command.

**PRODUCTION-SENSITIVE — HOST, API and Scheduler still stopped:**

```bash
export DB_PATH
{ python cli.py --bootstrap-afl-season "$AFL_SEASON"; } 2>&1 | \
  tee "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-bootstrap.log"
{ python cli.py --bootstrap-afl-season "$AFL_SEASON"; } 2>&1 | \
  tee "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-bootstrap-rerun.log"
```

Success requires exit zero and summaries showing persisted metadata and players.
The second run is the supported idempotency rehearsal: upserts must not create
duplicate identities/memberships. No reliable duration is promised. Network,
authentication, validation, or persistence errors are failures; the operation
is transaction-wrapped and a failed attempt should leave no partial hierarchy,
but verify rather than assume. Only the entire bootstrap may safely be rerun
after diagnosing a failure. The read-only `--collect-afl-metadata` can diagnose
public access; roster JSON is also read-only. Do not substitute `--scrape-*`
HTML commands for bootstrap. **STOP** if either required run or section 11 fails.

## 11. Post-bootstrap database verification

**SQLITE** — run against `$DB_PATH`; each result is a release gate:

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;

SELECT migration_id FROM schema_migrations ORDER BY migration_id;
SELECT name FROM sqlite_schema
WHERE type='table' AND name IN (
 'afl_competitions','afl_seasons','afl_team_seasons','afl_teams','api_keys',
 'canonical_players','cfs_player_stats','clubs','competition_season_players',
 'injuries','lineups','matches','player_provider_ids','player_stats','players',
 'rounds','scheduler_job_registry','schema_migrations','scrape_runs')
ORDER BY name;

SELECT 'clubs',COUNT(*) FROM clubs
UNION ALL SELECT 'competitions',COUNT(*) FROM afl_competitions
UNION ALL SELECT 'seasons',COUNT(*) FROM afl_seasons
UNION ALL SELECT 'rounds',COUNT(*) FROM rounds
UNION ALL SELECT 'matches',COUNT(*) FROM matches
UNION ALL SELECT 'teams',COUNT(*) FROM afl_teams
UNION ALL SELECT 'canonical_players',COUNT(*) FROM canonical_players
UNION ALL SELECT 'provider_ids',COUNT(*) FROM player_provider_ids
UNION ALL SELECT 'memberships',COUNT(*) FROM competition_season_players
UNION ALL SELECT 'lineups',COUNT(*) FROM lineups
UNION ALL SELECT 'injuries',COUNT(*) FROM injuries
UNION ALL SELECT 'legacy_player_stats',COUNT(*) FROM player_stats
UNION ALL SELECT 'cfs_player_stats',COUNT(*) FROM cfs_player_stats;

SELECT COUNT(*) AS null_required_identity
FROM competition_season_players
WHERE player_id IS NULL OR competition_season_id IS NULL OR source_provider IS NULL;

SELECT provider,provider_player_id,COUNT(*) AS duplicates
FROM player_provider_ids GROUP BY provider,provider_player_id HAVING COUNT(*)>1;
SELECT player_id,provider,COUNT(*) AS duplicates
FROM player_provider_ids GROUP BY player_id,provider HAVING COUNT(*)>1;
SELECT player_id,competition_season_id,COUNT(*) AS duplicates
FROM competition_season_players GROUP BY player_id,competition_season_id HAVING COUNT(*)>1;

SELECT COUNT(*) AS orphan_rounds FROM rounds r
LEFT JOIN afl_seasons s ON s.afl_id=r.season_id WHERE s.afl_id IS NULL;
SELECT COUNT(*) AS orphan_matches FROM matches m
LEFT JOIN rounds r ON r.round_id=m.round_id
LEFT JOIN afl_teams h ON h.afl_id=m.home_team_id
LEFT JOIN afl_teams a ON a.afl_id=m.away_team_id
WHERE r.round_id IS NULL OR h.afl_id IS NULL OR a.afl_id IS NULL;
SELECT COUNT(*) AS orphan_memberships FROM competition_season_players sp
LEFT JOIN canonical_players p ON p.id=sp.player_id
LEFT JOIN afl_seasons s ON s.afl_id=sp.competition_season_id
LEFT JOIN afl_team_seasons ats ON ats.competition_season_id=sp.competition_season_id
 AND ats.team_id=sp.team_id
WHERE p.id IS NULL OR s.afl_id IS NULL OR (sp.team_id IS NOT NULL AND ats.team_id IS NULL);
SELECT COUNT(*) AS orphan_injuries FROM injuries i
LEFT JOIN player_provider_ids pi ON pi.provider='afl'
 AND pi.provider_player_id=CAST(i.afl_id AS TEXT) WHERE pi.player_id IS NULL;
SELECT COUNT(*) AS orphan_lineups FROM lineups l
LEFT JOIN matches m ON CAST(m.match_id AS TEXT)=l.match_id
LEFT JOIN player_provider_ids pi ON pi.provider='afl'
 AND pi.provider_player_id=CAST(l.afl_id AS TEXT)
WHERE m.match_id IS NULL OR pi.player_id IS NULL;
SELECT COUNT(*) AS orphan_legacy_stats FROM player_stats ps
LEFT JOIN matches m ON m.match_id=ps.match_id
LEFT JOIN player_provider_ids pi ON pi.provider='afl'
 AND pi.provider_player_id=CAST(ps.afl_id AS TEXT)
WHERE m.match_id IS NULL OR (ps.afl_id IS NOT NULL AND pi.player_id IS NULL);
SELECT COUNT(*) AS orphan_cfs_stats FROM cfs_player_stats cs
LEFT JOIN matches m ON m.match_provider_id=cs.match_provider_id
LEFT JOIN canonical_players p ON p.id=cs.canonical_player_id
WHERE m.match_id IS NULL OR (cs.canonical_player_id IS NOT NULL AND p.id IS NULL);

SELECT c.code,s.year,r.round_number,m.match_id,h.name AS home,a.name AS away
FROM afl_competitions c JOIN afl_seasons s ON s.competition_id=c.afl_id
JOIN rounds r ON r.season_id=s.afl_id JOIN matches m ON m.round_id=r.round_id
JOIN afl_teams h ON h.afl_id=m.home_team_id JOIN afl_teams a ON a.afl_id=m.away_team_id
WHERE s.year=2026 LIMIT 5;
SELECT p.id,p.display_name,pi.provider,pi.provider_player_id,s.year,t.name
FROM canonical_players p JOIN player_provider_ids pi ON pi.player_id=p.id
JOIN competition_season_players sp ON sp.player_id=p.id
JOIN afl_seasons s ON s.afl_id=sp.competition_season_id
LEFT JOIN afl_teams t ON t.afl_id=sp.team_id WHERE s.year=2026 LIMIT 10;
```

Success: integrity is exactly `ok`; FK check, duplicate queries, all orphan/null
queries return no rows or `0`; migration IDs are exactly `0001`–`0011`; all 19
named canonical/operational tables appear; `clubs=18`; bootstrapped competition,
season, round, match, team, canonical-player, provider, and membership counts
are non-zero; rerun counts do not inflate; representative joins return sensible
2026 rows. Lineup/injury/stat tables may legitimately be zero before their
separate collectors, but any non-zero table must have zero orphans. **STOP** on
any other result; do not “repair” records ad hoc.

## 12. Docker build and startup

**HOST.** Build from the detached exact SHA. `--pull --no-cache` avoids a stale
base/cache; the revision label makes provenance inspectable.

```bash
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain)"
docker build --pull --no-cache \
  --label "org.opencontainers.image.version=$RELEASE_VERSION" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  -t "$IMAGE_NAME" .
docker image inspect "$IMAGE_NAME" --format \
  '{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
docker run --rm "$IMAGE_NAME" python -c \
  "from pathlib import Path; assert Path('/app/db/migrations').is_dir(); assert Path('/app/bootstrap/clubs.json').is_file()"
```

The repository Compose file builds its own service images and bind-mounts the
checkout; it cannot consume `$IMAGE_NAME` without an operator override. In the
reviewed production Compose configuration, pin each service to `$IMAGE_NAME`,
remove `.:/app`, remove API `--reload`, retain the shared data volume, then:

```bash
docker compose -f "$COMPOSE_FILE" config > "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-compose.yaml"
docker compose -f "$COMPOSE_FILE" config --images
test "$(docker compose -f "$COMPOSE_FILE" config --images | sort -u)" = "$IMAGE_NAME"
docker compose -f "$COMPOSE_FILE" up -d --no-build afl-api
docker compose -f "$COMPOSE_FILE" up -d --no-build afl-admin afl-scheduler
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" logs --since=10m --tail=300 afl-api afl-admin afl-scheduler
docker compose -f "$COMPOSE_FILE" images
docker compose -f "$COMPOSE_FILE" exec -T afl-api python -c \
  'import os; print(os.environ.get("DB_PATH", "data/afl_players.db"))'
```

Starting API before Scheduler makes database/readiness validation observable
before writers resume. Success: intended images start, API and Scheduler become
healthy, logs have no migration/startup traceback, and the captured resolved
configuration has the intended DB mount and immutable image/revision. Because
`compose.example.yaml` is not production-safe, treating it unchanged as a
production deployment is a **STOP** condition.

## 13. Health and readiness verification

API and Scheduler both include the same endpoints. `/healthz` is liveness and
returns HTTP 200 `{"status":"ok"}` without touching SQLite. `/readyz` executes
`SELECT 1` through the configured connection and returns the same response, or
HTTP 503 `{"status":"unavailable"}`. It proves database connectivity, not
migration completeness, so query migrations separately. Scheduler has no
different readiness endpoint; use its health, job API, registry, and logs.

**HOST/API CONTAINER/SCHEDULER CONTAINER:**

```bash
curl --fail-with-body --silent --show-error "$API_BASE_URL/healthz"
curl --fail-with-body --silent --show-error "$API_BASE_URL/readyz"
docker compose -f "$COMPOSE_FILE" exec -T afl-api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
docker compose -f "$COMPOSE_FILE" exec -T afl-scheduler \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read().decode())"
docker compose -f "$COMPOSE_FILE" exec -T afl-scheduler \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/scheduler/jobs').status)"
docker compose -f "$COMPOSE_FILE" ps
sqlite3 -readonly "$DB_PATH" 'SELECT COUNT(*) FROM schema_migrations;'
```

Success is the exact JSON, HTTP 200, both Compose health states `healthy`, an
HTTP 200 Scheduler job listing, and migration count 11. **STOP** on persistent
failure; liveness alone is insufficient.

## 14. Operational smoke sequence

Use authenticated read APIs and non-mutating Scheduler inspection first.
Select an existing round/match from SQLite; never guess IDs. The persistence
smoke is the second full bootstrap already completed in section 10: it is the
repository-supported transactional/idempotent write path and avoids duplicate
production ingestion. A live collection beyond that requires an explicitly
approved current resource and is a release gate described in the validation
report, not a guessed example here.

**HOST:**

```bash
export SMOKE_ROUND_ID="$(sqlite3 -readonly "$DB_PATH" 'SELECT round_id FROM rounds ORDER BY round_id LIMIT 1;')"
export SMOKE_MATCH_ID="$(sqlite3 -readonly "$DB_PATH" 'SELECT match_id FROM matches ORDER BY match_id LIMIT 1;')"
test -n "$SMOKE_ROUND_ID" && test -n "$SMOKE_MATCH_ID"
curl --fail-with-body --silent --show-error "$API_BASE_URL/"
curl --fail-with-body --silent --show-error -H "X-API-Key: $API_KEY" \
  "$API_BASE_URL/api/rounds/$SMOKE_ROUND_ID"
curl --fail-with-body --silent --show-error -H "X-API-Key: $API_KEY" \
  "$API_BASE_URL/api/matches/$SMOKE_MATCH_ID"
docker compose -f "$COMPOSE_FILE" exec -T afl-scheduler \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/scheduler/jobs').read().decode())" \
  > "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-scheduler-jobs.json"
sqlite3 -readonly "$DB_PATH" \
  'SELECT status,COUNT(*) FROM scheduler_job_registry GROUP BY status; SELECT status,COUNT(*) FROM scrape_runs GROUP BY status;'
docker compose -f "$COMPOSE_FILE" logs --since=15m afl-api afl-scheduler 2>&1 | \
  tee "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-smoke.log"
! grep -Eiq 'traceback|critical|database is locked|foreign key constraint failed' \
  "$BACKUP_DIR/${RELEASE_TAG}-${RELEASE_UTC}-smoke.log"
```

Success: root message is returned; authenticated round/match reads succeed;
the Scheduler list is non-empty with expected cron/dynamic registry entries and
no unexplained `failed`/`absent` state; bootstrap rerun and section 11 prove safe
persistence/canonical relationships; logs contain no critical pattern. Record
an approved Scheduler job's later `last_success_time` as post-release evidence.
**STOP** rather than trigger an uncertain ingestion merely to make the smoke pass.

## 15. Release decision gate

Do not proceed to tagging until every item is checked:

- [ ] Exact release SHA and clean checkout recorded.
- [ ] CI/tests and compile/CLI checks pass at that SHA.
- [ ] WAL-safe backup checksum/open verification passes.
- [ ] Isolated restoration rehearsal passes.
- [ ] Integrity and foreign-key checks pass.
- [ ] Migrations `0001`–`0011` pass and are idempotent.
- [ ] Full-season bootstrap and rerun pass.
- [ ] Post-bootstrap schema, count, duplicate, orphan, and join checks pass.
- [ ] Immutable image provenance and service startup pass.
- [ ] API/Scheduler health, readiness, and migration readiness pass.
- [ ] Operational smoke and critical-log inspection pass.
- [ ] Environment-dependent gates in the release-readiness/validation reports
      (CFS, injury, Scheduler routing, browser/image) are complete.

Any unchecked item means **STOP THE RELEASE**. Before publication, fix and
repeat from the appropriate safe point; after deployment, apply sections 19–20.

## 16. Annotated tag creation

**GITHUB — only after section 15.** Never move/recreate a published tag.

```bash
set -euo pipefail
git fetch origin --tags
if git show-ref --verify --quiet "refs/tags/$RELEASE_TAG" || \
   git ls-remote --exit-code --tags origin "refs/tags/$RELEASE_TAG" >/dev/null 2>&1; then
  echo "STOP: tag already exists" >&2
  exit 1
fi
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain)"
git tag -a "$RELEASE_TAG" "$RELEASE_SHA" -m "AFL-api $RELEASE_VERSION"
test "$(git rev-list -n 1 "$RELEASE_TAG")" = "$RELEASE_SHA"
git show --no-patch --format=fuller "$RELEASE_TAG"
git push origin "refs/tags/$RELEASE_TAG"
test "$(git ls-remote origin "refs/tags/$RELEASE_TAG^{}" | awk '{print $1}')" = "$RELEASE_SHA"
```

Success: the local annotated tag and remotely peeled tag both resolve exactly
to `$RELEASE_SHA`. **STOP** if an existing tag is found; investigate rather than
force-pushing or silently recreating it.

## 17. GitHub Release creation

**GITHUB.** Prepare reviewed notes describing source policy, canonical
bootstrap/injuries, migrations `0009`–`0011`, backup/restore requirement, known
HTML boundaries and both stats tables. Link
`docs/architecture/release_readiness_v0_5_0.md`, this runbook, and
`docs/architecture/release_validation_v0_5_0_issue_78.md`. v0.5.0 is a stable
release, not a prerelease, unless the release decision explicitly changes.

```bash
gh release create "$RELEASE_TAG" \
  --verify-tag \
  --title "AFL-api v0.5.0" \
  --notes-file /absolute/path/to/reviewed-v0.5.0-release-notes.md
gh release view "$RELEASE_TAG" --json url,tagName,isDraft,isPrerelease,targetCommitish
test "$(git ls-remote origin "refs/tags/$RELEASE_TAG^{}" | awk '{print $1}')" = "$RELEASE_SHA"
```

Success: published (not draft), correct title/tag/notes, not prerelease unless
approved, and remote peeled tag equals the recorded SHA. The `targetCommitish`
display is not a substitute for peeled-tag verification. **This documentation
change must not execute sections 16 or 17.**

## 18. Post-release checks

Immediately, again after the next expected Scheduler execution, and at the
operator's normal delayed observation interval, repeat sections 11, 13, and 14
plus:

```bash
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" images
docker compose -f "$COMPOSE_FILE" logs --since=1h afl-api afl-scheduler
df -h "$(dirname "$DB_PATH")" "$BACKUP_DIR"
du -h "$DB_PATH" "$DB_PATH-wal" "$DB_PATH-shm" 2>/dev/null || true
sqlite3 -readonly "$DB_PATH" \
  "SELECT job_id,status,last_attempt_time,last_success_time,last_error_summary FROM scheduler_job_registry ORDER BY updated_at DESC LIMIT 25; SELECT run_id,scrape_type,status,rows_read,rows_written,finished_at FROM scrape_runs ORDER BY started_at DESC LIMIT 25;"
docker image inspect "$IMAGE_NAME" --format \
  '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Retain timestamped health/API output, image ID/digest and revision, Compose
config/service state, sanitized logs, Scheduler job/run summaries, database
counts/relationship checks, disk/WAL sizes, release SHA/tag, backup checksum,
and operator decision. Escalate unexplained WAL growth, failed/stuck jobs,
missing writes, count regressions, critical logs, or SHA/image mismatch.

## 19. Rollback triggers

Rollback/stop triggers include: migration failure; non-`ok` integrity or any FK
row; failed/non-atomic bootstrap; persistent unhealthy/unready services;
incorrect source/image SHA; severe ingestion, duplicate, orphan, or identity
corruption; and unrecoverable application startup failure.

* **Before publication:** stop, leave tag/release uncreated, preserve evidence,
  and restore the backup if migration or writes changed the database.
* **Application rollback after deployment:** previous immutable image/SHA may be
  sufficient only when the database remains compatible and uncorrupted.
* **Database restoration:** required after an incompatible migration or
  corrupting write. It is a separate, production-sensitive decision because it
  discards all writes after the backup.

## 20. Full rollback procedure

**DESTRUCTIVE AND PRODUCTION-SENSITIVE.** Obtain incident authority and record
the data-loss boundary before restoring. Deploying the previous application is
not sufficient if an incompatible migration altered SQLite.

1. **HOST — stop writers** exactly as section 6.
2. **HOST — preserve failed state** without copying a live WAL:

```bash
export FAILED_DIR="$BACKUP_DIR/failed-${RELEASE_TAG}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$FAILED_DIR"
docker compose -f "$COMPOSE_FILE" logs --no-color > "$FAILED_DIR/compose.log" 2>&1 || true
docker compose -f "$COMPOSE_FILE" ps -a > "$FAILED_DIR/compose-ps.txt" || true
sqlite3 "$DB_PATH" 'PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(FULL);'
sqlite3 "$DB_PATH" ".backup '$FAILED_DIR/failed-state.sqlite3'"
sha256sum "$FAILED_DIR/failed-state.sqlite3" > "$FAILED_DIR/failed-state.sqlite3.sha256"
```

3. **HOST — select/verify the prior immutable application:**

```bash
git fetch origin --tags
export PREVIOUS_SHA="$(git rev-parse --verify "$PREVIOUS_REF^{commit}")"
test "${#PREVIOUS_SHA}" -eq 40
git show --no-patch "$PREVIOUS_SHA"
export PREVIOUS_IMAGE="afl-api:previous-${PREVIOUS_SHA:0:12}"
export PREVIOUS_WORKTREE="$(mktemp -d)"
git worktree add --detach "$PREVIOUS_WORKTREE" "$PREVIOUS_SHA"
docker build --pull --label "org.opencontainers.image.revision=$PREVIOUS_SHA" \
  -t "$PREVIOUS_IMAGE" "$PREVIOUS_WORKTREE"
git worktree remove "$PREVIOUS_WORKTREE"
```

If the prior image digest was recorded, pull/use that immutable digest instead
of rebuilding. Update the reviewed deployment override to `$PREVIOUS_IMAGE`.

4. **Rollback decision point:** if migrations/schema or a corrupting write
occurred, restore the verified pre-release backup. If only application code
failed and schema/data are compatible and pass all checks, retain the current
database. Record the decision and approver.

5. **DESTRUCTIVE — HOST, only when database restoration is approved:**

```bash
sha256sum --check "$BACKUP_FILE.sha256"
sqlite3 -readonly "$BACKUP_FILE" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
export RESTORE_STAGE="$(dirname "$DB_PATH")/.restore-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
test ! -e "$RESTORE_STAGE"
sqlite3 "$BACKUP_FILE" ".backup '$RESTORE_STAGE'"
chmod --reference="$DB_PATH" "$RESTORE_STAGE"
if test "$(id -u)" -eq 0; then chown --reference="$DB_PATH" "$RESTORE_STAGE"; fi
sqlite3 -readonly "$RESTORE_STAGE" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
mv "$DB_PATH" "$FAILED_DIR/pre-restore-production.sqlite3"
rm -f "$DB_PATH-wal" "$DB_PATH-shm"
mv "$RESTORE_STAGE" "$DB_PATH"
```

The atomic `mv` assumption requires staging on the same filesystem. Removing
WAL/SHM is safe here only because all writers/readers are stopped and the main
database has been replaced by a self-contained verified backup. Restoration
discards writes made after backup time; reconcile them only through an approved
application workflow, never hand-edited SQL.

6. **HOST — start previous API first, then Admin/Scheduler, and verify:**

```bash
docker compose -f "$COMPOSE_FILE" up -d afl-api
curl --fail-with-body --silent --show-error "$API_BASE_URL/readyz"
sqlite3 -readonly "$DB_PATH" \
  'PRAGMA integrity_check; PRAGMA foreign_key_check; SELECT migration_id FROM schema_migrations ORDER BY migration_id;'
docker compose -f "$COMPOSE_FILE" up -d afl-admin afl-scheduler
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" logs --since=10m afl-api afl-scheduler
```

Then rerun the compatible health, smoke, relationship, image/SHA, and critical
log checks in sections 11–14 and record the outcome/data-loss window. **STOP**
and keep writers stopped if restored integrity, FK, migration compatibility, or
readiness fails.

> Never simulate or fake a rollback by manually editing the `schema_migrations` table.

There is no tested reverse migration mechanism.

## 21. Release record template

```text
Release version:
Release SHA:
Previous known-good SHA/image/digest:
Operator:
UTC date/time:
Database path:
Backup path:
Backup SHA-256:
Restoration rehearsal result/evidence:
Tests/CI result/evidence:
Migration result (`0001`-`0011`):
Bootstrap and idempotent rerun result:
Integrity-check result:
Foreign-key-check result:
Health/readiness result:
Smoke/Scheduler result:
Image ID/digest and revision label:
Annotated tag:
GitHub Release URL:
Rollback status/decision/approver:
Notes (including assumptions and retained evidence):
```

## 22. References

* [Operator CLI command reference](../cli.md)
* [SQLite database migrations](../database_migrations.md)
* [Operational source policy](../operational_source_policy.md)
* [Architecture index](../architecture/README.md) and
  [v0.5.0 architectural review](../architecture/architectural_review_v0_5_0.md)
* [Database/schema migration guide](../database_migrations.md) (the executable
  schema is defined by `db/migrations/`)
* [v0.5.0 final release-readiness review](../architecture/release_readiness_v0_5_0.md)
* [v0.5.0 live/operational validation](../architecture/release_validation_v0_5_0_issue_78.md)
* [Scheduler registry operations](../scheduler_registry.md)
* [Repository setup and Docker/Compose operation](../../README.md)
* [`compose.example.yaml`](../../compose.example.yaml) and
  [`Dockerfile`](../../Dockerfile)
