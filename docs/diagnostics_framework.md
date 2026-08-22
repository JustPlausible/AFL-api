# Diagnostic evidence-capture framework

## Purpose

Upstream AFL/CFS payload behaviour is sometimes uncertain -- a field's real
shape, an enum's real values, or how a status transitions around a specific
moment (quarter time, a lineup change, a new season's opening round) -- and
the only reliable way to resolve that uncertainty is to poll the live
endpoint and retain evidence for manual inspection. The diagnostics
framework exists to make that kind of investigation cheap and safe to run
repeatedly, without inventing new scheduler, persistence, and reporting
machinery every time.

It grew out of Issue #148's `match_clock` investigation into
`score.matchClock.periods` / `periodCompleted` / `periodSeconds` and
`match.status` / `score.status` behaviour around quarter/half/three-quarter/
full time, delivered and live-tested in PR #175. `match_clock` is the
framework's first checked-in **diagnostic profile**, and the reference
implementation for anyone adding a new one. This framework is a clean
reimplementation of the concept on top of the finalized PR #175
implementation -- `collection/match_state_evidence.py`,
`scheduler/match_state_capture.py`'s parsing/candidate-selection logic,
migration `0016`, and the real `score.matchClock.periods` parsing they
contain are unchanged.

Issue #193's `interchange` investigation is the framework's **second**
checked-in profile, added ahead of a live match to observe CFS
`matchInterchange/{matchProviderId}` behaviour: whether
`homeInterchange[]`/`awayInterchange[]` array membership actually represents
players currently off the ground, how quickly entries change on an
interchange, whether `interchangeCount` increments contemporaneously,
`timeOnGround`/`timeOnBench` update cadence, `benchReason` behaviour, and
what the payload does around quarter breaks and POSTGAME/CONCLUDED. It is
evidence capture only -- see `collection/match_interchange_evidence.py` and
`scheduler/match_interchange_capture.py` -- and demonstrates the framework's
whole point: adding it required one new profile module, one new small
migration, and one registration line, with **zero** changes to `match_clock`
or to any shared scheduler/APScheduler code.

## Diagnostics vs. production collectors

This is the one rule the whole framework exists to enforce:

**Diagnostic evidence must never silently become production source
authority.** A diagnostic profile:

* is explicitly opt-in (`AFL_DIAGNOSTICS_ENABLED=true` and the profile named
  in `AFL_DIAGNOSTIC_PROFILES`), off by default;
* polls a candidate list computed independently of production scheduling
  state, and never writes to production tables (`matches`, `players`, stat
  tables, etc.) -- only to its own diagnostic table(s);
* is never read by scheduler planning, lease/finality machinery, or any
  consumer-facing API response;
* uses endpoint definitions that are kept out of the maintained
  `afl_json.contracts.ENDPOINTS` registry until (and unless) a separate,
  deliberate decision promotes a verified field into a production
  collector. A diagnostic profile observing a field is evidence for that
  future decision, not the decision itself.

If a diagnostic investigation confirms a field is reliable enough to use in
production, that is a *new, separate* piece of work: add or extend a
verified `EndpointDefinition` in `afl_json.contracts`, wire it into a real
collector, and only then consider retiring the diagnostic profile that
produced the supporting evidence.

## Architecture

```
diagnostics/
    framework.py            generic: profile contract, registry, enablement,
                             APScheduler registration, restart-safe re-registration,
                             shutdown
    profiles/
        __init__.py          imports + registers every checked-in profile
        match_clock.py        Issue #148 profile (thin adapter)
        interchange.py         Issue #193 profile (thin adapter)

collection/match_state_evidence.py   match_clock: endpoint def, parsing, transition
                                      detection, persistence (unchanged from PR #175)
scheduler/match_state_capture.py     match_clock: candidate selection, settings,
                                      poll-cycle orchestration (unchanged from PR #175,
                                      except settings.enabled is now sourced from the
                                      framework's is_profile_selected())

collection/match_interchange_evidence.py   interchange: endpoint def, parsing,
                                            transition detection, persistence
scheduler/match_interchange_capture.py     interchange: candidate selection
                                            (reuses match_state_capture's generic
                                            _live_matches/_kickoff_tolerance_matches
                                            helpers -- see that module's docstring),
                                            settings, poll-cycle orchestration
```

The framework owns everything that is common to *any* diagnostic
investigation:

* **profile registration** -- `diagnostics.framework.register_profile`, called
  once per checked-in profile from `diagnostics/profiles/__init__.py`;
* **configuration/enablement** -- one global switch
  (`AFL_DIAGNOSTICS_ENABLED`) and one profile-selection list
  (`AFL_DIAGNOSTIC_PROFILES`), checked by `is_profile_selected()`;
* **APScheduler registration** -- one interval job per *enabled* profile
  (`diagnostic_<profile_name>`), never a shared master tick, registered
  through the same `add_registered_job` wrapper as every other scheduler
  job so it gets the same persisted registry row, status tracking, and
  logging as production jobs;
* **execution isolation** -- each profile is its own APScheduler job, so one
  profile's uncaught exception cannot prevent another profile's job from
  running; `register_diagnostic_profiles` also isolates *registration*
  failures per profile the same way;
* **restart-safe operation** -- `register_diagnostic_profiles(scheduler)` is
  called unconditionally on every scheduler startup (`scheduler/start.py`),
  the same way the other interval jobs are; a profile's *evidence* is
  recovered from its own durable table, not from in-memory scheduler state
  (see match_clock's `poll_sequence` continuity across restarts);
* **shutdown** -- `shutdown_diagnostic_profiles()` calls each profile's
  `shutdown()` (default a no-op) so pooled resources like an HTTP client
  close cleanly.

A profile owns everything that is specific to its investigation:

* the endpoint(s) it polls and their (typically unverified)
  `EndpointDefinition`;
* candidate/entity selection (which matches/entities to poll, and when);
* payload parsing and transition/change detection;
* its default and configurable polling interval;
* its persistence and raw-response retention policy;
* how its own status/report should be interpreted.

## The profile contract

`diagnostics.framework.DiagnosticProfile` is deliberately small:

```python
class DiagnosticProfile(ABC):
    name: str

    @abstractmethod
    def interval_seconds(self) -> int: ...

    @abstractmethod
    def run(self, *, now: datetime) -> list[dict[str, Any]]: ...

    def status(self) -> dict[str, Any]: ...   # optional override
    def shutdown(self) -> None: ...            # optional override
```

`run()` must not raise for one candidate's failure -- catch and log
per-candidate the same way `match_clock`'s `capture_live_match_state` does,
so one bad match/entity never aborts the rest of that profile's poll. The
framework separately guarantees that one *profile* failing never affects
another, because each is its own scheduler job.

This is intentionally not a generic plugin system: profiles are checked-in
Python classes imported explicitly from `diagnostics/profiles/__init__.py`,
never discovered from configuration, a directory scan, or a URL.

## Configuration

```env
AFL_DIAGNOSTICS_ENABLED=true
AFL_DIAGNOSTIC_PROFILES=match_clock,interchange
```

`AFL_DIAGNOSTIC_PROFILES` is a comma-separated list of *already checked-in*
profile names -- it selects among code that exists in the repository, it is
never a mechanism for supplying arbitrary URLs, JSON paths, or code through
`.env`. Enabling a profile requires both the global switch and the profile
being named; either alone is a no-op. Each named profile is registered as its
own independent APScheduler job (see Architecture above), so `match_clock`
and `interchange` can be enabled together, individually, or not at all, in
any combination, and one being disabled never affects the other.

Profile-specific *safe* settings (an interval, a tolerance window) remain
individually configurable, following the profile's own name:

```env
AFL_DIAGNOSTIC_MATCH_CLOCK_INTERVAL_SECONDS=15
AFL_DIAGNOSTIC_MATCH_CLOCK_KICKOFF_TOLERANCE_SECONDS=600
AFL_DIAGNOSTIC_MATCH_CLOCK_POST_LIVE_GRACE_SECONDS=600

AFL_DIAGNOSTIC_INTERCHANGE_INTERVAL_SECONDS=15
AFL_DIAGNOSTIC_INTERCHANGE_KICKOFF_TOLERANCE_SECONDS=600
AFL_DIAGNOSTIC_INTERCHANGE_POST_LIVE_GRACE_SECONDS=600
```

### Backward compatibility with PR #175's configuration names

PR #175 shipped and live-tested the Issue #148 investigation under a
single-purpose set of names, before this framework existed:

```env
AFL_CAPTURE_MATCH_STATE_EVIDENCE=false
AFL_MATCH_STATE_CAPTURE_INTERVAL_SECONDS=15
AFL_MATCH_STATE_CAPTURE_KICKOFF_TOLERANCE_SECONDS=600
AFL_MATCH_STATE_CAPTURE_POST_LIVE_GRACE_SECONDS=600
```

Those names are **deprecated but still honoured** so that a deployment
already running with them does not unexpectedly start polling differently,
or fail to start, purely because this framework was introduced:

* If `AFL_DIAGNOSTICS_ENABLED` is not explicitly set, its value falls back to
  `AFL_CAPTURE_MATCH_STATE_EVIDENCE` (default `false` either way).
* If `AFL_DIAGNOSTIC_PROFILES` is not explicitly set (or set empty) and
  `AFL_CAPTURE_MATCH_STATE_EVIDENCE=true`, `AFL_DIAGNOSTIC_PROFILES` defaults
  to `match_clock` -- reproducing PR #175's single-profile behaviour exactly.
* Each `AFL_DIAGNOSTIC_MATCH_CLOCK_*` setting falls back to its
  `AFL_MATCH_STATE_CAPTURE_*` predecessor when the new name is not set.
* Whenever a new (`AFL_DIAGNOSTIC_*`) name is explicitly set, it always wins
  over the corresponding legacy name -- so an operator can opt back out
  (`AFL_DIAGNOSTICS_ENABLED=false`) even with a legacy variable still present
  in the environment.

New deployments and any future documentation should use the
`AFL_DIAGNOSTIC_*` names directly. The legacy names are candidates for
removal once no deployment still relies on them; see `config.py` for the
resolution helpers (`_bool_env_with_legacy_fallback`,
`_int_env_with_legacy_fallback`, `_diagnostic_profiles_with_legacy_fallback`).

Diagnostics remain **disabled by default** under both the new and legacy
names -- nothing here changes what a fresh deployment does out of the box.

## Operator endpoints

* `GET /scheduler/diagnostics` -- generic, framework-level status: whether
  diagnostics are globally enabled, and each checked-in profile's
  selected/enabled state and interval.
* `GET /scheduler/match-state-evidence` -- `match_clock`-specific evidence
  and settings (unchanged from PR #175); see also
  `scripts/report_match_state_evidence.py`.
* `GET /scheduler/match-interchange-evidence` -- `interchange`-specific
  evidence and settings; see also `scripts/report_interchange_evidence.py`,
  which suppresses noisy per-poll `timeOnGround`/`timeOnBench` transitions
  by default (`--verbose` to see them). Each profile added this way -- its
  own endpoint and/or report script -- the same pattern `match_clock`
  established.

## Persistence: why `match_state_evidence_observations` stays as-is

This version of the framework deliberately **keeps `match_clock`'s existing
`match_state_evidence_observations` table** rather than introducing a
generic `diagnostic_capture_sessions` / `diagnostic_observations` /
`diagnostic_events` schema. Reasoning:

* There is exactly one profile today. A generic schema's payoff is
  amortising persistence work across *multiple* profiles; with one profile,
  a generic schema is pure abstraction cost with no concrete benefit yet.
* PR #175 is the stable, live-tested implementation for Issue #148, and its
  table already holds real evidence gathered during that testing. Migrating
  it now would put that evidence and an active investigation at risk for no
  behavioural gain.
* `match_clock`'s table shape (typed columns for `periods_json`,
  `latest_period_number`, `latest_period_seconds`, `latest_period_completed`,
  `transition_flags_json`, selective `raw_match_item_json`) is specific
  enough to its own investigation that a one-size-fits-all generic
  `observations` table would likely need a similar amount of profile-specific
  structure anyway (a JSON blob per profile, or per-profile columns), without
  clearly saving implementation effort.

No migration/backfill was needed for this change: migration `0016` and the
`match_state_evidence_observations` table are untouched. All previously
captured Issue #148 evidence remains queryable exactly as before, through
`collection.match_state_evidence.evidence_rows` and
`scripts/report_match_state_evidence.py`, unchanged.

`interchange` (Issue #193) followed the same pattern rather than reopening
this decision: migration `0017` adds its own
`match_interchange_evidence_observations` table, shaped for its own evidence
(per-side player arrays, per-side count totals, a locally-observed
`match_status_at_poll` snapshot in place of match_clock's CFS-reported
`match_status`/`score_status`, since the matchInterchange payload does not
appear to carry an equivalent field) rather than being forced into
match_clock's column shape or a premature generic one.

**When to revisit this:** with two profiles now shipped, still keep watching
for a third before generalising. A reasonable trigger for a shared schema is
three or more profiles with materially the same shape (poll sequence,
observed-at, per-poll fields, transition flags, selective raw retention) --
at that point extracting a shared `diagnostic_observations`-style table (with
each profile owning its own typed "extra fields" column, or a
profile-specific companion table for anything that needs real columns)
becomes a genuine simplification rather than premature architecture. Any such
migration should be additive (new table, backfill by copying/mapping
existing rows, dual-read period, then retire the old table) so an
in-progress investigation is never interrupted.

## Adding a new diagnostic profile

`interchange` (Issue #193) is a second real, checked-in example of this
workflow alongside `match_clock` -- see `diagnostics/profiles/interchange.py`,
`collection/match_interchange_evidence.py`, and
`scheduler/match_interchange_capture.py`. Using a hypothetical
`match_roster_contract` investigation as a further illustration:

1. **Write the investigation-specific logic**, following `match_clock`'s
   split: pure parsing/transition-detection + persistence in one module
   (e.g. `collection/match_roster_contract_evidence.py`), candidate selection
   and poll-cycle orchestration in another (e.g.
   `scheduler/match_roster_contract_capture.py`). Define its endpoint as an
   unverified, diagnostic-local `EndpointDefinition` -- do not add it to
   `afl_json.contracts.ENDPOINTS`.
2. **Add persistence** -- either its own small migration + table (following
   `0016_match_state_evidence.py`'s pattern) if it needs typed, queryable
   columns, or reuse existing tooling if genuinely applicable. Decide raw
   retention policy deliberately (e.g. only on first observation and on
   detected changes, as `match_clock` does).
3. **Add config** for its safe settings, e.g.
   `AFL_DIAGNOSTIC_MATCH_ROSTER_CONTRACT_INTERVAL_SECONDS`.
4. **Implement the profile adapter** in
   `diagnostics/profiles/match_roster_contract.py`:

   ```python
   class MatchRosterContractProfile(DiagnosticProfile):
       name = "match_roster_contract"

       def interval_seconds(self) -> int:
           return config.AFL_DIAGNOSTIC_MATCH_ROSTER_CONTRACT_INTERVAL_SECONDS

       def run(self, *, now: datetime) -> list[dict[str, Any]]:
           return capture_match_roster_contract(clock=lambda: now)
   ```

5. **Register it**: add one import + `register_profile(...)` line to
   `diagnostics/profiles/__init__.py`. No scheduler, persistence-lifecycle,
   or restart code is needed -- the framework already provides all of that.
6. **Enable it for a round**: set `AFL_DIAGNOSTICS_ENABLED=true` and
   `AFL_DIAGNOSTIC_PROFILES=match_roster_contract` (or
   `match_clock,match_roster_contract` to run both), restart the scheduler,
   and disable it again afterwards by removing it from
   `AFL_DIAGNOSTIC_PROFILES` -- production collection behaviour never
   changes based on this switch.
7. **Add tests** mirroring `tests/test_match_state_evidence.py` and
   `tests/test_match_state_capture.py`, plus registering the new profile in
   any framework-level compatibility tests.

This is the whole workflow a 2027-season contract check (`cfs_contract_2027`
or similar) would follow: add a checked-in profile, enable it for the
opening round, capture selected endpoint responses, compare observed
structure against known expectations by hand (or with a future schema-diff
tool -- not built here), and disable it again once the investigation is
done.
