# Public AFL metadata collection

The public JSON collector discovers and normalises the AFL metadata hierarchy
without writing it to the application database:

```text
competition -> competition season -> rounds -> teams -> matches
```

Run it through the existing CLI and select a season explicitly when required:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 --print-json
```

The Premiership defaults to the stable `AFL` competition code and `CD_C014`
provider ID. These can be changed with `--afl-competition-code` and
`--afl-competition-provider-id`. If no season is supplied, the collector uses
an unambiguous current flag or a season date range containing today's date; it
fails with guidance rather than choosing by numeric ordering.

Raw source responses are disabled by default. Opt in for investigation or a
dry run by providing a dedicated directory:

```bash
python cli.py --collect-afl-metadata --afl-season 2026 \
  --afl-raw-directory data/collection-dry-run
```

Captures are JSON files grouped by endpoint, with deterministic filenames that
include relevant scope IDs and the response page. They are separate from the
normalised result printed by `--print-json`; request headers and credentials are
never included.

The public response shapes can gain new fields. Normalised records therefore
include a `source` copy of each record in addition to the currently understood
identity, name, reference, time, score, bye and metadata fields. No mapping is
invented for undocumented fields.
