"""
Joins this week's FanDuel salaries with matchup vulnerability, game
script, pace, and each player's own recent scoring to produce a
projection and value score.

Phase-1 projection model combines three independent multipliers
(vulnerability, game script, pace) on top of a recent-average
baseline — deliberately simple so the pipeline is correct end to end.
Swap _project() for a fancier model later without touching upstream
code.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from nfl_dfs.models import (
    AdvancedMetrics,
    GameContext,
    PaceProfile,
    PlayerValue,
    Position,
    SalaryEntry,
    VulnerabilityScore,
    WeeklyStatLine,
)
from nfl_dfs.name_matching import PlayerNameMatcher

# how much the opponent's vulnerability shifts the projection, as a
# fraction of the league-average points allowed at that position.
VULNERABILITY_WEIGHT = 0.35

# how much a team's implied total (vs. league-average implied total)
# shifts the projection.
GAME_SCRIPT_WEIGHT = 0.25
LEAGUE_AVG_IMPLIED_TOTAL = 22.0  # rough long-run NFL team scoring average

# how much a team's play volume (vs. league-average plays/game) shifts
# the projection — the "pace" signal, distinct from game script.
PACE_WEIGHT = 0.15

# floor/ceiling are built from each player's own recent game-to-game
# standard deviation. Ceiling gets a bigger multiplier than floor
# loses — DFS upside is asymmetric (points are bounded at 0 below but
# effectively unbounded above on a big game).
FLOOR_STDEV_MULTIPLIER = 1.0
CEILING_STDEV_MULTIPLIER = 1.5
RECENT_FORM_WINDOW = 5

# WOPR (target share + air yards share, nflverse-computed) and red
# zone touch share are earned-opportunity signals, distinct from raw
# scoring average. Applied to CEILING ONLY, not the point projection —
# a player's recent average already implicitly reflects their usage,
# so re-biasing the mean by the same usage data would double-count it.
# What usage share adds beyond the mean is upside conviction: a player
# whose role is real (high WOPR/red-zone share) but whose recent
# points lag due to TD variance has more true ceiling than the raw
# average alone suggests.
OPPORTUNITY_CEILING_WEIGHT = 0.20

# Team defenses don't appear as individual "players" in nflverse's
# per-player stats (there's no per-player row for a DST unit), so name
# matching against weekly_stats will never find one. Rather than let
# every DST come back unmatched — which would leave the FanDuel D slot
# with zero legal candidates and break lineup building entirely — DST
# gets its own simple heuristic: a flat baseline, adjusted by how much
# the *opposing offense* is implied to score against it. This is a
# known-rough approximation (see README) since it ignores actual
# defensive stats (sacks, takeaways, def TDs) entirely.
DST_BASELINE_PROJECTION = 7.5
DST_OPPONENT_TOTAL_WEIGHT = 0.4
DST_FLOOR_SPREAD = 3.0  # DST scoring is binary/spiky (pick-six, safety), so a wide, flat spread
DST_CEILING_SPREAD = 6.0


class ValueCalculator:
    def __init__(
        self,
        vulnerability_scores: dict[tuple[str, Position], VulnerabilityScore],
        weekly_stats: list[WeeklyStatLine],
        game_contexts: dict[tuple[str, str], GameContext] | None = None,
        pace_profiles: dict[str, PaceProfile] | None = None,
        advanced_metrics: dict[str, AdvancedMetrics] | None = None,
    ) -> None:
        self._vulnerability = vulnerability_scores
        self._game_contexts = game_contexts or {}
        self._pace_profiles = pace_profiles or {}
        self._advanced_metrics = advanced_metrics or {}
        self._recent_player_avg = self._build_player_averages(weekly_stats)
        self._recent_player_stdev = self._build_player_stdevs(weekly_stats)
        self._league_avg_by_position = self._build_league_averages()
        self._league_avg_plays = self._build_league_avg_plays()
        self._name_to_id = self._build_name_to_id(weekly_stats)
        self._league_avg_wopr_by_position, self._league_avg_rz_share_by_position = (
            self._build_league_avg_opportunity(weekly_stats)
        )
        self._name_matcher = PlayerNameMatcher(known_names=list(self._recent_player_avg.keys()))

    def build(self, salaries: list[SalaryEntry]) -> list[PlayerValue]:
        values = [self._value_one(entry) for entry in salaries]
        return sorted(values, key=lambda v: v.value_score, reverse=True)

    # ------------------------------------------------------------------

    def _value_one(self, entry: SalaryEntry) -> PlayerValue:
        if entry.position == Position.DST:
            return self._value_dst(entry)

        vuln = self._vulnerability.get((entry.opponent, entry.position))
        game_context = self._game_contexts.get((entry.team, entry.opponent))
        pace_profile = self._pace_profiles.get(entry.team)

        name_match = self._name_matcher.match(entry.player_name)
        canonical_name = name_match.canonical_name
        base_projection = self._recent_player_avg.get(canonical_name, 0.0) if canonical_name else 0.0
        stdev = self._recent_player_stdev.get(canonical_name, 0.0) if canonical_name else 0.0

        player_id = self._name_to_id.get(canonical_name) if canonical_name else None
        advanced = self._advanced_metrics.get(player_id) if player_id else None

        projection = self._project(entry.position, base_projection, vuln, game_context, pace_profile)
        # scale the raw variance by the same combined multiplier as the
        # point estimate, so a plus-matchup game raises floor/ceiling
        # together rather than just the average
        scale = (projection / base_projection) if base_projection else 1.0
        scaled_stdev = stdev * scale

        floor_projection = max(0.0, projection - FLOOR_STDEV_MULTIPLIER * scaled_stdev)
        ceiling_projection = projection + CEILING_STDEV_MULTIPLIER * scaled_stdev
        ceiling_projection = self._apply_opportunity_ceiling(entry.position, ceiling_projection, advanced)

        return PlayerValue(
            player_name=entry.player_name,
            position=entry.position,
            team=entry.team,
            opponent=entry.opponent,
            salary=entry.salary,
            projection=round(projection, 2),
            matchup_vulnerability=vuln,
            game_context=game_context,
            pace_profile=pace_profile,
            name_match_quality=name_match.quality,
            base_projection=base_projection,
            floor_projection=round(floor_projection, 2),
            ceiling_projection=round(ceiling_projection, 2),
            advanced_metrics=advanced,
        )

    def _apply_opportunity_ceiling(
        self, position: Position, ceiling_projection: float, advanced: AdvancedMetrics | None
    ) -> float:
        if advanced is None:
            return ceiling_projection

        league_wopr = self._league_avg_wopr_by_position.get(position, 0.0)
        league_rz_share = self._league_avg_rz_share_by_position.get(position, 0.0)
        if not league_wopr and not league_rz_share:
            return ceiling_projection  # e.g. QB/K — these metrics aren't meaningful for this position

        wopr_rel = _clamp((advanced.recent_wopr - league_wopr) / league_wopr, -1, 1) if league_wopr else 0.0
        rz_rel = (
            _clamp((advanced.recent_redzone_share - league_rz_share) / league_rz_share, -1, 1)
            if league_rz_share
            else 0.0
        )
        opportunity_score = 0.5 * wopr_rel + 0.5 * rz_rel

        return ceiling_projection * (1 + OPPORTUNITY_CEILING_WEIGHT * opportunity_score)

    def _value_dst(self, entry: SalaryEntry) -> PlayerValue:
        # the opposing offense's own game context tells us their
        # implied total against this defense.
        opposing_offense_context = self._game_contexts.get((entry.opponent, entry.team))

        projection = DST_BASELINE_PROJECTION
        if opposing_offense_context is not None:
            projection -= DST_OPPONENT_TOTAL_WEIGHT * (
                opposing_offense_context.implied_team_total - LEAGUE_AVG_IMPLIED_TOTAL
            )
        projection = max(0.0, projection)

        return PlayerValue(
            player_name=entry.player_name,
            position=entry.position,
            team=entry.team,
            opponent=entry.opponent,
            salary=entry.salary,
            projection=round(projection, 2),
            matchup_vulnerability=None,
            game_context=opposing_offense_context,
            pace_profile=None,
            name_match_quality="exact",  # not name-joined; this path always "matches"
            base_projection=projection,
            floor_projection=round(max(0.0, projection - DST_FLOOR_SPREAD), 2),
            ceiling_projection=round(projection + DST_CEILING_SPREAD, 2),
        )

    def _project(
        self,
        position: Position,
        base_projection: float,
        vuln: VulnerabilityScore | None,
        game_context: GameContext | None,
        pace_profile: PaceProfile | None,
    ) -> float:
        if base_projection == 0.0:
            return base_projection

        projection = base_projection

        if vuln is not None:
            league_avg = self._league_avg_by_position.get(position, vuln.blended_score)
            if league_avg:
                vuln_multiplier = 1 + VULNERABILITY_WEIGHT * ((vuln.blended_score - league_avg) / league_avg)
                projection *= vuln_multiplier

        if game_context is not None:
            script_multiplier = 1 + GAME_SCRIPT_WEIGHT * (
                (game_context.implied_team_total - LEAGUE_AVG_IMPLIED_TOTAL) / LEAGUE_AVG_IMPLIED_TOTAL
            )
            projection *= script_multiplier

        if pace_profile is not None and self._league_avg_plays:
            pace_multiplier = 1 + PACE_WEIGHT * (
                (pace_profile.blended_plays - self._league_avg_plays) / self._league_avg_plays
            )
            projection *= pace_multiplier

        return projection

    def _build_player_averages(self, weekly_stats: list[WeeklyStatLine]) -> dict[str, float]:
        by_player: dict[str, list[float]] = defaultdict(list)
        for line in weekly_stats:
            by_player[line.player_name].append(line.fantasy_points_ppr)

        return {
            name: round(sum(pts[-RECENT_FORM_WINDOW:]) / len(pts[-RECENT_FORM_WINDOW:]), 2)
            for name, pts in by_player.items()
            if pts
        }

    def _build_player_stdevs(self, weekly_stats: list[WeeklyStatLine]) -> dict[str, float]:
        by_player: dict[str, list[float]] = defaultdict(list)
        for line in weekly_stats:
            by_player[line.player_name].append(line.fantasy_points_ppr)

        stdevs: dict[str, float] = {}
        for name, pts in by_player.items():
            recent = pts[-RECENT_FORM_WINDOW:]
            stdevs[name] = round(statistics.pstdev(recent), 2) if len(recent) >= 2 else 0.0
        return stdevs

    def _build_league_averages(self) -> dict[Position, float]:
        by_position: dict[Position, list[float]] = defaultdict(list)
        for (_, position), score in self._vulnerability.items():
            by_position[position].append(score.blended_score)

        return {
            position: round(sum(scores) / len(scores), 2)
            for position, scores in by_position.items()
            if scores
        }

    def _build_league_avg_plays(self) -> float:
        if not self._pace_profiles:
            return 0.0
        values = [p.blended_plays for p in self._pace_profiles.values()]
        return round(sum(values) / len(values), 2) if values else 0.0

    def _build_name_to_id(self, weekly_stats: list[WeeklyStatLine]) -> dict[str, str]:
        name_to_id: dict[str, str] = {}
        for line in weekly_stats:
            name_to_id.setdefault(line.player_name, line.player_id)
        return name_to_id

    def _build_league_avg_opportunity(
        self, weekly_stats: list[WeeklyStatLine]
    ) -> tuple[dict[Position, float], dict[Position, float]]:
        # average recent WOPR per position, computed straight from
        # weekly_stats (cheap — no need to wait on advanced_metrics
        # being passed in, since this only needs the same data already
        # loaded for player averages)
        wopr_by_position: dict[Position, list[float]] = defaultdict(list)
        position_by_player_id: dict[str, Position] = {}
        for line in weekly_stats:
            position_by_player_id[line.player_id] = line.position

        by_player_wopr: dict[str, list[float]] = defaultdict(list)
        for line in weekly_stats:
            by_player_wopr[line.player_id].append(line.wopr)
        for player_id, values in by_player_wopr.items():
            recent = values[-RECENT_FORM_WINDOW:]
            if recent:
                wopr_by_position[position_by_player_id[player_id]].append(sum(recent) / len(recent))

        rz_share_by_position: dict[Position, list[float]] = defaultdict(list)
        for player_id, advanced in self._advanced_metrics.items():
            position = position_by_player_id.get(player_id)
            if position is not None:
                rz_share_by_position[position].append(advanced.recent_redzone_share)

        league_wopr = {pos: sum(v) / len(v) for pos, v in wopr_by_position.items() if v}
        league_rz_share = {pos: sum(v) / len(v) for pos, v in rz_share_by_position.items() if v}
        return league_wopr, league_rz_share


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
