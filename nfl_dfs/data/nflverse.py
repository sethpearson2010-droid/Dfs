"""
nflverse data source.

Free, no API key: reads directly from GitHub release assets published
by the nflverse project. URLs are stable release-asset links, not the
repo's default branch, so they don't change format under us.
"""

from __future__ import annotations

import csv
import gzip
import io
import urllib.request
from collections import defaultdict
from pathlib import Path

from nfl_dfs.data.base import StatDataSource
from nfl_dfs.models import GameContext, Position, RedZoneWeekly, WeeklyStatLine

BASE_URL = "https://github.com/nflverse/nflverse-data/releases/download"
SCHEDULE_URL = f"{BASE_URL}/schedules/games.csv"  # all seasons, one file


def _weekly_stats_url(season: int) -> str:
    # nflverse publishes one file per season under this tag; confirmed
    # available all the way back to 1999 and current through this year.
    return f"{BASE_URL}/stats_player/stats_player_week_{season}.csv"


def _team_stats_url(season: int) -> str:
    return f"{BASE_URL}/stats_team/stats_team_week_{season}.csv"


def _pbp_url(season: int) -> str:
    return f"{BASE_URL}/pbp/play_by_play_{season}.csv.gz"


# only need a handful of the ~370 pbp columns for red-zone counting;
# yardline_100 <= 20 defines "red zone" by the standard convention
RED_ZONE_YARDLINE_THRESHOLD = 20.0

_HEADERS = {"User-Agent": "Mozilla/5.0 (nfl-dfs-tool)"}


