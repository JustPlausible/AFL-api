# 🛠 Script Utilities

These CLI tools support managing access and maintaining your AFL API system.

---

## 🔐 API Key Manager — `manage_api_keys.py`

Create, list, and delete API keys stored as hashes in the SQLite DB. Full keys are shown only once when created; list output shows only safe prefixes.

### Usage

```bash
PYTHONPATH=/app python3 scripts/manage_api_keys.py [options]

Options
Option	Description
--list	List all key prefixes
--add LABEL	Add a new key and show it once
--remove KEY_OR_LABEL	Remove a key by presented key or label

Example
# Add a new API key
PYTHONPATH=/app python3 scripts/manage_api_keys.py --add "my-label"

# List current keys
PYTHONPATH=/app python3 scripts/manage_api_keys.py --list

🧱 Database Scripts
init_db.py – Initializes the SQLite database and tables.

import_to_db.py – Loads enriched JSON player files into the DB.

💡 Notes
All scripts assume data/afl_players.db as the active SQLite DB.

Scripts are meant to be run inside your container using PYTHONPATH=/app.