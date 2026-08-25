# AFL-api release runbook

This is a reusable repository release procedure for the supported topology:
one SQLite database shared by API, Admin, and exactly one Scheduler. Commands
using `compose.production.example.yaml` demonstrate repository behaviour;
operators must adapt service names, registry, storage, TLS, secrets, and
orchestration to their site.

Examples below use v0.7.0. Run them from the repository root unless stated
otherwise.

## 1. Fix the candidate identity

```bash
git fetch --all --prune
git switch <reviewed-release-branch>
git status --short --branch
test -z "$(git status --porcelain)"
export RELEASE_VERSION=0.7.0
export RELEASE_TAG=v${RELEASE_VERSION}
export RELEASE_SHA=$(git rev-parse HEAD)
git merge-base --is-ancestor 292a5ce "$RELEASE_SHA"
test "$(python cli.py --version)" = "$RELEASE_VERSION"
printf 'version=%s sha=%s\n' "$RELEASE_VERSION" "$RELEASE_SHA"
```

Record the full SHA, review result, test evidence, and release-note revision.
Confirm all intended work is merged; for v0.7.0, migration discovery must end
at `0024`:

```bash
python - <<'PY'
from db.migration_runner import discover_migrations
m = discover_migrations()
print(m[0].identifier, m[-1].identifier, len(m))
assert m[-1].identifier == "0024"
PY
```

## 2. Repository/release validation

These checks are portable release gates and do not require live AFL/CFS access:

```bash
python cli.py --version
pytest -q tests/test_version.py tests/test_migration_runner.py
pytest -q
git diff --check
```

Use local test clients to validate `/api/v1` discovery and Scheduler version
metadata as covered by the automated suite. Review
[`v0.7.0.md`](../releases/v0.7.0.md), refresh the separate readiness assessment
against `$RELEASE_SHA`, and complete the
[pre-tag checklist](v0.7.0-pre-tag-checklist.md).

## 3. Site-specific deployment preparation

Record the deployed database path and previous immutable image. The examples
assume the Compose example and named volume; an adapted deployment may instead
use a bind-mounted host path.

Drain or stop **all database writers** before backup and migration. For the
example stack, stop Admin and Scheduler (both can initiate writes) and stop API
traffic or the API service:

```bash
docker compose -f compose.production.example.yaml stop afl-scheduler afl-admin afl-api
```

With writers stopped, create a SQLite online-backup-API snapshot using the
release image or another controlled Python environment that can access both
paths. Replace the two paths with persistent, operator-controlled paths; do
not place the backup over the source database.

```bash
SOURCE_DB_PATH=/absolute/path/to/afl_players.db
BACKUP_PATH=/absolute/path/to/backups/afl_players-${RELEASE_SHA}.db
python - "$SOURCE_DB_PATH" "$BACKUP_PATH" <<'PY'
import sqlite3, sys
source, target = sys.argv[1:]
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as check:
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert check.execute("PRAGMA foreign_key_check").fetchall() == []
print(target)
PY
```

Use `SOURCE_DB_PATH` only as the host-side backup input. Do **not** export the
host path as `DB_PATH`: the example Compose file interpolates `DB_PATH` for its
containers, where the named-volume database remains `/app/data/afl_players.db`.

Retain the backup, its filesystem checksum, the previous immutable image
identifier, and the tested restore instructions. A backup stored only in the
same failure domain as the live database is insufficient.

## 4. Build one immutable image

Build only from the recorded clean candidate and label it with the full SHA.
All three services must use this exact image:

```bash
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test -z "$(git status --porcelain)"
export AFL_API_IMAGE=afl-api
export AFL_API_TAG="${RELEASE_VERSION}-${RELEASE_SHA}"
docker build --pull \
  --label "org.opencontainers.image.version=$RELEASE_VERSION" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  -t "${AFL_API_IMAGE}:${AFL_API_TAG}" .
docker image inspect "${AFL_API_IMAGE}:${AFL_API_TAG}" \
  --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Publish through the site's normal authenticated registry process if required;
record the immutable digest rather than relying on a mutable tag alone.

## 5. Migrate and validate SQLite

Configure the example Compose file to use the built tag and real persistent
volume. Keep writers stopped, then run the one-shot migration:

```bash
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python -m db.migrate
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python - <<'PY'
from db.connection import get_db_connection
from db.migration_runner import discover_migrations
with get_db_connection() as conn:
    applied = [r[0] for r in conn.execute("SELECT migration_id FROM schema_migrations ORDER BY migration_id")]
    assert applied[-1] == discover_migrations()[-1].identifier == "0024"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
