"""
CLI entry point.

Usage:
    # 5-point cash-to-GPP exploration (default) — one lineup at each preset level
    python -m nfl_dfs.main --season 2026 --salary-csv path/to/fanduel.csv --output output/players.json

    # the slider now drives BOTH risk and lineup count: 0.0 builds 1
    # cash lineup, 1.0 builds the full 50-lineup GPP batch, scaling
    # in between. This is the main GPP workflow.
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --risk-level 1.0

    # override the auto-derived count explicitly if you want a specific number
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --risk-level 0.75 --num-lineups 10

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
        "--risk-levels",
        default=None,
        help="Comma-separated risk levels 0.0-1.0, one lineup each, e.g. '0,0.5,1'. "
        "Defaults to a 5-point cash-to-GPP spread. Ignored if --risk-level is set.",
    )
    parser.add_argument(
        "--risk-level",
        type=float,
        default=None,
        help="A single point on the cash-to-GPP slider (0.0=cash, 1.0=max GPP). This now also "
        f"drives lineup count directly: 0.0 builds 1 lineup, 1.0 builds {MAX_LINEUPS} (scaling "
        "linearly between), unless --num-lineups explicitly overrides it.",
    )
    parser.add_argument(
        "--num-lineups",
        type=int,
        default=None,
        help=f"Explicit override for how many lineups to build at --risk-level. Capped at {MAX_LINEUPS}. "
        "Leave unset to let the risk-level slider determine the count automatically.",
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
    args = parser.parse_args()

    if args.num_lineups is not None and args.num_lineups > MAX_LINEUPS:
        print(f"--num-lineups capped at {MAX_LINEUPS} (requested {args.num_lineups})")

    data_source = NflverseDataSource(cache_dir=args.cache_dir)
    pipeline = DfsPipeline(data_source)

    risk_levels = None
    if args.risk_levels and args.risk_level is None:
        levels = [float(x.strip()) for x in args.risk_levels.split(",")]
        risk_levels = {lvl: label_for_risk(lvl) for lvl in levels}

    pipeline.run(
        season=args.season,
        salary_csv_path=args.salary_csv,
        output_path=args.output,
        risk_levels=risk_levels,
        single_risk_level=args.risk_level,
        num_lineups=args.num_lineups,
        randomness=args.randomness,
        skip_redzone=args.skip_redzone,
        max_player_salary=args.max_player_salary,
        max_salary_leftover=args.max_salary_leftover,
    )

    print(f"Done. Wrote player values to {args.output}")


if __name__ == "__main__":
    main()
