"""
Sleeper detection.

A high value_score alone isn't a sleeper signal — a min-salary player
with a mediocre-but-nonzero recent average can have a great points-per-
dollar ratio without any real statistical case for upside. A sleeper,
in the DFS sense this tool can actually support, is a player who:

  1. is priced below the median salary at their position (not chalk —
     the field isn't already pricing in the upside)
  2. has a real enough baseline role to trust (recent average above a
     floor — this filters out deep-bench scrubs with one garbage-time
     stat line)
  3. gets a meaningfully positive combined boost from matchup
     vulnerability + game script + pace, i.e. the *reason* to play them
     is statistical, not just cheapness

This is deliberately conservative: it flags fewer, better-justified
picks rather than a long list. Tune the thresholds below per your own
risk tolerance.
"""

from __future__ import annotations

from collections import defaultdict

from nfl_dfs.models import PlayerValue, Position, SleeperPick

# a candidate must be priced at or below this percentile of salaries
# at their position (0.5 = below the position's median salary)
SALARY_PERCENTILE_MAX = 0.5

# minimum recent-average fantasy points to be considered a real enough
# role to trust the projection at all (filters out scrubs/deep bench)
MIN_BASE_PROJECTION_BY_POSITION: dict[Position, float] = {
    Position.QB: 12.0,
    Position.RB: 6.0,
    Position.WR: 6.0,
    Position.TE: 4.0,
    Position.DST: 0.0,
    Position.K: 0.0,
}

# minimum combined boost (adjusted projection vs. own baseline) to
# flag as a sleeper — 0.08 means at least an 8% lift from matchup
# signals alone, not just noise
MIN_BOOST_PCT = 0.08

# skip sleeper detection entirely for a position if there aren't enough
# players to make "below median salary" a meaningful cutoff (guards
# against a real slate's thin positions, e.g. only a few kickers/DSTs
# rostered on a small-slate day)
MIN_CANDIDATE_POOL = 5

TOP_N_PER_POSITION = 3


class SleeperCalculator:
    def identify(self, player_values: list[PlayerValue]) -> list[SleeperPick]:
        by_position: dict[Position, list[PlayerValue]] = defaultdict(list)
        for pv in player_values:
            by_position[pv.position].append(pv)

        picks: list[SleeperPick] = []
        for position, players in by_position.items():
            picks.extend(self._identify_for_position(position, players))
        return picks

    def _identify_for_position(self, position: Position, players: list[PlayerValue]) -> list[SleeperPick]:
        if len(players) < MIN_CANDIDATE_POOL:
            return []

        salary_threshold = self._salary_percentile(players, SALARY_PERCENTILE_MAX)
        min_base = MIN_BASE_PROJECTION_BY_POSITION.get(position, 5.0)

        candidates: list[SleeperPick] = []
        for pv in players:
            if pv.name_match_quality == "unmatched":
                continue
            if pv.salary > salary_threshold:
                continue
            if pv.base_projection < min_base:
                continue

            boost_pct = (pv.projection / pv.base_projection) - 1 if pv.base_projection else 0.0
            if boost_pct < MIN_BOOST_PCT:
                continue

            candidates.append(
                SleeperPick(
                    player_name=pv.player_name,
                    position=pv.position,
                    team=pv.team,
                    opponent=pv.opponent,
                    salary=pv.salary,
                    base_projection=pv.base_projection,
                    adjusted_projection=pv.projection,
                    boost_pct=round(boost_pct, 3),
                    reasons=self._build_reasons(pv),
                )
            )

        candidates.sort(key=lambda p: p.boost_pct, reverse=True)
        return candidates[:TOP_N_PER_POSITION]

    def _build_reasons(self, pv: PlayerValue) -> list[str]:
        reasons: list[str] = []
        if pv.matchup_vulnerability and pv.matchup_vulnerability.blended_score > 0:
            reasons.append(
                f"opponent allows {pv.matchup_vulnerability.recent_avg_allowed} pts/gm "
                f"to {pv.position.value} recently"
            )
        if pv.game_context and pv.game_context.implied_team_total >= 24:
            reasons.append(f"team implied for {pv.game_context.implied_team_total} pts")
        if pv.pace_profile and pv.pace_profile.blended_plays > 0:
            reasons.append(f"team averaging {pv.pace_profile.recent_avg_plays} plays/gm recently")
        return reasons

    def _salary_percentile(self, players: list[PlayerValue], percentile: float) -> int:
        salaries = sorted(pv.salary for pv in players)
        if not salaries:
            return 0
        index = min(int(len(salaries) * percentile), len(salaries) - 1)
        return salaries[index]
