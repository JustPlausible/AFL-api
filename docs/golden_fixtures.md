# AFL golden source fixtures

The maintained corpus lives in `tests/fixtures/afl_sources/`. Existing useful captures under
`tests/fixtures/afl/` and `tests/fixtures/afl_json/` remain in place and are referenced from
`afl_sources/manifest.json`; do not duplicate them merely to impose a new hierarchy.

## Layout and names

`html_rendered/` contains DOM captured after browser execution and `expected/` is reserved for
focused canonical outputs too large for readable assertions. The manifest may also identify
`html_raw/`, `page_state/`, `api_json/`, or `cfs_json/` sources. Names use
`<domain>_<strategy>_<season-or-state>.<ext>` and never an opaque capture identifier alone.

Every manifest record supplies a stable ID, domain, parser contract, source type, capture date
(via the manifest default or an override), generalised URL pattern, represented state, expected
record count, important fields, sanitisation, purpose, and limitations. `related_paths` groups
state variants without copying expected payloads.

## Source distinctions

* **Raw HTTP HTML** is the unexecuted response body.
* **Playwright-rendered HTML** is the post-JavaScript DOM; no browser is used when replaying it.
* **Embedded page state** is JSON extracted from an HTML hydration/script container and remains a
  separate source contract.
* **Public AFL API JSON** comes from unauthenticated `aflapi.afl.com.au/afl/v2` endpoints.
* **Authenticated CFS JSON** comes from `api.afl.com.au/cfs/afl`; fixtures contain payload data
  only, never authentication headers or token acquisition responses.

## Add or refresh a fixture

1. Identify the production parser boundary and source state before capturing anything.
2. Remove cookies, authentication tokens, `WMCTok`, request signatures, irrelevant personal
   data, environment paths, analytics and unrelated page content.
3. Reduce the sample to the smallest extract that preserves selector ancestry, sibling order,
   structured containers and fields the real parser consumes. Prefer a supplied realistic sample
   over invented markup; use synthetic data only for a deliberate edge/failure case.
4. Add or update its manifest record, focused expected fields, parser test, valid empty/partial
   state where relevant, and a negative mutation that removes a required contract element.
5. Update expected output intentionally only after explaining the upstream contract difference.
   Never overwrite expectations simply to make a refresh pass.

Negative cases should mutate a valid golden fixture in memory so the valid and broken contracts
cannot drift independently. Assert the diagnostic category or meaningful message fragment rather
than an entire exception string. A fixture refresh is a **source-contract change** and must be
reviewed as such before new output is accepted.

Run the corpus alone with `pytest tests/test_afl_golden_fixtures.py`, then run `pytest`. Corpus tests
block sockets/common HTTP entry points and real sleeping, and require neither AFL credentials nor
a live browser.
