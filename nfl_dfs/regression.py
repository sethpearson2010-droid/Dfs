"""
Positive touchdown regression detection.

Touchdowns are the highest-variance part of fantasy scoring — a player
can get the exact same real opportunity (targets, carries, red-zone
touches) two weeks in a row and score 0 TDs one week, 2 the next, just
from randomness (a tipped pass, a goal-line fumble, a coach's play
call). Over a large enough league-wide sample, TDs per red-zone touch
converges to a fairly stable rate per position. A player who's been
getting real red-zone volume but scoring fewer TDs than that league
rate would predict has more expected production than their recent
box scores show — a classic "buy low" signal, independent of salary,
matchup, or ownership.

This is deliberately conservative: small samples are noisy (a rate
computed from 2-3 red-zone touches over 5 games means almost nothing),
so both a minimum touch volume and a minimum gap size are required
before flagging anyone.
"""

from __future__ import annotations

from collections import defaultdict

from nfl_dfs.models import PlayerValue, Position, RegressionCandidate

# TD-rate-per-red-zone-touch is only a meaningful signal for positions
# whose scoring is touch-driven this way — QB rushing TDs near the
# goal line follow different patterns, and K/DST don't fit this model
# at all.
ELIGIBLE_POSITIONS = {Position.RB, Position.WR, Position.TE}

# a player needs at least this many red-zone touches per game
# (recent-form average) before their personal TD rate is trusted at
# all — below this, the denominator is too small to mean anything
MIN_REDZONE_TOUCHES = 1.5

# minimum expected-minus-actual gap (in TDs per game) to flag as a
# candidate — filters out noise-level gaps that wouldn't move the
# needle on a real projection
MIN_REGRESSION_GAP = 0.15

TOP_N_PER_POSITION = 3


class RegressionCalculator:
    def identify(self, player_values: list[PlayerValue]) -> list[RegressionCandidate]:
        by_position: dict[Position, list[PlayerValue]] = defaultdict(list)
        for pv in player_values:
            if pv.position in ELIGIBLE_POSITIONS and pv.advanced_metrics and pv.name_match_quality != "unmatched":
                by_position[pv.position].append(pv)

        candidates: list[RegressionCandidate] = []
        for position, players in by_position.items():
            candidates.extend(self._identify_for_position(position, players))
        return candidates

    def _identify_for_position(self, position: Position, players: list[PlayerValue]) -> list[RegressionCandidate]:
        league_rate = self._league_td_rate_per_touch(players)
        if league_rate == 0:
            return []

        found: list[RegressionCandidate] = []
        for pv in players:
            advanced = pv.advanced_metrics
            touches = advanced.recent_redzone_touches
            if touches < MIN_REDZONE_TOUCHES:
                continue  # too small a sample to trust this player's own rate

            expected = league_rate * touches
            gap = expected - advanced.recent_touchdowns_per_game
            if gap < MIN_REGRESSION_GAP:
                continue

            found.append(
                RegressionCandidate(
                    player_name=pv.player_name,
                    position=pv.position,
                    team=pv.team,
                    opponent=pv.opponent,
                    recent_avg_touchdowns=advanced.recent_touchdowns_per_game,
                    recent_redzone_touches=touches,
                    expected_touchdowns_per_game=round(expected, 2),
                    regression_gap=round(gap, 2),
                )
            )

        found.sort(key=lambda c: c.regression_gap, reverse=True)
        return found[:TOP_N_PER_POSITION]

    def _league_td_rate_per_touch(self, players: list[PlayerValue]) -> float:
        # rate = total recent TDs/game summed across the position pool
        # divided by total recent red-zone touches/game summed — a
        # touch-weighted average rather than an average of individual
        # rates, so high-volume players aren't drowned out by noisy
        # low-volume ones.
        total_tds = sum(pv.advanced_metrics.recent_touchdowns_per_game for pv in players)
        total_touches = sum(pv.advanced_metrics.recent_redzone_touches for pv in players)
        return round(total_tds / total_touches, 4) if total_touches else 0.0
