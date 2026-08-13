"""
Domain models. Kept as plain dataclasses — no behavior here, just
typed structure — so every other module (data sources, calculators,
importers) operates on the same shared vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Position(str, Enum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    DST = "DST"
    K = "K"

    @classmethod
    def from_raw(cls, raw: str) -> "Position | None":
        """nflverse uses a few extra position codes (FB, etc.) that
        don't map to a fantasy-relevant bucket — return None for those
        so callers can filter them out instead of crashing."""
        try:
            return cls(raw.upper())
        except ValueError:
            return None


@dataclass(frozen=True)
class WeeklyStatLine:
    """One player's box score for one week, as sourced from nflverse."""

    player_id: str
    player_name: str
    position: Position
    team: str
    opponent: str
    season: int
    week: int
    fantasy_points_ppr: float
    targets: int = 0
    carries: int = 0
    target_share: float = 0.0  # this player's share of the team's targets that game
    air_yards_share: float = 0.0  # this player's share of the team's air yards that game
    wopr: float = 0.0  # weighted opportunity rating (nflverse-computed: 1.5*target_share + 0.7*air_yards_share)
    touchdowns: int = 0  # passing + rushing + receiving TDs combined, for the positive-regression signal


@dataclass(frozen=True)
class RedZoneWeekly:
    """Raw red-zone play counts from play-by-play, keyed by nflverse's
    GSIS player_id (not name — pbp uses abbreviated names like
    'T.McBride', but player_id matches stats_player_week's player_id
    exactly, so joining on ID avoids a second fragile name-matching
    problem)."""

    player_redzone_touches: dict[str, list[tuple[int, int]]]  # player_id -> [(week, touches)]
    team_redzone_plays: dict[str, list[tuple[int, int]]]  # team -> [(week, plays)]


@dataclass(frozen=True)
class AdvancedMetrics:
    """Recent-form (last-5-game) advanced usage metrics for one player.
    These inform floor/ceiling confidence (see value.py) rather than
    re-biasing the central point projection, since target share and
    red zone usage are already implicitly reflected in a player's
    recent scoring average — treating them as a second independent
    signal on the mean would double-count the same underlying usage."""

    recent_target_share: float
    recent_air_yards_share: float
    recent_wopr: float
    recent_redzone_touches: float  # per game
    recent_redzone_share: float  # this player's share of their team's red-zone plays
    recent_touchdowns_per_game: float = 0.0  # for the positive-regression signal


@dataclass(frozen=True)
class LineupSlot:
    """One filled roster spot — the slot label (e.g. 'FLEX') can differ
    from the player's actual position (e.g. a WR filling FLEX)."""

    slot: str
    player: PlayerValue


@dataclass(frozen=True)
class Lineup:
    """A complete, salary-cap-legal FanDuel lineup, built at a given
    point on the cash-to-GPP risk slider."""

    slots: list[LineupSlot]
    risk_level: float  # 0.0 = safest/cash, 1.0 = highest-upside/GPP
    total_salary: int
    projected_points: float  # blended floor/ceiling objective the lineup was built on
    floor_points: float
    ceiling_points: float
    stack_players: list[str] = field(default_factory=list)  # QB's own pass-catchers rostered alongside them
    bring_back_players: list[str] = field(default_factory=list)  # opponent's pass-catchers (full game stack)


@dataclass(frozen=True)
class RegressionCandidate:
    """A player whose recent red-zone volume implies more touchdowns,
    league-wide, than they've actually scored recently — a 'positive
    regression' / buy-low signal. TDs are the highest-variance part of
    fantasy scoring, so real opportunity without matching results often
    normalizes upward, independent of salary or matchup this week."""

    player_name: str
    position: Position
    team: str
    opponent: str
    recent_avg_touchdowns: float
    recent_redzone_touches: float
    expected_touchdowns_per_game: float  # league-average TD rate per red-zone touch, applied to this player's volume
    regression_gap: float  # expected minus actual — positive means underperforming their opportunity


@dataclass(frozen=True)
class SleeperPick:
    """A player flagged as a statistical sleeper: below-median salary
    at their position, whose matchup/game-script/pace signals boost
    their projection well above their own recent-form baseline."""

    player_name: str
    position: Position
    team: str
    opponent: str
    salary: int
    base_projection: float
    adjusted_projection: float
    boost_pct: float  # (adjusted / base) - 1, as a fraction
    reasons: list[str]


