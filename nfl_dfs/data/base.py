"""
Abstract base for stat data sources.

Why this exists: today it's nflverse (free). If you ever add
BluecollarDFS or SportsDataIO later, that becomes a second subclass —
nothing in vulnerability.py or pipeline.py needs to change, because
they only depend on this interface, not on nflverse specifics.
This is the Strategy pattern: interchangeable implementations behind
one contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from nfl_dfs.models import GameContext, RedZoneWeekly, WeeklyStatLine


class StatDataSource(ABC):
    """Contract every stat provider must satisfy."""

    @abstractmethod
    def fetch_weekly_stats(self, season: int) -> list[WeeklyStatLine]:
        """Return every player's weekly stat lines for a season."""
        raise NotImplementedError

    @abstractmethod
    def fetch_schedule(self, season: int) -> dict[int, list[tuple[str, str]]]:
        """Return {week: [(home_team, away_team), ...]} for a season."""
        raise NotImplementedError

    @abstractmethod
    def fetch_game_context(self, season: int) -> dict[tuple[str, str], GameContext]:
        """Return {(team, opponent): GameContext} — one entry per team
        per side of each game, keyed both ways round so a lookup by
        either team's own salary-CSV (team, opponent) pair resolves.
        For each (team, opponent) pair with multiple meetings in a
        season, prefers the most recent/upcoming one."""
        raise NotImplementedError

    @abstractmethod
    def fetch_team_plays(self, season: int) -> dict[str, list[tuple[int, int]]]:
        """Return {team: [(week, plays_run), ...]} — offensive plays
        (pass attempts + sacks + carries) per team per week. This is
        the raw material for pace scoring."""
        raise NotImplementedError

    @abstractmethod
    def fetch_redzone_data(self, season: int) -> RedZoneWeekly:
        """Return red-zone touch counts, keyed by player_id (not name
        — see RedZoneWeekly's docstring for why)."""
        raise NotImplementedError
