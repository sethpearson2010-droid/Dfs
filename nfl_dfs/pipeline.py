"""
Pipeline: the Facade over the whole system. Everything else in this
package is a focused, independently testable unit; this is the one
class that knows how they fit together, so main.py and the GitHub
Actions workflow only need to call one method.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from nfl_dfs.advanced_stats import AdvancedMetricsCalculator
from nfl_dfs.data.base import StatDataSource
from nfl_dfs.explanations import explain_player, format_game_log
from nfl_dfs.lineup_builder import DEFAULT_MAX_SALARY_LEFTOVER, LineupBuilder, MAX_LINEUPS
from nfl_dfs.models import Lineup, PlayerValue, Position, RedZoneWeekly
from nfl_dfs.name_matching import normalize_name
from nfl_dfs.ownership import OwnershipEstimator
from nfl_dfs.pace import PaceCalculator
from nfl_dfs.regression import RegressionCalculator
from nfl_dfs.salary import OUT_INJURY_STATUSES, FanDuelSalaryImporter
from nfl_dfs.sleepers import SleeperCalculator
from nfl_dfs.value import RECENT_FORM_WINDOW, ValueCalculator
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


# how much to boost the likely beneficiary's floor/ceiling/projection
# when a same-team, same-position starter is marked Out/IR — see
# DfsPipeline._apply_injury_replacement_boosts. RB gets the largest
# boost since a backup RB's role typically jumps the most of any
# position when the lead back is out (workload is heavily
# concentrated on one player); WR/TE targets are usually already
# spread across more players, so the redistribution to any one
# teammate is more modest.
# FanDuel's real point value for a rushing/receiving TD — regression.py
# scopes to RB/WR/TE only, so this is the correct constant regardless
# of position (no passing TDs, which are worth less, ever apply here).
REGRESSION_TD_POINT_VALUE = 6.0

INJURY_REPLACEMENT_BOOST_BY_POSITION = {
    Position.RB: 0.25,
    Position.WR: 0.15,
    Position.TE: 0.12,
}


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
        lock_target_counts: dict[str, int] | None = None,
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

        weekly_stats = self._fetch_weekly_stats_with_carryover(season)

        # vulnerability, pace, red-zone, and snap-count data are all
        # scoped to the CURRENT season only (see
        # _fetch_weekly_stats_with_carryover's docstring for why they
        # don't get the cross-season carryover player-level stats do)
        # — for a season with zero games played, each of these fetches
        # 404s the same way the player-stats fetch does, and each
        # gracefully degrades to an empty/uninformative result instead
        # of crashing the whole run. This is an honest gap, not a
        # workaround: there's no substitute for real current-season
        # matchup and usage data, so nothing here tries to fake one.
        try:
            vulnerability_calc = VulnerabilityCalculator(self._data_source)
            vulnerability_scores = vulnerability_calc.compute(season)
        except RuntimeError as error:
            print(f"{error} Vulnerability scoring will be empty until season {season} has games.")
            vulnerability_scores = {}

        game_contexts = self._data_source.fetch_game_context(season)  # games.csv spans all seasons in one file, doesn't 404

        try:
            pace_calc = PaceCalculator(self._data_source)
            pace_profiles = pace_calc.compute(season)
        except RuntimeError as error:
            print(f"{error} Pace scoring will be empty until season {season} has games.")
            pace_profiles = {}

        # red zone data requires downloading play-by-play (~19MB
        # compressed) — skip_redzone lets a quick test run bypass that
        if skip_redzone:
            advanced_metrics = {}
        else:
            try:
                redzone_data = self._fetch_redzone_data_with_carryover(season)
                advanced_metrics = self._advanced_calc.compute(weekly_stats, redzone_data)
            except RuntimeError as error:
                print(f"{error} Advanced usage metrics (WOPR, red zone) will be empty until season {season} has games.")
                advanced_metrics = {}

        # snap counts is a small, fast fetch (unlike pbp) — always
        # fetched regardless of skip_redzone, since it's what catches
        # the "technically played, but actually a backup now" case
        try:
            snap_counts = self._data_source.fetch_snap_counts(season)
        except RuntimeError as error:
            print(f"{error} Backup-QB snap-share detection will be skipped until season {season} has games.")
            snap_counts = {}

        salaries = self._salary_importer.load(salary_csv_path)

        auto_replacement_qbs = self._detect_injury_replacement_qbs(salaries)
        combined_include_players = list(dict.fromkeys((include_players or []) + auto_replacement_qbs))

        value_calc = ValueCalculator(
            vulnerability_scores, weekly_stats, game_contexts, pace_profiles, advanced_metrics, snap_counts
        )
        player_values = value_calc.build(salaries, force_include=combined_include_players)

        self._apply_manual_exclusions(player_values, exclude_players)

        self._apply_injury_replacement_boosts(player_values)

        self._ownership_estimator.assign(player_values)

        sleeper_picks = self._sleeper_calc.identify(player_values)
        sleeper_keys = {(sp.player_name, sp.team) for sp in sleeper_picks}

        regression_candidates = self._regression_calc.identify(player_values)
        regression_keys = {(rc.player_name, rc.team) for rc in regression_candidates}
        regression_gap_by_key = {(rc.player_name, rc.team): rc.regression_gap for rc in regression_candidates}

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
                # a real points adjustment, not just a selection-time
                # objective nudge: regression_gap is already "expected
                # TDs per game minus actual TDs per game" from real
                # red-zone volume — worth REGRESSION_TD_POINT_VALUE (6,
                # the real FanDuel value of a rushing/receiving TD,
                # which is what regression.py scopes to) points each.
                # This directly counters "chasing previous week high
                # scorers": a player whose recent scoring looks low
                # because their TDs haven't hit yet, despite real
                # volume that supports more, now shows a projection
                # that reflects the expected regression, not just their
                # (currently unlucky) recent point total.
                gap = max(0.0, regression_gap_by_key.get((pv.player_name, pv.team), 0.0))
                points_adjustment = round(gap * REGRESSION_TD_POINT_VALUE, 2)
                pv.projection = round(pv.projection + points_adjustment, 2)
                pv.floor_projection = round(pv.floor_projection + points_adjustment * 0.5, 2)
                pv.ceiling_projection = round(pv.ceiling_projection + points_adjustment, 2)
                # defensive: same clamp as value.py's, in case a
                # degenerate small-number edge case ever inverts these
                pv.ceiling_projection = max(pv.ceiling_projection, pv.floor_projection)

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
                lock_target_counts=lock_target_counts,
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

    def _fetch_weekly_stats_with_carryover(self, season: int):
        """Answers 'how does Week 1 of a new season get decided when
        there's no current-season data yet?' — it doesn't, not on its
        own. This project's whole projection model is built on recent
        game history, so with zero games played in `season`, every
        player's recent-form average, floor, and ceiling would compute
        from an empty list — a hard 0 across the board, indistinguishable
        from every player being "unmatched".

        Fixed by carrying over the tail of the PREVIOUS season's data
        when the current season doesn't have a full recent-form window
        yet: each player's last `RECENT_FORM_WINDOW` games from last
        season are pulled in with NEGATIVE week numbers, so they sort
        strictly before this season's real games. Because
        `_build_player_averages` etc. always take the last
        `RECENT_FORM_WINDOW` entries chronologically, this means: Week
        0/before-season-starts uses last season's final games outright,
        Week 1 blends in 1 real current game once it exists, and by
        Week 5+ the window is entirely real current-season data with no
        special-casing needed anywhere else in the codebase.

        Deliberately scoped to PLAYER-level projections only (this
        method's output feeds ValueCalculator). Defense vulnerability
        and team pace do NOT get this carryover — they're computed by
        VulnerabilityCalculator/PaceCalculator, which fetch the current
        season directly from the data source themselves, independent of
        this method. That's intentional: roster turnover and scheme
        changes between seasons make cross-season defense/pace
        carryover a much shakier assumption than an individual skill
        player's own recent form. The honest tradeoff: vulnerability
        and pace scores will be genuinely uninformative (empty or
        near-empty) in the first few weeks of a season — there's no
        good substitute for real current-season matchup data, so this
        doesn't try to fake one."""
        try:
            current_stats = self._data_source.fetch_weekly_stats(season)
        except RuntimeError as error:
            # season hasn't started at all yet (0 games, not just "fewer
            # than RECENT_FORM_WINDOW") — nflverse doesn't publish a
            # file until there's at least one game to put in it, so this
            # isn't "fewer weeks than we'd like", it's "no file exists".
            # Full carryover from the previous season is the only
            # option; if that's unavailable too, there's genuinely
            # nothing to build a projection from.
            print(f"{error} Falling back to full carryover from season {season - 1}.")
            current_stats = []

        current_max_week = max((line.week for line in current_stats), default=0)
        self._last_real_season_max_week = current_max_week  # shared with _fetch_redzone_data_with_carryover below

        if current_max_week >= RECENT_FORM_WINDOW:
            return current_stats  # enough real current-season data — no carryover needed

        try:
            previous_stats = self._data_source.fetch_weekly_stats(season - 1)
        except RuntimeError:
            print(f"No carryover data available from season {season - 1} either — proceeding with season {season} alone.")
            return current_stats

        by_player: dict[str, list] = defaultdict(list)
        for line in previous_stats:
            by_player[line.player_name].append(line)

        carryover_lines = []
        for lines in by_player.values():
            lines.sort(key=lambda line: line.week)
            tail = lines[-RECENT_FORM_WINDOW:]
            for i, line in enumerate(tail):
                offset = len(tail) - i  # 1-indexed distance from the end of last season
                carryover_lines.append(replace(line, season=season, week=-offset))

        print(
            f"Season {season} has only {current_max_week} week(s) of data so far — "
            f"carrying over each player's last {RECENT_FORM_WINDOW} games from season {season - 1} "
            f"for player-level projections (defense vulnerability/pace still use season {season} only)."
        )
        return carryover_lines + current_stats

    def _fetch_redzone_data_with_carryover(self, season: int) -> RedZoneWeekly:
        """Red-zone touches specifically come from separate
        play-by-play data (fetch_redzone_data), NOT from the
        weekly_stats this pipeline already carries over above — a real
        gap reported as "red zone targets appear to be season, not
        last week": target_share/WOPR (sourced from weekly_stats) were
        already getting carryover correctly, but red-zone touches
        weren't, so early in a season (when the carryover window
        matters most) they only reflected however many real games had
        been played so far — indistinguishable from "the whole season"
        precisely when the season is only 1-2 weeks old, which is
        exactly what got reported. Mirrors the weekly-stats carryover
        above: each player's/team's last RECENT_FORM_WINDOW real weeks
        from the previous season, renumbered with negative weeks so
        they sort before the current season and phase out naturally as
        real games accumulate."""
        current = self._data_source.fetch_redzone_data(season)
        current_max_week = getattr(self, "_last_real_season_max_week", RECENT_FORM_WINDOW)

        if current_max_week >= RECENT_FORM_WINDOW:
            return current

        try:
            previous = self._data_source.fetch_redzone_data(season - 1)
        except RuntimeError:
            print(f"No red-zone carryover data available from season {season - 1} either.")
            return current

        def merge(current_dict, previous_dict):
            merged: dict[str, list[tuple[int, int]]] = {}
            for key, weeks in previous_dict.items():
                tail = sorted(weeks, key=lambda pair: pair[0])[-RECENT_FORM_WINDOW:]
                merged[key] = [(-(len(tail) - i), count) for i, (_week, count) in enumerate(tail)]
            for key, weeks in current_dict.items():
                merged[key] = merged.get(key, []) + list(weeks)
            return merged

        return RedZoneWeekly(
            player_redzone_touches=merge(current.player_redzone_touches, previous.player_redzone_touches),
            team_redzone_plays=merge(current.team_redzone_plays, previous.team_redzone_plays),
        )

    def _detect_injury_replacement_qbs(self, salaries) -> list[str]:
        """Auto-detects a team's backup QB when their presumptive
        starter (the team's highest-salaried QB — FanDuel's own
        pricing is a reasonable proxy for role) is marked Out/IR in
        the real FanDuel injury data, and force-includes that backup
        the same way --include-players would — reusing the same "make
        viable even with a thin track record" machinery, just
        triggered automatically by real injury news instead of
        needing you to type a name in each week. Only acts when the
        presumptive starter is actually marked out; a backup who's
        merely questionable or a committee situation isn't touched."""
        by_team: dict[str, list] = defaultdict(list)
        for entry in salaries:
            if entry.position == Position.QB:
                by_team[entry.team].append(entry)

        auto_included = []
        for team, qbs in by_team.items():
            if len(qbs) < 2:
                continue
            qbs_sorted = sorted(qbs, key=lambda e: -e.salary)
            starter = qbs_sorted[0]
            if starter.injury_status.upper() not in OUT_INJURY_STATUSES:
                continue
            backups = [e for e in qbs_sorted[1:] if e.injury_status.upper() not in OUT_INJURY_STATUSES]
            if not backups:
                continue
            backup = backups[0]
            auto_included.append(backup.player_name)
            print(
                f"Auto-detected injury replacement: {backup.player_name} (QB, {team}) starting "
                f"in place of {starter.player_name} ({starter.injury_status})"
            )
        return auto_included

    def _apply_injury_replacement_boosts(self, player_values: list[PlayerValue]) -> None:
        """When a team's presumptive starter (highest-salaried player,
        same reasoning as the QB detection above) at RB/WR/TE is
        marked Out/IR, the next-highest-salaried HEALTHY teammate at
        that position is the most likely direct beneficiary of the
        vacated touches/targets — a well-known real DFS pattern
        (a backup RB's role often jumps the most of any position when
        the lead back is out). Boosts floor/ceiling/projection by a
        modest, position-tuned percentage to reflect the expected
        volume increase.

        This is a salary-as-proxy-for-role heuristic, not a real
        depth-chart or target-share redistribution model — it can be
        wrong (a committee situation with no clear single beneficiary,
        or a package player rather than the top backup stepping up).
        Tagged `injury_replacement_for` on the output so it's visible
        and distinguishable from an organically-earned projection,
        and surfaced in the explanation text rather than hidden."""
        by_team_position: dict[tuple[str, Position], list[PlayerValue]] = defaultdict(list)
        for pv in player_values:
            if pv.position in (Position.RB, Position.WR, Position.TE):
                by_team_position[(pv.team, pv.position)].append(pv)

        for (team, position), players in by_team_position.items():
            sorted_players = sorted(players, key=lambda p: -p.salary)
            out_starters = [p for p in sorted_players if p.is_out]
            if not out_starters:
                continue
            vacated = out_starters[0]
            beneficiaries = [
                p
                for p in sorted_players
                if not p.is_out
                and not p.is_stale
                and p.name_match_quality != "unmatched"
                and p.salary < vacated.salary
                and p.projection > 0
            ]
            if not beneficiaries:
                continue
            beneficiary = beneficiaries[0]
            boost = INJURY_REPLACEMENT_BOOST_BY_POSITION.get(position, 0.15)
            beneficiary.projection = round(beneficiary.projection * (1 + boost), 2)
            beneficiary.floor_projection = round(beneficiary.floor_projection * (1 + boost * 0.5), 2)
            beneficiary.ceiling_projection = round(beneficiary.ceiling_projection * (1 + boost), 2)
            beneficiary.injury_replacement_for = vacated.player_name
            print(
                f"Injury replacement boost: {beneficiary.player_name} (+{int(boost * 100)}%) filling in "
                f"for {vacated.player_name} ({team} {position.value})"
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

        matched_names = []
        for pv in player_values:
            normalized_player = normalize_name(pv.player_name)
            if any(exc in normalized_player or normalized_player in exc for exc in normalized_excludes):
                pv.projection = 0.0
                pv.floor_projection = 0.0
                pv.ceiling_projection = 0.0
                pv.manually_excluded = True
                matched_names.append(pv.player_name)

        # requested-vs-matched can differ legitimately (e.g. "Chris"
        # matching several players) or point to a real name mismatch —
        # either way, printing both makes that visible rather than
        # silent, since a request that matches 0 real players looks
        # identical to "exclusion did nothing" from the outside.
        print(f"--exclude-players requested {exclude_players}, matched: {matched_names or '(none)'}")

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
                "recent_game_log": format_game_log(pv.recent_game_log),
                "explanation": explain_player(pv),
                "volatility_label": pv.volatility_label,
                "injury_replacement_for": pv.injury_replacement_for,
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
                    "real_games_in_sample": pv.advanced_metrics.real_games_in_touches_sample,
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
        qb_name = next((s.player.player_name for s in lineup.slots if s.player.position.value == "QB"), "")

        def _slot_dict(s):
            is_stacked = s.player.player_name in lineup.stack_players
            is_bring_back = s.player.player_name in lineup.bring_back_players
            stack_partner = qb_name if (is_stacked or is_bring_back) and qb_name else ""
            return {
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
                "injury_replacement_for": s.player.injury_replacement_for,
                "force_included": s.player.force_included,
                "fanduel_id": s.player.fanduel_id,
                "explanation": explain_player(s.player, stack_partner=stack_partner, is_bring_back=is_bring_back),
                "recent_game_log": format_game_log(s.player.recent_game_log),
            }

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
            "slots": [_slot_dict(s) for s in lineup.slots],
        }