@dataclass(frozen=True)
class VulnerabilityScore:
    """How much fantasy production a defense concedes to a position,
    recent-form weighted. This is the core 'vulnerability' signal."""

    team: str
    position: Position
    season_avg_allowed: float
    recent_avg_allowed: float  # last N games, weighted more heavily
    games_sampled: int

    @property
    def blended_score(self) -> float:
        """70/30 blend favoring recent form over season-long baseline.
        Recent form matters more for DFS since defenses change with
        injuries/scheme week to week."""
        return 0.7 * self.recent_avg_allowed + 0.3 * self.season_avg_allowed


@dataclass(frozen=True)
class PaceProfile:
    """A team's offensive play volume — the 'pace' half of game
    script. More plays run means more opportunities for every skill
    player on that offense, independent of matchup quality."""

    team: str
    season_avg_plays: float
    recent_avg_plays: float
    games_sampled: int

    @property
    def blended_plays(self) -> float:
        return 0.7 * self.recent_avg_plays + 0.3 * self.season_avg_plays


@dataclass(frozen=True)
class GameContext:
    """Vegas-line-derived game environment for one team in one game.
    This is the 'game script / pace' signal — a defense can be tough
    on paper but if the team is implied for 28 points, volume still
    flows to the offense facing them."""

    team: str
    opponent: str
    spread_line: float  # positive => home favored, per nflverse convention
    total_line: float
    is_home: bool

    @property
    def implied_team_total(self) -> float:
        home_total = self.total_line / 2 + self.spread_line / 2
        away_total = self.total_line / 2 - self.spread_line / 2
        return home_total if self.is_home else away_total


@dataclass(frozen=True)
class SalaryEntry:
    """One row from a FanDuel slate salary CSV export."""

    player_name: str
    position: Position
    team: str
    opponent: str
    salary: int
    fanduel_id: str = ""
    injury_status: str = ""  # FanDuel's own indicator: O (out), Q (questionable), D (doubtful), IR, etc.
    injury_details: str = ""


@dataclass
class PlayerValue:
    """Final joined output: a player with salary + vulnerability-
    adjusted projection + computed value score. This is the row the
    frontend table renders."""

    player_name: str
    position: Position
    team: str
    opponent: str
    salary: int
    projection: float
    matchup_vulnerability: VulnerabilityScore | None
    game_context: GameContext | None = None
    pace_profile: PaceProfile | None = None
    name_match_quality: str = "exact"
    base_projection: float = 0.0  # recent-average, before matchup/script/pace multipliers
    floor_projection: float = 0.0  # projection minus recent game-to-game variance
    ceiling_projection: float = 0.0  # projection plus recent game-to-game variance
    advanced_metrics: AdvancedMetrics | None = None
    projected_ownership_pct: float = 0.0  # heuristic estimate, not real crowd data — see ownership.py
    vulnerability_multiplier: float = 1.0
    game_script_multiplier: float = 1.0
    pace_multiplier: float = 1.0
    opportunity_multiplier: float = 1.0
    is_stale: bool = False  # hasn't recorded a stat line recently enough to trust — likely injured/inactive
    injury_status: str = ""  # FanDuel's own designation (O/Q/D/IR/etc) — see salary.py's OUT_INJURY_STATUSES
    injury_details: str = ""
    is_out: bool = False  # injury_status is in OUT_INJURY_STATUSES — excluded from lineup building entirely
    is_sleeper: bool = False  # set post-hoc by pipeline.py after SleeperCalculator runs; read by lineup_builder
    is_regression_candidate: bool = False  # set post-hoc by pipeline.py after RegressionCalculator runs
    manually_excluded: bool = False  # user-specified --exclude-players match — excluded like is_out
    fanduel_id: str = ""  # for CSV export back to FanDuel's bulk-upload format
    value_score: float = field(init=False)
    smash_score: float = field(init=False)  # ceiling per $1000 salary — "value" using upside, not the mean
    smash_alignment: int = field(init=False)  # how many of the 4 matchup signals are pointing up (0-4)

    def __post_init__(self) -> None:
        # value per $1000 of salary — standard DFS value convention
        self.value_score = round((self.projection / self.salary) * 1000, 3) if self.salary else 0.0
        self.smash_score = round((self.ceiling_projection / self.salary) * 1000, 3) if self.salary else 0.0
        self.smash_alignment = sum(
            1
            for m in (
                self.vulnerability_multiplier,
                self.game_script_multiplier,
                self.pace_multiplier,
                self.opportunity_multiplier,
            )
            if m > 1.02  # small threshold so near-1.0 noise doesn't count as "aligned"
        )