print("migration head and SQLite checks: ok")
PY
```

The runner applies ordered migrations transactionally and rejects checksum or
history mismatches. Do not edit an applied migration to force an upgrade.

## 6. Site-specific season and credential checks

The selected season and live upstream access are deployment choices. Where the
site is intended to serve a current season, run the supported sequence and
review results rather than assuming a zero exit code alone proves completeness:

```bash
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --bootstrap-afl-season <YEAR>
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --sync-afl-season <YEAR>
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --report-afl-season <YEAR> --print-json > season-report.json
docker compose -f compose.production.example.yaml run --rm --no-build afl-api python cli.py --list-api-keys
```

Resolve completeness/reconciliation findings appropriate to the site's season.
Verify every required consumer key is active. Ordinary `/api/v1` reads require
authentication but do not currently enforce `standard-read`; grant
`advanced-read` only when that consumer is approved for advanced provenance.
Never put full keys into the release record.

## 7. Start and smoke-test

Start read services first, verify them, then start the sole Scheduler writer:

```bash
docker compose -f compose.production.example.yaml up -d --no-build afl-api afl-admin
docker compose -f compose.production.example.yaml ps
curl --fail --silent http://127.0.0.1:${AFL_API_PORT:-8000}/healthz
curl --fail --silent http://127.0.0.1:${AFL_API_PORT:-8000}/readyz
docker compose -f compose.production.example.yaml up -d --no-build afl-scheduler
docker compose -f compose.production.example.yaml ps
```

From an authorized management path, set a test key without writing it to shell
history, then smoke-test discovery and representative resources applicable to
the populated database:

```bash
read -rsp 'API key: ' AFL_API_KEY; echo
curl --fail --silent -H "X-Api-Key: $AFL_API_KEY" "http://127.0.0.1:${AFL_API_PORT:-8000}/api/v1"
curl --fail --silent -H "X-Api-Key: $AFL_API_KEY" "http://127.0.0.1:${AFL_API_PORT:-8000}/api/v1/seasons"
curl --fail --silent -H "X-Api-Key: $AFL_API_KEY" "http://127.0.0.1:${AFL_API_PORT:-8000}/api/v1/seasons/<SEASON_ID>/rounds"
curl --fail --silent -H "X-Api-Key: $AFL_API_KEY" "http://127.0.0.1:${AFL_API_PORT:-8000}/api/v1/matches/<MATCH_ID>/player-stats"
unset AFL_API_KEY
```

Also smoke-test the v1 player, injuries, commentary, interchange, and roster
resources for known site data. A legitimate `not_available` resource response
must be assessed against season completeness rather than changed into synthetic
data.

Verify all supported version surfaces:

```bash
docker run --rm "${AFL_API_IMAGE}:${AFL_API_TAG}" python cli.py --version
curl --fail --silent -H "X-Api-Key: <TEST_KEY>" "http://127.0.0.1:${AFL_API_PORT:-8000}/api/v1"
docker compose -f compose.production.example.yaml exec afl-scheduler python - <<'PY'
from version import __version__
assert __version__ == "0.7.0"
print(__version__)
PY
```

Inspect `/scheduler/health` from the private management network/Admin, Admin's
Season Review and analytics report, configured log sources, Scheduler logs,
diagnostic profile state (if deliberately enabled), and analytics reporting:

```bash
docker compose -f compose.production.example.yaml exec afl-scheduler python -m scripts.report_analytics --help
docker compose -f compose.production.example.yaml logs --since=15m afl-api afl-admin afl-scheduler
```

The first command verifies the checked-in reporting interface; run the desired
report arguments from its help for the site's review window.

## 8. Tag only after validation

Do not tag a merely built candidate. After every gate and the final readiness
refresh passes, confirm HEAD still matches the reviewed SHA:

```bash
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
git tag -a "$RELEASE_TAG" "$RELEASE_SHA" -m "AFL-api $RELEASE_VERSION"
git show --no-patch --decorate "$RELEASE_TAG"
git push origin "$RELEASE_TAG"
```

Creating the GitHub Release is a later maintainer action. Issue #220 must not
itself push `v0.7.0`.

## 9. Rollback

Retain the previous immutable image and verified pre-migration backup until the
release retention gate expires. If application-only rollback is proven safe,
stop writers and redeploy that exact image. This does not undo migrations or
newer writes.

For database rollback, stop API, Admin, and Scheduler, archive the failed
database plus WAL/SHM sidecars for investigation, restore the verified backup
to a clean target using the site's storage procedure, re-run integrity and
foreign-key checks, then start the previous image using the same read-services-
before-Scheduler sequence. Never copy an old main database file over a live WAL
database. Record the incident, restored backup checksum, database path, image
digest, and validation results.
