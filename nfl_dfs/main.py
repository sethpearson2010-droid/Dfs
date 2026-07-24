"""
CLI entry point.

Usage:
    # 5-point cash-to-GPP exploration (default)
    python -m nfl_dfs.main --season 2026 --salary-csv path/to/fanduel.csv --output output/players.json

    # a single lineup at a chosen risk level
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --risk-level 0.75

    # up to 50 diverse lineups for GPP mass multi-entry
    python -m nfl_dfs.main --season 2026 --salary-csv fanduel.csv --risk-level 1.0 --num-lineups 20

The --salary-csv step is the one manual part of the pipeline (FanDuel
has no public salary API) — everything else (nflverse stats,
schedule, vulnerability, pace, red zone data) runs automatically.
"""

from __future__ import annotations

import argparse

from nfl_dfs.data.nflverse import NflverseDataSource
from nfl_dfs.lineup_builder import MAX_LINEUPS
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
        help="A single risk level (0.0=cash, 1.0=max GPP) to build one or more lineups at. "
        "Combine with --num-lineups for GPP mass multi-entry.",
    )
    parser.add_argument(
        "--num-lineups",
        type=int,
        default=1,
        help=f"Number of diverse lineups to build at --risk-level (default 1.0 if unset). Capped at {MAX_LINEUPS}.",
    )
    parser.add_argument(
        "--skip-redzone",
        action="store_true",
        help="Skip downloading play-by-play for red-zone metrics (faster; loses the WOPR/red-zone "
        "ceiling adjustment and the advanced_metrics in the output).",
    )
    args = parser.parse_args()

    if args.num_lineups > MAX_LINEUPS:
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
        skip_redzone=args.skip_redzone,
    )

    print(f"Done. Wrote player values to {args.output}")


if __name__ == "__main__":
    main()