class NflverseDataSource(StatDataSource):
    """Fetches weekly stats + schedules from nflverse.

    cache_dir: if set, downloaded CSVs are saved there so repeated
    runs (e.g. local dev, or a workflow re-run) don't re-download the
    full multi-season file every time. Pass None to always fetch fresh
    (the scheduled job should do this, to guarantee current data).
    """

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # StatDataSource interface
    # ------------------------------------------------------------------

    def fetch_weekly_stats(self, season: int) -> list[WeeklyStatLine]:
        rows = self._get_csv_rows(_weekly_stats_url(season), f"stats_player_week_{season}.csv")
        stat_lines: list[WeeklyStatLine] = []

        for row in rows:
            if row["season_type"] != "REG":
                continue  # skip playoffs for regular-season vulnerability baseline

            position = Position.from_raw(row["position"])
            if position is None:
                continue  # skip FB, K-specialists misc rows, etc.

            opponent = row.get("opponent_team") or ""
            if not opponent:
                continue

            stat_lines.append(
                WeeklyStatLine(
                    player_id=row["player_id"],
                    player_name=row["player_display_name"],
                    position=position,
                    team=row["team"],
                    opponent=opponent,
                    season=season,
                    week=int(row["week"]),
                    fantasy_points_ppr=float(row["fantasy_points_ppr"] or 0.0),
                    targets=int(float(row["targets"] or 0)),
                    carries=int(float(row["carries"] or 0)),
                    target_share=float(row.get("target_share") or 0.0),
                    air_yards_share=float(row.get("air_yards_share") or 0.0),
                    wopr=float(row.get("wopr") or 0.0),
                    touchdowns=int(float(row.get("passing_tds") or 0))
                    + int(float(row.get("rushing_tds") or 0))
                    + int(float(row.get("receiving_tds") or 0)),
                )
            )
        return stat_lines

    def fetch_schedule(self, season: int) -> dict[int, list[tuple[str, str]]]:
        rows = self._get_csv_rows(SCHEDULE_URL, "games.csv")
        schedule: dict[int, list[tuple[str, str]]] = defaultdict(list)

        for row in rows:
            if int(row["season"]) != season:
                continue
            if row["game_type"] != "REG":
                continue
            week = int(row["week"])
            schedule[week].append((row["home_team"], row["away_team"]))

        return dict(schedule)

    def fetch_game_context(self, season: int) -> dict[tuple[str, str], GameContext]:
        rows = self._get_csv_rows(SCHEDULE_URL, "games.csv")
        # keep, per (team, opponent) pair, the row for the game with no
        # result yet (upcoming) if one exists, else the highest week
        # played — that's the most relevant line for "this week's slate"
        best_row_by_pair: dict[tuple[str, str], dict] = {}

        for row in rows:
            if int(row["season"]) != season or row["game_type"] != "REG":
                continue
            if not row.get("spread_line") or not row.get("total_line"):
                continue  # lines not posted yet for this game

            week = int(row["week"])
            is_upcoming = not row.get("result")

            for team, opponent in ((row["home_team"], row["away_team"]), (row["away_team"], row["home_team"])):
                key = (team, opponent)
                existing = best_row_by_pair.get(key)
                if existing is None:
                    best_row_by_pair[key] = {**row, "_week": week, "_upcoming": is_upcoming}
                    continue
                # prefer upcoming over played; among same status, prefer later week
                better = (is_upcoming, week) > (existing["_upcoming"], existing["_week"])
                if better:
                    best_row_by_pair[key] = {**row, "_week": week, "_upcoming": is_upcoming}

        contexts: dict[tuple[str, str], GameContext] = {}
        for (team, opponent), row in best_row_by_pair.items():
            contexts[(team, opponent)] = GameContext(
                team=team,
                opponent=opponent,
                spread_line=float(row["spread_line"]),
                total_line=float(row["total_line"]),
                is_home=(row["home_team"] == team),
            )
        return contexts

    def fetch_team_plays(self, season: int) -> dict[str, list[tuple[int, int]]]:
        rows = self._get_csv_rows(_team_stats_url(season), f"stats_team_week_{season}.csv")
        plays_by_team: dict[str, list[tuple[int, int]]] = defaultdict(list)

        for row in rows:
            if row["season_type"] != "REG":
                continue
            attempts = int(float(row["attempts"] or 0))
            sacks = int(float(row["sacks_suffered"] or 0))
            carries = int(float(row["carries"] or 0))
            plays = attempts + sacks + carries  # standard offensive-plays proxy
            plays_by_team[row["team"]].append((int(row["week"]), plays))

        for team in plays_by_team:
            plays_by_team[team].sort(key=lambda pair: pair[0])
        return dict(plays_by_team)

    def fetch_redzone_data(self, season: int) -> RedZoneWeekly:
        player_touches: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        team_plays: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))

        for row in self._stream_pbp_rows(season):
            if row.get("season_type") != "REG":
                continue

            yardline_raw = row.get("yardline_100")
            if not yardline_raw:
                continue
            try:
                yardline = float(yardline_raw)
            except ValueError:
                continue
            if yardline > RED_ZONE_YARDLINE_THRESHOLD:
                continue

            is_rush = row.get("rush") == "1"
            is_pass = row.get("pass") == "1"
            if not (is_rush or is_pass):
                continue

            week_raw = row.get("week")
            if not week_raw:
                continue
            week = int(float(week_raw))

            posteam = row.get("posteam") or ""
            if posteam:
                team_plays[posteam][week] += 1

            if is_rush and row.get("rusher_player_id"):
                player_touches[row["rusher_player_id"]][week] += 1
            elif is_pass and row.get("receiver_player_id"):
                player_touches[row["receiver_player_id"]][week] += 1

        return RedZoneWeekly(
            player_redzone_touches={pid: sorted(weeks.items()) for pid, weeks in player_touches.items()},
            team_redzone_plays={team: sorted(weeks.items()) for team, weeks in team_plays.items()},
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _stream_pbp_rows(self, season: int):
        """Streams play-by-play rows without ever holding the full
        decompressed file (~150MB+ for a season) in memory at once —
        only the compressed bytes (~19MB) are buffered if caching, and
        gzip decompression + CSV parsing happen row by row."""
        cache_path = self.cache_dir / f"play_by_play_{season}.csv.gz" if self.cache_dir else None

        if cache_path and cache_path.exists():
            fileobj = cache_path.open("rb")
        else:
            request = urllib.request.Request(_pbp_url(season), headers=_HEADERS)
            response = urllib.request.urlopen(request, timeout=180)
            if cache_path:
                compressed = response.read()
                cache_path.write_bytes(compressed)
                fileobj = io.BytesIO(compressed)
            else:
                fileobj = response

        with fileobj, gzip.GzipFile(fileobj=fileobj) as gz:
            text_stream = io.TextIOWrapper(gz, encoding="utf-8")
            reader = csv.DictReader(text_stream)
            yield from reader

    def _get_csv_rows(self, url: str, cache_name: str) -> list[dict]:
        text = self._get_text(url, cache_name)
        return list(csv.DictReader(io.StringIO(text)))

    def _get_text(self, url: str, cache_name: str) -> str:
        cache_path = self.cache_dir / cache_name if self.cache_dir else None
        if cache_path and cache_path.exists():
            return cache_path.read_text()

        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")

        if cache_path:
            cache_path.write_text(text)
        return text
