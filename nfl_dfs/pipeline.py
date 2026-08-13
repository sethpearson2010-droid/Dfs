"""
Pipeline: the Facade over the whole system. Everything else in this
package is a focused, independently testable unit; this is the one
class that knows how they fit together, so main.py and the GitHub
Actions workflow only need to call one method.
"""

from __future__ import annotations

import json
from pathlib import Path

from nfl_dfs.advanced_stats import AdvancedMetricsCalculator
from nfl_dfs.data.base import StatDataSource
from nfl_dfs.lineup_builder import DEFAULT_MAX_SALARY_LEFTOVER, LineupBuilder, MAX_LINEUPS
from nfl_dfs.models import Lineup
from nfl_dfs.name_matching import normalize_name
from nfl_dfs.ownership import OwnershipEstimator
from nfl_dfs.pace import PaceCalculator
from nfl_dfs.regression import RegressionCalculator
from nfl_dfs.salary import FanDuelSalaryImporter
from nfl_dfs.sleepers import SleeperCalculator
from nfl_dfs.value import ValueCalculator
from nfl_dfs.vulnerability import VulnerabilityCalculator

# the risk slider isn't infinitely continuous in the output — these
# five points span cash (0.0) to max-upside GPP (1.0) with reasonable
# granularity. Pass a custom list to run() for a different spread.
DEFAULT_RISK_LEVELS: dict[float, str] = {
    0.0: "cash (floor-optimized)",
    0.25: "safe GPP",
    0.5: "balanced",
    0.75: "risky GPP",
    1.0: "max upside (ceiling-optimized)",
}


def label_for_risk(risk_level: float) -> str:
    """Labels by the risk value itself, not by list position, so any
    subset/count of levels still labels correctly."""
    if risk_level <= 0.05:
        return "cash (floor-optimized)"
    if risk_level <= 0.3:
        return "safe GPP"
    if risk_level <= 0.6:
        return "balanced"
    if risk_level <= 0.85:
        return "risky GPP"
    return "max upside (ceiling-optimized)"


