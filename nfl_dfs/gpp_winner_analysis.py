"""
Analyzes real, imported GPP-winning lineups (inputs/gpp_winners.json) for
concrete, computable patterns — salary distribution by roster slot, FLEX
position tendencies, QB salary tier, stacking rate, and how much of the
$60,000 cap winners actually use.

This is explicitly NOT a model that predicts winners — it's a small,
growing dataset of real outcomes, and every statistic here should be
read with its sample size attached. Two winning lineups is enough to
notice a striking, exactly-consistent pattern (see salary utilization
below) but not enough to treat any single number as a target to hit
precisely; this module reports the real distribution as more lineups
get imported, rather than pretending a small sample is a robust model.

WEEKLY WORKFLOW (see README's "Learning from real GPP-winning lineups"
section for the full writeup): each week, once that week's contest
results are in, append the new winning lineup(s) to
inputs/gpp_winners.json in the same shape as the existing entries, then
re-run the pipeline (this module runs automatically as part of it) or
`python -m nfl_dfs.gpp_winner_analysis` directly. The per-week trend
section below exists specifically so a real shift in the data (not
just noise) is visible as more weeks accumulate — a pattern seen in
literally every imported week so far is worth a real tuning change in
lineup_builder.py; a pattern that's drifting or inconsistent week to
week is worth watching, not acting on yet.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

SALARY_CAP = 60000
ROSTER_SIZE = 9

# a QB and a pass-catcher (WR/TE) from the SAME team is the classic
# GPP correlation play — both score together when that offense has a
# big game. RB isn't counted here since a QB+RB stack from the same
# team is much less common/correlated (a big RB game usually implies
# a run-heavy, lower-passing script, working against the QB's own
# score) — the well-established "stack" concept in DFS specifically
# means a QB with his own pass-catchers.
STACK_ELIGIBLE_POSITIONS = {"WR", "TE"}


def load_winners(path: str | Path = "inputs/gpp_winners.json") -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _lineup_summary(lineup: dict) -> dict:
    """Per-lineup stats used both for the aggregate analysis and for
    the per-week trend list — computed once, shared by both, so the
    two views can never disagree with each other."""
    players = lineup["players"]
    total_salary = sum(p["salary"] for p in players)
    qb = next((p for p in players if p["position"] == "QB"), None)
    flex = next((p for p in players if p.get("roster_slot") == "FLEX"), None)

    stack_partners = 0
    if qb:
        stack_partners = sum(
            1 for p in players if p["team"] == qb["team"] and p["position"] in STACK_ELIGIBLE_POSITIONS
        )

    return {
        "season": lineup.get("season"),
        "week": lineup.get("week"),
        "contest": lineup.get("contest"),
        "total_salary": total_salary,
        "leftover": SALARY_CAP - total_salary,
        "qb_salary": qb["salary"] if qb else None,
        "qb_name": qb["name"] if qb else None,
        "flex_position": flex["position"] if flex else None,
        "flex_name": flex["name"] if flex else None,
        "stack_partners": stack_partners,
        "has_stack": stack_partners > 0,
        "total_points": round(sum(p["points"] for p in players), 2),
    }


def analyze(winners: list[dict]) -> dict:
    if not winners:
        return {"sample_size": 0, "note": "No winning lineups imported yet."}

    # sort by (season, week) so the trend view reads chronologically
    # regardless of the order lineups were appended in
    sorted_winners = sorted(winners, key=lambda w: (w.get("season", 0), w.get("week", 0)))
    weekly_trend = [_lineup_summary(w) for w in sorted_winners]

    total_salaries = [w["total_salary"] for w in weekly_trend]
    leftover_salaries = [w["leftover"] for w in weekly_trend]
    qb_salaries = [w["qb_salary"] for w in weekly_trend if w["qb_salary"] is not None]
    stack_count = sum(1 for w in weekly_trend if w["has_stack"])
    stack_sizes = [w["stack_partners"] for w in weekly_trend if w["has_stack"]]

    flex_entries = []
    per_slot_salaries: dict[str, list[int]] = defaultdict(list)
    per_position_points: dict[str, list[float]] = defaultdict(list)
    for lineup in sorted_winners:
        for p in lineup["players"]:
            slot = p.get("roster_slot", p["position"])
            per_slot_salaries[slot].append(p["salary"])
            per_position_points[p["position"]].append(p["points"])
            if slot == "FLEX":
                flex_entries.append((p["position"], p["salary"], p["points"]))

    flex_position_counts = Counter(pos for pos, _salary, _points in flex_entries)

    def summarize(values: list) -> dict:
        return {
            "min": min(values),
            "max": max(values),
            "avg": round(statistics.mean(values), 1),
            "median": round(statistics.median(values), 1),
        }

    # a real trend needs at least 3 data points to distinguish
    # "consistent so far" from "just noise" — below that, the trend
    # section still shows the raw weekly list (always useful) but the
    # note says explicitly that it's too early to call anything a
    # trend one way or the other.
    MIN_SAMPLE_FOR_TREND_CLAIM = 3
    is_trend_established = len(weekly_trend) >= MIN_SAMPLE_FOR_TREND_CLAIM

    return {
        "sample_size": len(winners),
        "weekly_trend": weekly_trend,
        "trend_note": (
            f"{len(weekly_trend)} week(s) imported so far — "
            + (
                "enough to start distinguishing a real, repeating pattern from a single week's noise, "
                "though still a small sample."
                if is_trend_established
                else "too early to call anything a trend rather than noise; keep importing weekly."
            )
        ),
        "salary_utilization": {
            "total_salary_used": summarize(total_salaries),
            "leftover": summarize(leftover_salaries),
            "note": (
                "How much of the $60,000 cap winning lineups actually used. "
                "A small sample can still show a striking, consistent pattern "
                "(e.g. every winner so far spending within a few hundred "
                "dollars of the full cap) — worth treating as a real signal "
                "even with few data points, since it's a strong, repeatable "
                "behavior, not a borderline average."
            ),
        },
        "qb_salary_tier": {
            **summarize(qb_salaries),
            "note": "The salary of the QB actually rostered in each winning lineup — not necessarily the most expensive QB available that week.",
        },
        "flex_position_tendency": {
            "counts": dict(flex_position_counts),
            "entries": [
                {"position": pos, "salary": salary, "points": points} for pos, salary, points in flex_entries
            ],
            "note": "Which position wins FLEX in practice, and at what salary/outcome — small samples will look position-heavy just from which games happened to break that way.",
        },
        "salary_by_roster_slot": {
            slot: summarize(salaries) for slot, salaries in sorted(per_slot_salaries.items())
        },
        "points_by_position": {
            position: summarize([round(p) for p in points]) for position, points in per_position_points.items()
        },
        "stacking": {
            "lineups_with_a_stack": stack_count,
            "lineups_without_a_stack": len(winners) - stack_count,
            "stack_rate_pct": round(100 * stack_count / len(winners), 1),
            "stack_size_distribution": dict(Counter(stack_sizes)),
            "note": "A QB paired with one or more WR/TE from his own team — the classic GPP correlation play. RB isn't counted as a stack partner since a big RB game usually implies a run-heavy script, working against the QB's own score.",
        },
    }


def write_analysis(output_path: str | Path = "output/gpp_winner_analysis.json") -> dict:
    winners = load_winners()
    result = analyze(winners)
    Path(output_path).write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    result = write_analysis()
    print(json.dumps(result, indent=2))
