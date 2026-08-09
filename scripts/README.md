# 🛠 Script Utilities

These CLI tools support managing access and maintaining your AFL API system.

---

## 🔐 API Key Manager

API keys are created, listed, and deleted through the operator CLI in
[`cli.py`](../cli.py); see [`docs/cli.md`](../docs/cli.md#api-key-management)
for the authoritative reference. `scripts/manage_api_keys.py` holds the
persistence logic that those flags call into — it is a library module, not a
standalone script, and is not meant to be executed directly.

Keys are stored as hashes in the SQLite DB. Full keys are shown only once
when created; list output shows only safe prefixes.

### Usage

Run from the repository root (or the equivalent working directory inside the
container). No `PYTHONPATH` configuration is required:

```bash
python cli.py --add-api-key LABEL
python cli.py --list-api-keys
python cli.py --remove-api-key KEY_OR_LABEL
```

| Option | Description |
| --- | --- |
| `--list-api-keys` | List all key labels, prefixes, and active status |
| `--add-api-key LABEL` | Add a new key and show it once |
| `--remove-api-key KEY_OR_LABEL` | Remove a key by presented key or label |

### Example

```bash
# Add a new API key
python cli.py --add-api-key "my-label"

# List current keys
python cli.py --list-api-keys
```

## 🧱 Database Scripts

- [`db/init_db.py`](../db/init_db.py) (run as `python -m db.init_db`) – initializes the SQLite database and tables.
- [`db/import_to_db.py`](../db/import_to_db.py) – loads enriched JSON player files into the DB.

## 💡 Notes

API-key management uses the configured `DB_PATH` value from `config.py`, matching the running application database.
