"""
Projected ownership estimation.

IMPORTANT — this is a heuristic, not real data. Actual DFS ownership
percentages come from the crowd (what % of entered lineups rostered a
player), which requires either a paid ownership-projection service or
scraping post-lock ownership reports — neither fits this project's
free-data-only design. Instead, ownership is estimated from the two
signals we do have: a player's rank by raw projection (name-brand
"obvious plays" get rostered a lot) and by value score (great
cost-efficiency becomes chalk once the pool notices it) within their
position. This produces a plausible-shaped distribution (a few high-
owned plays, a long tail of low-owned ones) but should be treated as
directional, not predictive — it has no access to real public
sentiment, beat-writer hype, or last-minute injury news that actually
drive real ownership.
"""

from __future__ import annotations

from collections import defaultdict

from nfl_dfs.models import PlayerValue, Position

MIN_OWNERSHIP_PCT = 0.5
MAX_OWNERSHIP_PCT = 40.0

# how much projection rank vs. value rank each contribute to the
# popularity score that ownership is derived from
PROJECTION_RANK_WEIGHT = 0.6
VALUE_RANK_WEIGHT = 0.4

# exponent > 1 concentrates ownership among the top-ranked players and
# tails off quickly for the rest, matching the real shape of DFS
# ownership (a handful of high-owned chalk plays, a long thin tail)
CONCENTRATION_EXPONENT = 2.5


class OwnershipEstimator:
    def assign(self, player_values: list[PlayerValue]) -> None:
        """Mutates each PlayerValue's projected_ownership_pct in place."""
        by_position: dict[Position, list[PlayerValue]] = defaultdict(list)
        for pv in player_values:
            by_position[pv.position].append(pv)

        for players in by_position.values():
            self._assign_for_position(players)

    def _assign_for_position(self, players: list[PlayerValue]) -> None:
        if not players:
            return

        projection_rank = self._percentile_ranks(players, key=lambda p: p.projection)
        value_rank = self._percentile_ranks(players, key=lambda p: p.value_score)

        for pv in players:
            popularity = (
                PROJECTION_RANK_WEIGHT * projection_rank[id(pv)] + VALUE_RANK_WEIGHT * value_rank[id(pv)]
            )
            ownership = MIN_OWNERSHIP_PCT + (MAX_OWNERSHIP_PCT - MIN_OWNERSHIP_PCT) * (
                popularity**CONCENTRATION_EXPONENT
            )
            pv.projected_ownership_pct = round(ownership, 1)

    def _percentile_ranks(self, players: list[PlayerValue], key) -> dict[int, float]:
        """Returns {id(player): percentile in [0, 1]}, 1.0 being the highest."""
        ordered = sorted(players, key=key)
        n = len(ordered)
        if n == 1:
            return {id(ordered[0]): 1.0}
        return {id(p): index / (n - 1) for index, p in enumerate(ordered)}
