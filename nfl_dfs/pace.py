"""
Pace scoring: how many offensive plays does each team run, and how has
that trended recently. Mirrors vulnerability.py's season/recent-form
blend, applied to a team's own play volume instead of what it allows.
"""

from __future__ import annotations

from nfl_dfs.data.base import StatDataSource
from nfl_dfs.models import PaceProfile

RECENT_FORM_WINDOW = 5


class PaceCalculator:
    def __init__(self, data_source: StatDataSource) -> None:
        self._data_source = data_source

    def compute(self, season: int) -> dict[str, PaceProfile]:
        plays_by_team = self._data_source.fetch_team_plays(season)

        profiles: dict[str, PaceProfile] = {}
        for team, week_plays in plays_by_team.items():
            values = [plays for _week, plays in week_plays]  # already sorted by week
            recent_values = values[-RECENT_FORM_WINDOW:]
            profiles[team] = PaceProfile(
                team=team,
                season_avg_plays=_avg(values),
                recent_avg_plays=_avg(recent_values),
                games_sampled=len(values),
            )
        return profiles


def _avg(values: list[int]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0
