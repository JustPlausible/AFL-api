"""Regression tests for CFS URL construction (Issue #199).

Locks in the exact resolved URLs for every maintained CFS endpoint family as
the compatibility baseline for the CFS_SERVICE_ROOT / per-endpoint-family-path
refactor: CFS_SERVICE_ROOT ("https://api.afl.com.au/cfs") is the shared
service root, and each endpoint definition models its own family path
(e.g. "/afl/players", "/commentaryFeed/{...}") rather than the root itself
encoding the "afl" family. Every URL asserted below is unchanged from before
the refactor -- these are compatibility guards, not new endpoint contracts.

Deliberately resolves URLs through EndpointDefinition.url_template, the same
abstraction afl_json.client.AflJsonClient and every collector use, rather
than asserting only the CFS_SERVICE_ROOT constant in isolation.
"""
from __future__ import annotations

import pytest

from afl_json.contracts import CFS_SERVICE_ROOT, get_endpoint
from afl_json.match_commentary import MATCH_COMMENTARY_ENDPOINT as PRODUCTION_COMMENTARY_ENDPOINT
from afl_json.match_interchange import MATCH_INTERCHANGE_ENDPOINT as PRODUCTION_INTERCHANGE_ENDPOINT
from collection.match_commentary_evidence import MATCH_COMMENTARY_ENDPOINT as DIAGNOSTIC_COMMENTARY_ENDPOINT
from collection.match_interchange_evidence import MATCH_INTERCHANGE_ENDPOINT as DIAGNOSTIC_INTERCHANGE_ENDPOINT
from collection.match_state_evidence import MATCH_ITEM_ENDPOINT


def test_cfs_service_root_has_no_afl_family_or_trailing_slash():
    """The shared root must never itself encode the "afl" endpoint family or
    a trailing slash -- either would risk a malformed "//" or a family
    endpoint silently losing its own "/afl" segment."""
    assert CFS_SERVICE_ROOT == "https://api.afl.com.au/cfs"
    assert not CFS_SERVICE_ROOT.endswith("/afl")
    assert not CFS_SERVICE_ROOT.endswith("/")


# --- "/cfs/afl/..." endpoint family ------------------------------------------

def test_wmc_token_resolves_under_the_afl_family():
    definition = get_endpoint("wmc_token")
    assert definition.url_template == "https://api.afl.com.au/cfs/afl/WMCTok"


def test_season_players_resolves_under_the_afl_family():
    """A normal "/cfs/afl/..." endpoint."""
    definition = get_endpoint("season_players")
    assert definition.url_template == "https://api.afl.com.au/cfs/afl/players"


def test_match_rosters_resolves_under_the_afl_family():
    definition = get_endpoint("match_rosters")
    resolved = definition.url_template.format(round_provider_id="CD_R202601420")
    assert resolved == "https://api.afl.com.au/cfs/afl/matchRosters/round/CD_R202601420"


def test_match_player_statistics_resolves_under_the_afl_family():
    """Production player/stat endpoint whose construction previously relied
    on CFS_API_BASE already including "/afl"."""
    definition = get_endpoint("match_player_statistics")
    resolved = definition.url_template.format(match_provider_id="CD_M20260142001")
    assert resolved == "https://api.afl.com.au/cfs/afl/playerStats/match/CD_M20260142001"


def test_match_item_diagnostic_resolves_under_the_afl_family():
    resolved = MATCH_ITEM_ENDPOINT.url_template.format(match_provider_id="CD_M20260142001")
    assert resolved == "https://api.afl.com.au/cfs/afl/matchItem/CD_M20260142001"


@pytest.mark.parametrize("endpoint", [PRODUCTION_INTERCHANGE_ENDPOINT, DIAGNOSTIC_INTERCHANGE_ENDPOINT])
def test_match_interchange_resolves_under_the_afl_family(endpoint):
    resolved = endpoint.url_template.format(match_provider_id="CD_M20260142001")
    assert resolved == "https://api.afl.com.au/cfs/afl/matchInterchange/CD_M20260142001"


# --- commentaryFeed: lives directly under the CFS root, not "/afl" ----------

@pytest.mark.parametrize("endpoint", [PRODUCTION_COMMENTARY_ENDPOINT, DIAGNOSTIC_COMMENTARY_ENDPOINT])
def test_commentary_feed_resolves_directly_under_the_cfs_root(endpoint):
    resolved = endpoint.url_template.format(match_provider_id="CD_M20260142001")
    assert resolved == "https://api.afl.com.au/cfs/commentaryFeed/CD_M20260142001"
    assert "/afl/" not in resolved


# --- Malformed-URL protection -------------------------------------------------

@pytest.mark.parametrize("endpoint", [
    get_endpoint("wmc_token"), get_endpoint("season_players"), get_endpoint("match_rosters"),
    get_endpoint("match_player_statistics"), MATCH_ITEM_ENDPOINT,
    PRODUCTION_INTERCHANGE_ENDPOINT, DIAGNOSTIC_INTERCHANGE_ENDPOINT,
])
def test_afl_family_endpoints_never_duplicate_the_afl_segment(endpoint):
    """Guards against a malformed "/cfs/afl/afl/..." path: each "/afl" family
    endpoint must own exactly one "/afl/" segment, contributed by its own
    path_template rather than implied twice by base_url and path_template
    both."""
    assert endpoint.path_template.startswith("/afl/")
    assert endpoint.url_template.count("/afl/") == 1


@pytest.mark.parametrize("endpoint", [PRODUCTION_COMMENTARY_ENDPOINT, DIAGNOSTIC_COMMENTARY_ENDPOINT])
def test_commentary_feed_is_never_moved_under_the_afl_family(endpoint):
    """commentaryFeed must never be modelled under "/afl" -- that was the
    pre-refactor bug this project shipped and fixed (Issue #196/#201)."""
    assert not endpoint.path_template.startswith("/afl")
    assert "/afl/" not in endpoint.url_template
