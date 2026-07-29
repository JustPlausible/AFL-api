--Match player statistics

outerHTML: match-player-stats-completed-8216.html

Source type: rendered HTML
Capture date: 2026-07-28
Page state: completed
AFL match ID: 8216
Provider match ID: CD_M20260142001
Original URL: https://www.afl.com.au/afl/matches/8216#player-stats
Sanitisation: none / removed scripts and unrelated content

Network requests:

*playerStats*
request URL: https://api.afl.com.au/cfs/afl/playerStats/match/CD_M20260142001
Domain: Match player statistics
Source: Authenticated CFS JSON
Endpoint: GET /cfs/afl/playerStats/match/{matchProviderId}
Example provider ID: CD_M20260142001
Observed status: HTTP 200
Response type: application/json
Authentication: x-media-mis-token header required by browser request
Browser execution required: No for the endpoint itself, provided a valid token can be acquired
Observed origin: https://www.afl.com.au
Cache behaviour: max-age=3 with ETag support
Capture date: 2026-07-28
Token value: REDACTED
live browser response matches the existing /cfs/afl/playerStats/match/{matchProviderId}

*cfs*
request URL: https://api.afl.com.au/cfs/afl/matchRoster/full/CD_M20260142001
Domain: Presume some level of match player rosters, linked to next GET request
Source: Uncertain, direct browser response =
{"code":"CFSAPI001","date":"2026-07-28T15:22:07.916+0000","host":"mis-matchroster-6fc97bd668-xh9x6","mdc":"@D85g@","path":"/afl/matchRoster/full/CD_M20260142001","status":401,"techMessage":"Access to this site is forbidden","userMessage":"Access to this site is forbidden","version":"2.0.138"}
Endpoint: OPTIONS /cfs/afl/matchRoster/full/{matchProviderId}
Observed status: HTTP 204 No Content

request URL: https://api.afl.com.au/cfs/afl/matchRoster/full/CD_M20260142001
Domain: Match player roster (home and away teams), match status, umpires, weather, venue, recent team match results
Source: Authenticated CFS JSON
Endpoint: GET /cfs/afl/matchRoster/full/{matchProviderId}
Observed status: HTTP 200 OK
Response type: application/json
Authentication: x-media-mis-token header required by browser request
Browser execution required: No for the endpoint itself, provided a valid token can be acquired
Observed origin: https://www.afl.com.au
Cache behaviour: max-age=3 with ETag support
Capture date: 2026-07-28
Token value: REDACTED

request URL: https://api.afl.com.au/cfs/afl/matchRosters/round/CD_R202601420?minimal=true
Domain: Match player rosters, includes
Source: Authenticated CFS JSON
Endpoint: GET /cfs/afl/matchRosters/round/{matchProviderId}?minimal=true

request URL: https://api.afl.com.au/cfs/afl/matchItem/CD_M20260142001
Domain: Match information including match name, round details, score (broken down into home and away, period scoring, match clock (time for each period), scoreworm (including match time, player scoring event)), weather and venue
Source: Authenticated CFS JSON
Endpoint: GET /cfs/afl/matchItem/{matchProviderId}

request URL: https://api.afl.com.au/cfs/afl/matchInterchange/CD_M20260142001
Domain: Match player interchange history
Source: Authenticated CFS JSON
Endpoint: GET /cfs/afl/matchInterchange/{matchProviderId}
NOTE: CONCLUDED match seems to only show final five players from each team remaining on the interchange bench. Live match may show further details during the game including reasons for players taken from the field.

request URL: https://api.afl.com.au/cfs/commentaryFeed/CD_M20260142001
Domain: Match commentary with links to player events via playerId and teamId
Source: Authenticated CFS JSON
Endpoint: GET /cfs/commentaryFeed/{matchProviderId}
Observed status: HTTP 200 OK
NOTE: Might be a fun endpoint to implement if ever looking to provide live, translated commentary in fantasy leagues!
