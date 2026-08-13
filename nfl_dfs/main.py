"""
CLI entry point.

Usage:
    # the normal way to run this: pick a risk level (1-10, 1=safest,
    # 10=riskiest) and a lineup count — every lineup in the batch is
    # built at EXACTLY that one risk level, no blending, no
    # auto-derived count. Risk level and count are fully independent.
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --risk-scale 5 --num-lineups 50

    # single lineup (num-lineups defaults to 1)
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --risk-scale 8

    # explicit opt-in only: one lineup at each of 5 preset risk levels,
    # for a quick look across the cash-to-GPP spectrum
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --explore

The --salary-csv step is the one manual part of the pipeline (FanDuel
has no public salary API) — everything else (nflverse stats,
schedule, vulnerability, pace, red zone data) runs automatically.
"""

from __future__ import annotations

import argparse

from nfl_dfs.data.nflverse import NflverseDataSource
from nfl_dfs.lineup_builder import DEFAULT_MAX_SALARY_LEFTOVER, MAX_LINEUPS
from nfl_dfs.pipeline import DfsPipeline, label_for_risk


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FanDuel DFS vulnerability/value/lineup pipeline.")
    parser.add_argument("--season", type=int, required=True, help="Season year, e.g. 2026")
    parser.add_argument("--salary-csv", required=True, help="Path to the FanDuel salary CSV export")
    parser.add_argument("--output", default="output/players.json", help="Where to write the JSON output")
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Optional local cache dir for downloaded nflverse CSVs (speeds up repeated local runs)",
    )
    parser.add_argument(
        "--risk-scale",
        type=int,
        default=5,
        choices=range(1, 11),
        metavar="1-10",
        help="1=safest/cash, 10=riskiest/max GPP (default 5). Every lineup built this run uses "
        "EXACTLY this risk level — no blending across levels, no effect on lineup count. "
        "Converts internally to risk_level=(scale-1)/9. Ignored if --explore is set.",
    )
    parser.add_argument(
        "--risk-level",
        type=float,
        default=None,
        help="Advanced: raw 0.0-1.0 risk level instead of --risk-scale. Takes priority over "
        "--risk-scale if both are set.",
    )
    parser.add_argument(
        "--num-lineups",
        type=int,
        default=1,
        help=f"How many lineups to build, all at --risk-scale (default 1). Capped at {MAX_LINEUPS}. "
        "This is completely independent of the risk level — e.g. --risk-scale 5 --num-lineups 50 "
        "builds 50 lineups that are ALL at risk level 5, not a spread across levels.",
    )
    parser.add_argument(
        "--explore",
        action="store_true",
        help="Explicit opt-in: build one lineup at each of 5 preset risk levels (cash through max "
        "GPP) instead of a batch at a single level. Ignores --risk-scale/--risk-level/--num-lineups.",
    )
    parser.add_argument(
        "--risk-levels",
        default=None,
        help="Advanced, only used with --explore: comma-separated risk levels 0.0-1.0 instead of "
        "the default 5-point spread, e.g. '0,0.5,1'.",
    )
    parser.add_argument(
        "--randomness",
        type=float,
        default=1.0,
        help="Multiplier on how differentiated a multi-lineup GPP batch is (default 1.0). "
        ">1 for wilder/more varied batches, <1 for tighter ones, 0 to disable noise-driven "
        "diversity (only distinct local-search optima will differ).",
    )
    parser.add_argument(
        "--skip-redzone",
        action="store_true",
        help="Skip downloading play-by-play for red-zone metrics (faster; loses the WOPR/red-zone "
        "ceiling adjustment, the advanced_metrics in the output, and regression candidates).",
    )
    parser.add_argument(
        "--max-player-salary",
        type=int,
        default=None,
        help="Exclude any player priced above this from lineup consideration entirely "
        "(a punt/no-studs build constraint). Unset by default (no cap).",
    )
    parser.add_argument(
        "--max-salary-leftover",
        type=int,
        default=DEFAULT_MAX_SALARY_LEFTOVER,
        help=f"Push lineups to spend within this much of the $60,000 cap (default ${DEFAULT_MAX_SALARY_LEFTOVER}). "
        "Best-effort: if the pool can't support spending that close to the cap (e.g. combined with a "
        "low --max-player-salary), the actual leftover may exceed this rather than break the cap or fail "
        "outright. Pass a large number (e.g. 60000) to effectively disable this.",
    )
    parser.add_argument(
        "--exclude-players",
        default=None,
        help="Comma-separated player names to exclude from lineup building entirely, e.g. "
        "'Mitchell Trubisky,Christian McCaffrey'. Independent of the automatic injury exclusion "
        "(is_out) — use this for late-breaking news or a personal judgment call, regardless of "
        "whether FanDuel's own injury designation has caught up yet. Matches on a normalized, "
        "substring-tolerant basis, so 'Trubisky' alone is enough.",
    )
    args = parser.parse_args()

    if args.num_lineups > MAX_LINEUPS:
        print(f"--num-lineups capped at {MAX_LINEUPS} (requested {args.num_lineups})")
        args.num_lineups = MAX_LINEUPS

    risk_level = args.risk_level if args.risk_level is not None else (args.risk_scale - 1) / 9.0
    if args.risk_level is None:
        print(f"--risk-scale {args.risk_scale} -> risk_level={risk_level:.3f}")
    print(f"Building {args.num_lineups} lineup(s), all at risk_level={risk_level:.3f}")

    data_source = NflverseDataSource(cache_dir=args.cache_dir)
    pipeline = DfsPipeline(data_source)

    risk_levels = None
    if args.explore and args.risk_levels:
        levels = [float(x.strip()) for x in args.risk_levels.split(",")]
        risk_levels = {lvl: label_for_risk(lvl) for lvl in levels}

    exclude_players = None
    if args.exclude_players:
        exclude_players = [name.strip() for name in args.exclude_players.split(",") if name.strip()]
        print(f"Excluding: {', '.join(exclude_players)}")

    pipeline.run(
        season=args.season,
        salary_csv_path=args.salary_csv,
        output_path=args.output,
        risk_levels=risk_levels,
        single_risk_level=risk_level,
        num_lineups=args.num_lineups,
        randomness=args.randomness,
        skip_redzone=args.skip_redzone,
        max_player_salary=args.max_player_salary,
        max_salary_leftover=args.max_salary_leftover,
        explore=args.explore,
        exclude_players=exclude_players,
    )

    print(f"Done. Wrote player values to {args.output}")


if __name__ == "__main__":
    main()
