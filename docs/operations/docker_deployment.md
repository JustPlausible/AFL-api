# Docker deployment examples

## Supported boundary and choosing an example

This repository supports one carefully operated AFL-api deployment in which
API, Admin, and Scheduler share one SQLite database. It does not claim support
for horizontal scaling or multiple replicas.

[`compose.example.yaml`](../../compose.example.yaml) is for local development,
evaluation, and test/demo use. Its repository source bind mount and Uvicorn
`--reload` commands intentionally favour quick iteration.

[`compose.production.example.yaml`](../../compose.production.example.yaml) is a
small production-like, single-instance starting point. It has no source bind
mount or reload process, gives all three services the same explicitly tagged
image, and mounts only persistent runtime data and logs. It is an example, not
a complete hosting framework: review ports, volume storage, proxy/TLS, secrets,
backup, monitoring, and host controls for the operator's environment. The
`/opt/...` layout in the README is illustrative and is not a repository default.

## Build and image identity

The production-like file declares both `build:` on `afl-api` and one shared
`image:` anchor. A Compose build therefore builds the checkout once under the
selected name and tag; API, Admin, and Scheduler use that exact local image.
Alternatively, make that same name and tag available from an operator-controlled
registry and start with `--no-build` (and the deployment's normal authenticated
image-pull procedure).

Choose the placeholders below; they are operator-selected values, not values
discovered automatically from `version.py`:

```bash
git checkout <release-tag-or-exact-commit>
export AFL_API_IMAGE=afl-api
export AFL_API_TAG=<release-or-short-sha>
docker compose -f compose.production.example.yaml build --pull
docker compose -f compose.production.example.yaml up -d
```

The equivalent direct build is:

```bash
docker build --pull -t afl-api:<version-or-sha> .
```

`docker compose build --no-cache` controls reuse of build layers; it neither
identifies nor versions the resulting image. It is useful for diagnostics or an
intentionally clean rebuild, but is not routinely required for versioning. A
stable deployment should use an explicit tag and record both the Git release
tag or exact commit SHA and the resulting image name/tag. `latest` alone is not
an adequate release record.

A tag is a mutable label, not proof of reproducibility. Rebuilding the same tag
later from a changed checkout does not reproduce the original image.
Reproducibility depends on the recorded, reviewed source revision and controlled
dependency/base-image inputs, not merely the Docker tag.

## Persistent state and access

All three services mount the same named volumes read/write:

| Container path | Contents and users |
| --- | --- |
| `/app/data` | Persistent application data used by API, Admin, and Scheduler. `DB_PATH` defaults in this example to `/app/data/afl_players.db`, and all three services receive the same value. |
| `/app/logs` | Persistent file logs and operator/audit outputs where enabled, used by API, Admin, and Scheduler. |

Current repository-relative writers also place collection artifacts beneath
`data/`, including `players-*-raw.json`, normalized `players-*.json`,
`afl_stats_leaderboard.json`, `bbbffl_player_stats.csv`, and optional raw AFL
JSON captures when an operator selects a raw directory below `data/`. Keep any
operator-selected persistent collection output beneath `/app/data`; output
directed elsewhere is not covered by the example volume.

SQLite may create `afl_players.db-wal` and `afl_players.db-shm` beside the main
database while WAL mode is active. Persist the entire database directory. Do
not selectively copy the live main file: stop writers and follow the linked
backup procedure, which accounts for SQLite journal state. Playwright browser
binaries and Python packages are image contents installed by the Docker build;
they do not belong in a host source bind mount.

The `.env` value for `DB_PATH`, if set, must name a path beneath `/app/data` so
that it is shared and persisted. Relative values resolve under `/app` (for
example, `data/afl_players.db`). Ensure the volume has suitable ownership and
read/write permissions for every service.

## Network and secret boundary

- The API port is published for direct use or a reverse proxy.
- Admin is authenticated, but its operator-controlled published port should be
  limited by trusted LAN/VPN access, firewall policy, SSH forwarding, or a
  private reverse proxy. Authentication and an `internal` Docker network do not
  make arbitrary changes to a deployment safe.
- Scheduler has no host-published port. Admin reaches it over the internal
  `management` network; Scheduler also joins `scheduler-egress` for collection
  traffic. Do not route Scheduler through a public proxy.
- Store credentials and secrets in an uncommitted external `.env` or another
  operator-controlled secret mechanism. Copy `.env.example` as a starting
  schema, replace its demonstration values, and never commit `.env` contents.

For release gates, safe SQLite backup/restore, verification, and rollback, use
the active [reusable release runbook](release_runbook.md), especially its
backup and rollback procedures; do not substitute this overview for that
runbook.
