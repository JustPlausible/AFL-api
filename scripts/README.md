# 🛠 Script Utilities

These CLI tools support managing access and maintaining your AFL API system.

---

## 🔐 API Key Manager — `manage_api_keys.py`

Create, list, and delete API keys stored in the SQLite DB.

### Usage

```bash
PYTHONPATH=/app python3 scripts/manage_api_keys.py [options]

Options
Option	Description
--list	List all keys (masked by default)
--list --show	List full keys (unsafe)
--add	Add a new key
--remove	Remove a key by label or key

Example
# Add a new API key
PYTHONPATH=/app python3 scripts/manage_api_keys.py --add

# List current keys
PYTHONPATH=/app python3 scripts/manage_api_keys.py --list

🧱 Database Scripts
init_db.py – Initializes the SQLite database and tables.

import_to_db.py – Loads enriched JSON player files into the DB.

💡 Notes
All scripts assume data/afl_players.db as the active SQLite DB.

Scripts are meant to be run inside your container using PYTHONPATH=/app.