# Reviewed match data exceptions

AFL-api deliberately separates three facts: provider/canonical lifecycle,
machine-detected missing-data evidence, and a human explanation. A reviewed
exception never rewrites `matches.status`, scores, raw/source JSON, or CFS
responses, and never creates player-stat rows.

## Operator workflow

1. Synchronise/report normally. An unreviewed concluded match with no
   authoritative rows remains actionable as
   `match.final_without_authoritative_stats` and season sync remains partial.
2. List candidates and their database evidence:

   ```bash
   python cli.py --report-stats-absence-candidates 2015
   ```

   Candidate detection requires a concluded match and zero authority-2 CFS
   player-stat rows. It reports useful corroboration (score, successful empty
   collection attempts, and whether provider source evidence is present), but
   **does not assign a reason**. Empty CFS statistics alone do not excuse data.
3. Inspect the retained provider/Bruno evidence, comparing the suspect response
   with an ordinary concluded match. Research an independent explanation when
   needed.
4. Record the reviewed disposition. The known canonical match 847 review is
   reproducible with:

   ```bash
   python cli.py --review-stats-not-expected 847 \
     --reason-code abandoned \
     --display-reason "Match abandoned and not played." \
     --evidence-url "https://www.afl.com.au/news/197577/crows-clash-with-geelong-abandoned-remainder-of-round-14-to-go-ahead" \
     --reviewed-by OPERATOR
   ```

   Repeating identical input is idempotent. Changed input updates the active
   record. Creates, changes, and revocations are retained in
   `match_data_exception_audit` with actor and timestamp.
5. Run `python cli.py --report-afl-season 2015` and
   `python cli.py --sync-afl-season 2015`. The report separately counts
   authoritative-stat matches and `stats_not_expected` matches, and emits the
   informational `match.stats_not_expected` finding. Sync reports the same
   disposition and stops retrying it as an empty collection.
6. Correct or revoke a review with:

   ```bash
   python cli.py --revoke-stats-not-expected 847 --reviewed-by OPERATOR
   ```

   If statistics remain absent, the match immediately becomes actionable and
   incomplete again.

Allowed reasons are `abandoned`, `cancelled`, `forfeit`, `not_played`,
`historical_data_unavailable`, `provider_data_unavailable`, and `other`.
These values describe a human-reviewed disposition; detection remains generic.
