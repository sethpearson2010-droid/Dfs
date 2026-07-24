"""
Computes recent-form (last-5-game) advanced usage metrics per player,
joined internally by nflverse's own player_id — a reliable join since
both weekly stats and play-by-play come from the same source and use
the same GSIS ID convention. This sidesteps the FanDuel-name-matching
problem entirely; that join only happens once, later, in value.py.
"""

from __future__ import annotations

from collections import defaultdict

from nfl_dfs.models import AdvancedMetrics, RedZoneWeekly, WeeklyStatLine

RECENT_FORM_WINDOW = 5


class AdvancedMetricsCalculator:
    def compute(self, weekly_stats: list[WeeklyStatLine], redzone_data: RedZoneWeekly) -> dict[str, AdvancedMetrics]:
        target_share_by_player = self._recent_avg_by_player(weekly_stats, lambda line: line.target_share)
        air_yards_share_by_player = self._recent_avg_by_player(weekly_stats, lambda line: line.air_yards_share)
        wopr_by_player = self._recent_avg_by_player(weekly_stats, lambda line: line.wopr)
        touchdowns_by_player = self._recent_avg_by_player(weekly_stats, lambda line: float(line.touchdowns))

        team_by_player_id = self._team_by_player_id(weekly_stats)
        recent_team_plays = self._recent_avg_by_team(redzone_data.team_redzone_plays)
        recent_player_touches = self._recent_avg_by_key(redzone_data.player_redzone_touches)

        all_player_ids = set(target_share_by_player) | set(recent_player_touches)
        metrics: dict[str, AdvancedMetrics] = {}
        for player_id in all_player_ids:
            touches = recent_player_touches.get(player_id, 0.0)
            team = team_by_player_id.get(player_id)
            team_plays = recent_team_plays.get(team, 0.0) if team else 0.0
            redzone_share = round(touches / team_plays, 3) if team_plays else 0.0

            metrics[player_id] = AdvancedMetrics(
                recent_target_share=target_share_by_player.get(player_id, 0.0),
                recent_air_yards_share=air_yards_share_by_player.get(player_id, 0.0),
                recent_wopr=wopr_by_player.get(player_id, 0.0),
                recent_redzone_touches=touches,
                recent_redzone_share=redzone_share,
                recent_touchdowns_per_game=touchdowns_by_player.get(player_id, 0.0),
            )
        return metrics

    # ------------------------------------------------------------------

    def _recent_avg_by_player(self, weekly_stats: list[WeeklyStatLine], extractor) -> dict[str, float]:
        by_player: dict[str, list[float]] = defaultdict(list)
        for line in weekly_stats:
            by_player[line.player_id].append(extractor(line))

        return {
            player_id: round(sum(values[-RECENT_FORM_WINDOW:]) / len(values[-RECENT_FORM_WINDOW:]), 3)
            for player_id, values in by_player.items()
            if values
        }

    def _team_by_player_id(self, weekly_stats: list[WeeklyStatLine]) -> dict[str, str]:
        # a player's most recent team, in case of an in-season trade
        team_by_player: dict[str, str] = {}
        for line in sorted(weekly_stats, key=lambda line: line.week):
            team_by_player[line.player_id] = line.team
        return team_by_player

    def _recent_avg_by_team(self, weekly_plays: dict[str, list[tuple[int, int]]]) -> dict[str, float]:
        return {
            team: self._avg([plays for _week, plays in weeks[-RECENT_FORM_WINDOW:]])
            for team, weeks in weekly_plays.items()
        }

    def _recent_avg_by_key(self, weekly_counts: dict[str, list[tuple[int, int]]]) -> dict[str, float]:
        return {
            key: self._avg([count for _week, count in weeks[-RECENT_FORM_WINDOW:]])
            for key, weeks in weekly_counts.items()
        }

    def _avg(self, values: list[int]) -> float:
        return round(sum(values) / len(values), 2) if values else 0.0
