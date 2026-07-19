# API key storage migration

API keys are stored as `sha256:`-prefixed SHA-256 digests of the full high-entropy key. The database also stores `key_prefix`, an eight-character prefix used only to help administrators identify keys. The recoverable full key is shown only in the creation or renewal response and is not available in list or detail views afterward.

## Existing plaintext keys

Running the normal database initialisation path migrates existing `api_keys` rows in place:

1. Add `key_hash` and `key_prefix` columns if they are missing.
2. For every row with a plaintext `api_key`, compute and store its hash and safe prefix while the plaintext is still available.
3. Preserve the row's `is_active` value.
4. Set `api_key` to `NULL` so the recoverable plaintext value is removed.

Existing API consumers do not need to take action during this migration. Their current key continues to authenticate because the stored hash is derived from that same key. Inactive keys remain inactive and continue to be rejected.

Operators should back up the SQLite database before upgrading, run the application or `python db/init_db.py` once to apply the migration, and then verify that `api_keys.api_key` is empty for migrated rows.
