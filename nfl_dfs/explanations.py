"""
Generates a short, human-readable explanation of why a player was
selected for a lineup and what their range of outcomes looks like.

The dashboard is a static GitHub Pages site with no backend — there's
nothing to call at view time, so this composes the explanation once
during the Python pipeline run from signals already computed
elsewhere (value.py's multipliers, sleepers.py/regression.py's flags),
and it's baked directly into lineups.json. Deliberately rule-based,
not a model call: every reason stated here traces to a specific field
on PlayerValue, so the explanation can't say something the rest of
the output doesn't also support.
"""

from __future__ import annotations

from nfl_dfs.models import PlayerValue

# a multiplier has to be at least this far from neutral (1.0) to be
# worth mentioning — otherwise nearly every player would collect a
# "favorable matchup" reason from noise-level multiplier variation
SIGNAL_THRESHOLD = 1.05

LOW_OWNERSHIP_THRESHOLD = 10.0  # percent


def explain_player(player: PlayerValue, stack_partner: str = "", is_bring_back: bool = False) -> str:
    """One or two sentences: why this player, and what range of
    outcomes to expect. `stack_partner` is the QB's name when this
    player is part of a QB+pass-catcher stack; `is_bring_back` marks
    an opposing-team stack piece (see Lineup.stack_players /
    bring_back_players)."""
    reasons: list[str] = []

    if player.is_out or player.is_stale:
        # shouldn't normally reach here (excluded players aren't
        # selected), but if this is ever called on one directly, don't
        # fabricate a positive-sounding reason for a zeroed-out player
        return "Not a real selection — this player was excluded (see injury/staleness status) and has a zeroed-out projection."

    if player.force_included:
        reasons.append("manually force-included — likely stepping into a larger role than the box scores reflect yet")

    if player.vulnerability_multiplier and player.vulnerability_multiplier >= SIGNAL_THRESHOLD:
        reasons.append("a favorable matchup (this defense has given up extra fantasy points to the position recently)")

    if player.game_script_multiplier and player.game_script_multiplier >= SIGNAL_THRESHOLD:
        reasons.append("a game environment with a high implied team total")

    if player.pace_multiplier and player.pace_multiplier >= SIGNAL_THRESHOLD:
        reasons.append("a fast-paced offense generating extra plays per game")

    if player.opportunity_multiplier and player.opportunity_multiplier >= SIGNAL_THRESHOLD:
        reasons.append("real target share / red-zone opportunity beyond his raw scoring average")

    if player.is_sleeper:
        reasons.append("flagged as a statistical value play relative to his salary")

    if player.is_regression_candidate:
        reasons.append("due for positive TD regression given his red-zone touch volume")

    if stack_partner:
        verb = "part of the bring-back" if is_bring_back else "stacked with"
        reasons.append(f"{verb} {stack_partner} for correlated upside")

    if player.projected_ownership_pct is not None and player.projected_ownership_pct < LOW_OWNERSHIP_THRESHOLD:
        reasons.append(f"a low-owned leverage play (~{player.projected_ownership_pct:.0f}% projected ownership)")

    if reasons:
        if len(reasons) == 1:
            reason_text = reasons[0]
        else:
            reason_text = ", ".join(reasons[:-1]) + f", and {reasons[-1]}"
        selection_sentence = f"Selected for {reason_text}."
    else:
        # every player has SOME reason to be in the objective's top
        # pick for their slot — if none of the above stood out, it's
        # simply the best median-projected value at the salary, which
        # is worth saying plainly rather than reaching for a reason
        # that isn't really there
        selection_sentence = "Selected on median projected value at this salary — no single standout signal, just a solid baseline play."

    outcome_sentence = f"Projected range: {player.floor_projection:.1f} (floor) to {player.ceiling_projection:.1f} (ceiling)."
    if player.volatility_label:
        outcome_sentence += f" {player.volatility_label} week-to-week."

    return f"{selection_sentence} {outcome_sentence}"


def format_game_log(recent_game_log: list) -> str:
    """[(18, 2.64), (12, 14.52), ...] (already sorted ascending by
    value.py) -> 'Wk12: 14.5, Wk18: 2.6' for display.

    Only real current-season games are shown — carried-over
    previous-season games (see pipeline.py's
    _fetch_weekly_stats_with_carryover) are tagged with negative week
    numbers internally for projection purposes, but that negative
    number is only a sort key (distance from the season boundary), not
    a real week anyone played, so there's no honest week label to show
    for them. Filtered out here rather than shown as a vague "LastYr"
    entry for every game — a season that's mostly or entirely
    carryover (e.g. Week 1, before any real games) will show an empty
    log, which is the honest state: there ISN'T a current-season log
    yet, rather than a log padded with entries that don't have real
    week numbers."""
    real_games = [(week, points) for week, points in recent_game_log if week > 0]
    if not real_games:
        return ""
    return ", ".join(f"Wk{week}: {points:.1f}" for week, points in real_games)
