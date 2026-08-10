# API implementation designs

This directory contains endpoint- and interface-specific implementation
designs. These documents translate the human-led
[consumer API workflow](../workflows/consumer_api_design.md) into bounded
technical changes; they remain subordinate to that broader architecture and do
not by themselves describe shipped behaviour.

## Available designs

- [Canonical CFS player-stat read API](player_stats_api_design.md) records the
  implementation design for the first `/api/v1` player-stat resource. Stage 1
  is shipped; see the [consumer reference](../../api_v1_player_stats.md) for
  current behaviour.
