## matchItem endpoint

`GET /cfs/afl/matchItem/{matchProviderId}` returns a composite payload used
across multiple match-centre components. It does not correspond to one
specific rendered HTML section.

Observed content includes:

- match and team metadata;
- venue information;
- match roster information;
- ins, outs, emergencies and milestones;
- umpires and weather;
- recent meetings;
- scores by period;
- match clock data;
- scoring events and score-worm data.

Some fields may be fetched but not rendered in the currently visible page
or tab. Mapping should therefore be based on JSON structure and browser
network usage rather than assuming a one-to-one HTML equivalent.