# API key storage migration

API keys are stored as `sha256:`-prefixed SHA-256 digests of the full high-entropy key. The database also stores `key_prefix`, an eight-character prefix used only to help administrators identify keys. The recoverable full key is shown only in the creation or renewal response and is not available in list or detail views afterward.

## Configured database path

Database initialisation and API-key migration use the same configured path as the running application: `config.DB_PATH`. Set `DB_PATH` to the intended SQLite database location before running the migration. Absolute paths are used as provided; relative paths are resolved from the repository root, not from the process working directory.

Operators can verify the selected database path before migration with:

```bash
python - <<'PY'
import config
print(config.DB_PATH)
PY
```

When running the migration, the resolved database path is logged so operators can confirm the target:

```bash
python -m db.init_db
```

The configured database parent directory must already exist and must be a directory. Initialisation fails clearly if the parent is missing or invalid instead of silently creating a database under an unintended current working directory.

## Existing plaintext keys

Running the normal database initialisation path migrates existing `api_keys` rows in place at `config.DB_PATH`:

1. Add `key_hash` and `key_prefix` columns if they are missing.
2. For every row with a plaintext `api_key`, compute and store its hash and safe prefix while the plaintext is still available.
3. Preserve the row's `is_active` value.
4. Set `api_key` to `NULL` so the recoverable plaintext value is removed.

Existing API consumers do not need to take action during this migration. Their current key continues to authenticate because the stored hash is derived from that same key. Inactive keys remain inactive and continue to be rejected.

Operators should back up the SQLite database before upgrading, set `DB_PATH` to the deployed database (for example `/opt/docker/appdata/afl-api/data/afl_players.db`), verify the selected path with the command above, run `python -m db.init_db` once to apply the migration, and then verify that `api_keys.api_key` is empty for migrated rows.