class DfsPipeline:
    def __init__(self, data_source: StatDataSource) -> None:
        self._data_source = data_source
        self._salary_importer = FanDuelSalaryImporter()
        self._sleeper_calc = SleeperCalculator()
        self._regression_calc = RegressionCalculator()
        self._lineup_builder = LineupBuilder()
        self._advanced_calc = AdvancedMetricsCalculator()
        self._ownership_estimator = OwnershipEstimator()

    def run(
        self,
        season: int,
        salary_csv_path: str | Path,
        output_path: str | Path,
        risk_levels: dict[float, str] | None = None,
        single_risk_level: float = 0.5,
        num_lineups: int = 1,
        randomness: float = 1.0,
        skip_redzone: bool = False,
        max_player_salary: int | None = None,
        max_salary_leftover: int | None = DEFAULT_MAX_SALARY_LEFTOVER,
        explore: bool = False,
        exclude_players: list[str] | None = None,
        include_players: list[str] | None = None,
    ) -> None:
        self._write_run_config(
            output_path,
            season=season,
            single_risk_level=single_risk_level,
            num_lineups=num_lineups,
            randomness=randomness,
            max_player_salary=max_player_salary,
            max_salary_leftover=max_salary_leftover,
            explore=explore,
            exclude_players=exclude_players or [],
            include_players=include_players or [],
        )

        weekly_stats = self._data_source.fetch_weekly_stats(season)

        vulnerability_calc = VulnerabilityCalculator(self._data_source)
        vulnerability_scores = vulnerability_calc.compute(season)

        game_contexts = self._data_source.fetch_game_context(season)

        pace_calc = PaceCalculator(self._data_source)
        pace_profiles = pace_calc.compute(season)

        # red zone data requires downloading play-by-play (~19MB
        # compressed) — skip_redzone lets a quick test run bypass that
        if skip_redzone:
            advanced_metrics = {}
        else:
            redzone_data = self._data_source.fetch_redzone_data(season)
            advanced_metrics = self._advanced_calc.compute(weekly_stats, redzone_data)

        # snap counts is a small, fast fetch (unlike pbp) — always
        # fetched regardless of skip_redzone, since it's what catches
        # the "technically played, but actually a backup now" case
        snap_counts = self._data_source.fetch_snap_counts(season)

        salaries = self._salary_importer.load(salary_csv_path)

        value_calc = ValueCalculator(
            vulnerability_scores, weekly_stats, game_contexts, pace_profiles, advanced_metrics, snap_counts
        )
        player_values = value_calc.build(salaries, force_include=include_players)

        self._apply_manual_exclusions(player_values, exclude_players)

        self._ownership_estimator.assign(player_values)

        sleeper_picks = self._sleeper_calc.identify(player_values)
        sleeper_keys = {(sp.player_name, sp.team) for sp in sleeper_picks}

        regression_candidates = self._regression_calc.identify(player_values)
        regression_keys = {(rc.player_name, rc.team) for rc in regression_candidates}

        # set directly on the objects (not just tracked via the key
        # sets above) so lineup_builder can read is_sleeper /
        # is_regression_candidate straight off PlayerValue and factor
        # them into which players actually get selected, instead of
        # these being purely informational side-panels.
        for pv in player_values:
            if (pv.player_name, pv.team) in sleeper_keys:
                pv.is_sleeper = True
            if (pv.player_name, pv.team) in regression_keys:
                pv.is_regression_candidate = True

        self._write_output(player_values, output_path, sleeper_keys, regression_keys)
        self._write_sleepers(sleeper_picks, output_path)
        self._write_regression_candidates(regression_candidates, output_path)

        if explore:
            # explicit opt-in only now — one lineup at each of 5 preset
            # risk levels, for a quick look across the spectrum. This
            # used to be the silent DEFAULT whenever risk wasn't set,
            # which meant a blank/misconfigured risk input silently
            # produced 5 lineups spanning different risk levels instead
            # of the batch the user actually asked for — confusing and
            # exactly the "slider" behavior that's been removed.
            self._write_lineups(
                player_values,
                output_path,
                risk_levels or DEFAULT_RISK_LEVELS,
                max_player_salary,
                max_salary_leftover,
            )
            return

        # risk level and lineup count are independent, always-literal
        # inputs now — no auto-derived count, no blending across risk
        # levels. Pick risk 0.5 (or risk-scale 5) and ask for 50
        # lineups: all 50 are built at exactly risk_level=0.5, only the
        # specific players/combinations vary for diversity.
        num_lineups = max(1, min(num_lineups, MAX_LINEUPS))
        if num_lineups > 1:
            lineups = self._lineup_builder.build_many(
                player_values,
                single_risk_level,
                num_lineups,
                randomness=randomness,
                max_player_salary=max_player_salary,
                max_salary_leftover=max_salary_leftover,
            )
            self._write_lineup_set(lineups, single_risk_level, output_path)
        else:
            lineup = self._lineup_builder.build(
                player_values,
                single_risk_level,
                max_player_salary=max_player_salary,
                max_salary_leftover=max_salary_leftover,
            )
            self._write_lineup_set([lineup] if lineup else [], single_risk_level, output_path)

    # ------------------------------------------------------------------

    def _write_run_config(
        self,
        output_path: str | Path,
        season: int,
        single_risk_level: float,
        num_lineups: int,
        randomness: float,
        max_player_salary: int | None,
        max_salary_leftover: int | None,
        explore: bool,
        exclude_players: list[str],
        include_players: list[str],
    ) -> None:
        """Records the actual parameters this run used, so the
        dashboard's rebuild-without-this-player feature (a browser-side
        GitHub Actions workflow_dispatch call — see frontend/index.html)
        knows what settings to replay rather than guessing or resetting
        to defaults."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        config_path = output_path.parent / "run_config.json"

        config_path.write_text(
            json.dumps(
                {
                    "season": season,
                    "risk_level": round(single_risk_level, 3),
                    "risk_scale": round(single_risk_level * 9 + 1),
                    "num_lineups": num_lineups,
                    "randomness": randomness,
                    "max_player_salary": max_player_salary,
                    "max_salary_leftover": max_salary_leftover,
                    "explore": explore,
                    "exclude_players": exclude_players,
                    "include_players": include_players,
                },
                indent=2,
            )
        )

    def _apply_manual_exclusions(self, player_values, exclude_players: list[str] | None) -> None:
        """Zeroes out and flags any player matching a name in
        exclude_players — an easy override for late-breaking news or a
        personal judgment call, independent of the automatic is_out
        injury exclusion (which only fires for FanDuel's own O/IR/etc
        designations). Matches on a normalized, substring-tolerant
        basis so 'Trubisky' matches 'Mitchell Trubisky' without needing
        the exact full name or worrying about Jr./II punctuation."""
        if not exclude_players:
            return

        normalized_excludes = [normalize_name(name) for name in exclude_players if name.strip()]
        if not normalized_excludes:
            return

        for pv in player_values:
            normalized_player = normalize_name(pv.player_name)
            if any(exc in normalized_player or normalized_player in exc for exc in normalized_excludes):
                pv.projection = 0.0
                pv.floor_projection = 0.0
                pv.ceiling_projection = 0.0
                pv.manually_excluded = True

    def _write_output(
        self,
        player_values,
        output_path: str | Path,
        sleeper_keys: set[tuple[str, str]] | None = None,
        regression_keys: set[tuple[str, str]] | None = None,
    ) -> None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sleeper_keys = sleeper_keys or set()
        regression_keys = regression_keys or set()

        serializable = []
        for pv in player_values:
            row = {
                "player_name": pv.player_name,
                "position": pv.position.value,
                "team": pv.team,
                "opponent": pv.opponent,
                "salary": pv.salary,
                "projection": pv.projection,
                "floor_projection": pv.floor_projection,
                "ceiling_projection": pv.ceiling_projection,
                "value_score": pv.value_score,
                "name_match_quality": pv.name_match_quality,
                "is_stale": pv.is_stale,
                "is_backup_qb": pv.is_backup_qb,
                "force_included": pv.force_included,
                "injury_status": pv.injury_status,
                "injury_details": pv.injury_details,
                "is_out": pv.is_out,
                "manually_excluded": pv.manually_excluded,
                "fanduel_id": pv.fanduel_id,
                "is_sleeper": (pv.player_name, pv.team) in sleeper_keys,
                "is_regression_candidate": (pv.player_name, pv.team) in regression_keys,
                "projected_ownership_pct": pv.projected_ownership_pct,
                "smash_score": pv.smash_score,
                "smash_alignment": pv.smash_alignment,
                "signal_multipliers": {
                    "vulnerability": pv.vulnerability_multiplier,
                    "game_script": pv.game_script_multiplier,
                    "pace": pv.pace_multiplier,
                    "opportunity": pv.opportunity_multiplier,
                },
            }
            if pv.matchup_vulnerability:
                row["matchup_vulnerability"] = {
                    "recent_avg_allowed": pv.matchup_vulnerability.recent_avg_allowed,
                    "season_avg_allowed": pv.matchup_vulnerability.season_avg_allowed,
                    "games_sampled": pv.matchup_vulnerability.games_sampled,
                }
            if pv.game_context:
                row["game_context"] = {
                    "implied_team_total": round(pv.game_context.implied_team_total, 1),
                    "spread_line": pv.game_context.spread_line,
                    "total_line": pv.game_context.total_line,
                    "is_home": pv.game_context.is_home,
                }
            if pv.pace_profile:
                row["pace"] = {
                    "recent_avg_plays": pv.pace_profile.recent_avg_plays,
                    "season_avg_plays": pv.pace_profile.season_avg_plays,
                }
            if pv.advanced_metrics:
                row["advanced_metrics"] = {
                    "target_share": pv.advanced_metrics.recent_target_share,
                    "air_yards_share": pv.advanced_metrics.recent_air_yards_share,
                    "wopr": pv.advanced_metrics.recent_wopr,
                    "redzone_touches_per_game": pv.advanced_metrics.recent_redzone_touches,
                    "redzone_share": pv.advanced_metrics.recent_redzone_share,
                }
            serializable.append(row)

        output_path.write_text(json.dumps(serializable, indent=2))

    def _write_sleepers(self, sleeper_picks, output_path: str | Path) -> None:
        output_path = Path(output_path)
        sleepers_path = output_path.parent / "sleepers.json"

        serializable = [
            {
                "player_name": sp.player_name,
                "position": sp.position.value,
                "team": sp.team,
                "opponent": sp.opponent,
                "salary": sp.salary,
                "base_projection": sp.base_projection,
                "adjusted_projection": sp.adjusted_projection,
                "boost_pct": sp.boost_pct,
                "reasons": sp.reasons,
            }
            for sp in sleeper_picks
        ]
        sleepers_path.write_text(json.dumps(serializable, indent=2))

    def _write_regression_candidates(self, regression_candidates, output_path: str | Path) -> None:
        output_path = Path(output_path)
        regression_path = output_path.parent / "regression_candidates.json"

        serializable = [
            {
                "player_name": rc.player_name,
                "position": rc.position.value,
                "team": rc.team,
                "opponent": rc.opponent,
                "recent_avg_touchdowns": rc.recent_avg_touchdowns,
                "recent_redzone_touches": rc.recent_redzone_touches,
                "expected_touchdowns_per_game": rc.expected_touchdowns_per_game,
                "regression_gap": rc.regression_gap,
            }
            for rc in regression_candidates
        ]
        regression_path.write_text(json.dumps(serializable, indent=2))

    def _write_lineups(
        self,
        player_values,
        output_path: str | Path,
        risk_levels: dict[float, str],
        max_player_salary: int | None = None,
        max_salary_leftover: int | None = DEFAULT_MAX_SALARY_LEFTOVER,
    ) -> None:
        output_path = Path(output_path)
        lineups_path = output_path.parent / "lineups.json"

        serializable = []
        for risk_level, label in sorted(risk_levels.items()):
            lineup = self._lineup_builder.build(
                player_values,
                risk_level,
                max_player_salary=max_player_salary,
                max_salary_leftover=max_salary_leftover,
            )
            if lineup is None:
                serializable.append(self._error_entry(risk_level, label, lineup_number=1))
                continue
            serializable.append(self._serialize_lineup(lineup, label, lineup_number=1, total=1))

        lineups_path.write_text(json.dumps(serializable, indent=2))

    def _write_lineup_set(
        self,
        lineups: list[Lineup],
        risk_level: float,
        output_path: str | Path,
    ) -> None:
        output_path = Path(output_path)
        lineups_path = output_path.parent / "lineups.json"
        label = label_for_risk(risk_level)

        if not lineups:
            serializable = [self._error_entry(risk_level, label, lineup_number=1)]
        else:
            serializable = [
                self._serialize_lineup(lineup, label, lineup_number=i + 1, total=len(lineups))
                for i, lineup in enumerate(lineups)
            ]

        lineups_path.write_text(json.dumps(serializable, indent=2))

    def _error_entry(self, risk_level: float, label: str, lineup_number: int) -> dict:
        return {
            "risk_level": risk_level,
            "label": label,
            "lineup_number": lineup_number,
            "error": "Could not build a legal lineup from this salary pool "
            "(a required position may be missing entirely from the CSV).",
        }

    def _serialize_lineup(
        self,
        lineup: Lineup,
        label: str,
        lineup_number: int,
        total: int,
    ) -> dict:
        return {
            "risk_level": lineup.risk_level,
            "label": label,
            "lineup_number": lineup_number,
            "total_lineups": total,
            "total_salary": lineup.total_salary,
            "salary_remaining": self._lineup_builder.salary_cap - lineup.total_salary,
            "projected_points": lineup.projected_points,
            "floor_points": lineup.floor_points,
            "ceiling_points": lineup.ceiling_points,
            "stack_players": lineup.stack_players,
            "bring_back_players": lineup.bring_back_players,
            "slots": [
                {
                    "slot": s.slot,
                    "player_name": s.player.player_name,
                    "position": s.player.position.value,
                    "team": s.player.team,
                    "opponent": s.player.opponent,
                    "salary": s.player.salary,
                    "floor_projection": s.player.floor_projection,
                    "ceiling_projection": s.player.ceiling_projection,
                    "projected_ownership_pct": s.player.projected_ownership_pct,
                    "is_sleeper": s.player.is_sleeper,
                    "is_regression_candidate": s.player.is_regression_candidate,
                    "injury_status": s.player.injury_status,
                    "injury_details": s.player.injury_details,
                    "is_backup_qb": s.player.is_backup_qb,
                    "force_included": s.player.force_included,
                    "fanduel_id": s.player.fanduel_id,
                }
                for s in lineup.slots
            ],
        }
