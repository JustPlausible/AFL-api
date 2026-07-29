#!/usr/bin/env bash
set -euo pipefail

TOKEN=$(
  curl -sS --compressed --fail-with-body \
    -X POST \
    --data '' \
    -H 'Accept: */*' \
    -H 'Origin: https://www.afl.com.au' \
    -H 'Referer: https://www.afl.com.au/' \
    -H 'Cache-Control: no-cache' \
    -H 'Pragma: no-cache' \
    -H 'User-Agent: Mozilla/5.0' \
    'https://api.afl.com.au/cfs/afl/WMCTok' |
  jq -er '.token'
)

capture() {
  local url="$1"
  local output="$2"

  curl -sS --compressed --fail-with-body \
    -H 'Accept: application/json' \
    -H "x-media-mis-token: $TOKEN" \
    -H 'Origin: https://www.afl.com.au' \
    -H 'Referer: https://www.afl.com.au/' \
    "$url" |
  jq '.' > "$output"

  jq empty "$output"
  echo "Saved: $output"
}

capture \
  'https://api.afl.com.au/cfs/afl/matchItem/CD_M20260142001' \
  'tests/fixtures/afl/match_item/match_item_8216_concluded.json'
