"""Latest injury news -> salary-entry injury statuses.

The FanDuel salary CSV is a snapshot taken whenever it was exported, so
a player can be ruled Out (or downgraded to Questionable) afterwards
and the model never sees it. This pulls ESPN's public NFL injuries feed
at run time and folds the *more severe* of (FanDuel status, news
status) into each SalaryEntry, so the existing machinery then applies:
Out/Doubtful/IR -> hard exclusion (value.py), Questionable -> the
0.75 availability discount and 15% exposure cap (pipeline/lineup_builder).

Fail-soft by design: any network/parse problem returns no news and the
run proceeds exactly as before. Every status change is printed.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, replace

from nfl_dfs.models import Position, SalaryEntry
from nfl_dfs.name_matching import normalize_name

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"

# ESPN status text -> FanDuel-style indicator used everywhere else
_STATUS_MAP = {
    "out": "O",
    "injured reserve": "IR",
    "reserve": "IR",
    "suspension": "SUSP",
    "suspended": "SUSP",
    "doubtful": "D",
    "questionable": "Q",
    "day-to-day": "Q",
    "day to day": "Q",
}
# higher = worse; a news status only ever REPLACES a milder one
_SEVERITY = {"": 0, "Q": 1, "D": 2, "O": 3, "SUSP": 3, "IR": 3, "NFI": 3, "PUP": 3}

_TEAM_FIXES = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR"}


@dataclass(frozen=True)
class NewsItem:
    name: str
    team: str  # normalized abbreviation, "" if the feed didn't say
    status: str  # FanDuel-style indicator (O/D/Q/IR/SUSP)
    detail: str


def _fetch_json(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nfl_dfs)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_espn_injuries(payload: dict) -> list[NewsItem]:
    items: list[NewsItem] = []
    for team_block in payload.get("injuries", []) or []:
        for injury in team_block.get("injuries", []) or []:
            athlete = injury.get("athlete") or {}
            name = athlete.get("displayName") or athlete.get("fullName") or ""
            raw_status = str(injury.get("status") or "").strip().lower()
            status = _STATUS_MAP.get(raw_status)
            if not name or not status:
                continue
            team = ((athlete.get("team") or {}).get("abbreviation") or "").upper()
            team = _TEAM_FIXES.get(team, team)
            detail = str(injury.get("shortComment") or injury.get("longComment") or "").strip()
            items.append(NewsItem(name=name, team=team, status=status, detail=detail[:240]))
    return items


def fetch_injury_news() -> list[NewsItem]:
    try:
        return parse_espn_injuries(_fetch_json(ESPN_INJURIES_URL))
    except Exception as error:  # noqa: BLE001 - fail-soft on purpose
        print(f"News: could not fetch injury news ({error}); continuing with FanDuel statuses only.")
        return []


def apply_news(salaries: list[SalaryEntry], news: list[NewsItem]) -> int:
    """Upgrade each matching entry's injury_status to the more severe of
    FanDuel's and the news feed's. SalaryEntry is frozen, so entries are
    replaced IN PLACE in the list. Returns the number of changes."""
    by_name: dict[str, list[NewsItem]] = {}
    for item in news:
        by_name.setdefault(normalize_name(item.name), []).append(item)

    changed = 0
    for index, entry in enumerate(salaries):
        if entry.position == Position.DST:
            continue
        candidates = by_name.get(normalize_name(entry.player_name), [])
        if not candidates:
            continue
        team = _TEAM_FIXES.get(entry.team.upper(), entry.team.upper())
        same_team = [c for c in candidates if c.team == team]
        # same name on another team (or feed has no team): only trust a
        # team-matched item, or a lone teamless one
        pool = same_team or [c for c in candidates if not c.team]
        if not pool:
            continue
        item = max(pool, key=lambda c: _SEVERITY.get(c.status, 0))
        current = entry.injury_status.upper()
        if _SEVERITY.get(item.status, 0) > _SEVERITY.get(current, 0):
            print(f"News: {entry.player_name} ({entry.team}) {current or '-'} -> {item.status}: {item.detail}")
            salaries[index] = replace(
                entry,
                injury_status=item.status,
                injury_details=entry.injury_details or item.detail,
            )
            changed += 1
    print(f"News: {len(news)} injury items fetched, {changed} player status change(s) applied.")
    return changed
