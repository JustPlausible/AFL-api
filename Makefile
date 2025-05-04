# Makefile

# === Setup ===
setup:
	pip install -r requirements.txt
	playwright install

# === Run CLI ===
scrape:
	python3 main.py --all --skip-existing

force-scrape:
	python3 main.py --all

club:
	python3 main.py --club=$(club)

# === Clean output files ===
clean:
	rm -f data/players-*.json
