# NFL FanDuel DFS — Matchup Vulnerability & Value Tool

Free data pipeline: [nflverse](https://github.com/nflverse/nflverse-data)
(play-by-play-derived weekly stats + schedules, no API key) joined with
a manually-exported FanDuel salary CSV, to rank players by projected
value (projection ÷ salary).

## Why the code is split up this way

Each file is one responsibility, and they only talk to each other
through small, typed interfaces. That's deliberate — the two moving
parts most likely to change later are the data source (you may add a
paid feed) and the projection model (you'll want to improve on the
Phase 1 model), so those are isolated behind a `StatDataSource`
abstract base class and `ValueCalculator._project()` respectively.
Nothing else needs to change when either of those does.

| File | Responsibility | Pattern |
|---|---|---|
| `models.py` | Typed data structures shared everywhere | Plain dataclasses |
| `data/base.py` | Contract for any stat provider (weekly stats, schedule, Vegas lines, team plays, red zone data) | Strategy (abstract base class) |
| `data/nflverse.py` | Concrete nflverse implementation | Strategy implementation |
| `vulnerability.py` | Defense-vs-position scoring | Depends on `StatDataSource`, not nflverse directly (dependency injection) |
| `pace.py` | Team play-volume scoring, season + recent-form blend | Mirrors `vulnerability.py`'s shape |
| `advanced_stats.py` | Recent target share, air yards share, WOPR, red zone usage | Joins weekly stats + play-by-play by nflverse's own player_id |
| `name_matching.py` | Normalizes + fuzzy-matches FanDuel names to nflverse names | Exact-first, fuzzy fallback, reports unmatched rather than guessing |
| `salary.py` | Parses FanDuel's CSV export | Tolerant column-alias mapping (FanDuel's headers have shifted before) |
| `value.py` | Joins salary + vulnerability + game script + pace + advanced metrics + recent form → projection, floor, ceiling | See "Projection model" below |
| `ownership.py` | Heuristic projected ownership per player | Rank-based approximation — see its docstring for why this isn't real data |
| `sleepers.py` | Flags below-median-salary players with a meaningful statistical boost | Conservative, threshold-based; not just "high value_score" |
| `regression.py` | Flags players whose red-zone volume implies more TDs than they've recently scored | Positive-regression / buy-low signal, independent of salary or matchup |
| `roster_rules.py` | FanDuel classic-contest roster slots + salary cap | Constants, isolated so DraftKings/Underdog rules are a separate file later |
| `lineup_builder.py` | Builds one or many legal lineups at a chosen cash-to-GPP risk level | Greedy fill + local-search swap improvement; `build_many()` adds noise + overlap rejection for diversity |
| `pipeline.py` | Wires the above together | Facade |
| `main.py` | CLI entry point | — |
| `scripts/refresh_vulnerability.py` | Vulnerability-only refresh (no salary CSV needed) | Run by the scheduled GitHub Action |

## Running from a phone (no terminal)

`.github/workflows/run-and-deploy.yml` runs the whole pipeline in
GitHub's cloud and publishes the dashboard as a website — after setup,
using it from a phone is just: replace a file, tap a button, open a
link.

**One-time setup:**
1. Push this repo to GitHub.
2. **Settings → Pages** → set Source to **GitHub Actions**.

**Each week, from your phone's browser:**
1. Download the FanDuel CSV (may need "Request desktop site" for
   FanDuel's export button to show), then open `inputs/salary.csv` in
   GitHub's web editor and replace its contents with the new CSV —
   see `inputs/README.md` for exact steps. Commit to `main`.
2. **Actions tab → "Run pipeline and deploy dashboard" → Run
   workflow**, set season/risk level, run it. (The dropdown UI renders
   more reliably with desktop-site mode on.)
3. Once it finishes, open your Pages URL (shown on the Settings →
   Pages screen, like `https://<username>.github.io/<repo>/`) — the
   dashboard, lineups, and sleeper picks are all there.

This avoids Termux/local Python entirely; the tradeoff is a manual
CSV-paste step each week since FanDuel has no API GitHub Actions could
pull from automatically, same limitation as running locally.

## Running it locally

```bash
# 1. Get this week's FanDuel salary CSV: on FanDuel's lineup builder page,
#    click "Download CSV" and save it somewhere, e.g. salaries.csv

# 2. Run the full pipeline (5-point cash-to-GPP exploration, default)
python -m nfl_dfs.main --season 2026 --salary-csv salaries.csv --output output/players.json

# ...or build a single lineup at a chosen risk level
python -m nfl_dfs.main --season 2026 --salary-csv salaries.csv --risk-level 0.75

# ...or up to 50 diverse lineups for GPP mass multi-entry
python -m nfl_dfs.main --season 2026 --salary-csv salaries.csv --risk-level 1.0 --num-lineups 20

# 3. Open frontend/index.html in a browser (it reads ../output/players.json)
```

## Sleeper picks

`nfl_dfs.main` also writes `output/sleepers.json` — up to 3 picks per
position, each meeting all of:

1. priced at or below the median salary at their position (not chalk)
2. a recent-average baseline high enough to trust the role (filters
   out deep-bench players with one fluke stat line — thresholds are
   in `sleepers.py`'s `MIN_BASE_PROJECTION_BY_POSITION`)
3. at least an 8% combined lift from matchup vulnerability + game
   script + pace, over their own baseline — the *statistical case*,
   not just being cheap

Positions with fewer than 5 rostered candidates on the slate are
skipped entirely (a thin pool makes "below median" meaningless — this
matters most on small-slate days for K/DST). Each pick's `reasons`
list explains *why* — e.g. "opponent allows 28.1 pts/gm to RB
recently" — so you can sanity-check the call rather than trust a black
box. Player rows in `players.json` also carry an `is_sleeper` boolean
for cross-referencing, and the dashboard shows a dedicated panel plus
a 💤 badge on flagged rows.

## Advanced usage metrics

Every non-DST player in `players.json` carries an `advanced_metrics`
block:

- `target_share`, `air_yards_share`, `wopr` — nflverse-computed,
  recent-5-game average, pulled directly from `stats_player_week`
  (no extra fetch needed)
- `redzone_touches_per_game`, `redzone_share` — computed from
  play-by-play (`advanced_stats.py` + the streaming red-zone fetch in
  `nflverse.py`), joined to weekly stats by nflverse's own
  `player_id` (a GSIS ID) rather than by name — pbp uses abbreviated
  names like `T.McBride`, so name-matching a second time would just
  reintroduce the same fragility `name_matching.py` exists to avoid.

These deliberately do **not** shift the point projection — a
player's recent scoring average already implicitly reflects their
usage, so re-weighting the mean by the same usage data would double-
count it. Instead they adjust `ceiling_projection` only
(`OPPORTUNITY_CEILING_WEIGHT` in `value.py`): a player with real,
high-volume usage but recent scoring that lags behind (TD variance)
has more genuine ceiling than the raw average alone suggests. Skipped
entirely for QB/K, where target share and red zone touches aren't
meaningful signals.

`--skip-redzone` bypasses the play-by-play download (~19MB compressed
per season) for a faster run, at the cost of losing this ceiling
adjustment and the `advanced_metrics` block in the output.

## Projected ownership (heuristic, not real data)

**No free data source exists for actual crowd ownership** — that's
the % of entered lineups that rostered a player, and getting real
numbers requires a paid ownership-projection service or scraping
post-lock reports. `ownership.py` instead estimates ownership from a
player's rank by projection and by value score within their position
(chalk = obvious plays + great cost efficiency once the pool notices
it), mapped through a concentration curve so a few plays get high
"ownership" and the rest tail off — a plausible *shape*, not a
prediction. Treat `projected_ownership_pct` as directional at best; it
has no access to real public sentiment, beat-writer hype, or
last-minute news.

## Lineup builder (cash ↔ GPP risk slider, up to 50 lineups)

`nfl_dfs.main` writes `output/lineups.json` in one of two modes:

**Exploration mode** (default, no `--risk-level` passed): one legal
lineup at each of 5 points on the risk slider — see table below.

**Multi-lineup mode** (`--risk-level X --num-lineups N`): up to
`N` (capped at 50) diverse lineups at a single risk level, for GPP
mass multi-entry. Diversity is enforced by rejecting any candidate
that shares more than 6 of 9 players with an already-accepted lineup
— if your player pool is thin (as in `test_data/full_slate.csv`'s 49
players — a real slate has hundreds), you may get fewer than
requested rather than near-duplicate lineups; the output is honest
about the shortfall rather than padding it.

| risk_level | style | optimizes toward |
|---|---|---|
| 0.0 | cash | each player's `floor_projection` |
| 0.25 | safe GPP | mostly floor, some ceiling |
| 0.5 | balanced | even blend |
| 0.75 | risky GPP | mostly ceiling |
| 1.0 | max upside | each player's `ceiling_projection`, plus a leverage bonus that rewards lower `projected_ownership_pct` (scaled by risk_level — a no-op in cash, fully active at max GPP) |

Floor/ceiling come from each player's own recent-game standard
deviation (`value.py`), scaled by the same matchup/script/pace
multiplier as their point projection, then further adjusted by the
opportunity signal above. **DST is a special case**: nflverse has no
per-player row for a team defense, so there's nothing to join a DST
salary entry against. Rather than leave the FanDuel `D` slot with zero
legal candidates (which would break lineup building outright),
`value.py` gives DST a flat baseline adjusted by the opposing
offense's Vegas-implied total, with a wide fixed floor/ceiling spread
— this is a rough approximation, not real defensive-stat modeling
(sacks, takeaways, def TDs aren't in it).

**Why greedy + local search, not an exact solver**: getting a
provably-optimal lineup needs a real ILP solver (PuLP/mip), which is a
dependency this project deliberately avoids (see `requirements.txt`).
The greedy-fill-then-swap-improve approach reliably finds a strong
lineup and is transparent/debuggable, at the cost of not guaranteeing
the mathematical optimum. `build_many()` uses fewer local-search
iterations per candidate than a single-lineup build — quantity of
diverse attempts matters more than per-candidate perfection when
generating many lineups, and this keeps a 50-lineup request from
taking an unreasonable amount of time (verified: ~8s against the
49-player test slate, including the diversity rejection-sampling).

Verified against `test_data/full_slate.csv` (49 real 2025 nflverse
players): all 5 exploration-mode risk levels produced legal lineups
with genuinely different rosters (cash: Stafford/Kittle/McCaffrey;
max upside: Lawrence/Pitts/Bijan Robinson), and multi-lineup mode
produced correctly diverse, salary-legal lineups up to the pool's
real diversity limit.

## Positive TD regression (buy-low candidates)

`nfl_dfs.main` also writes `output/regression_candidates.json` — up to
3 players per RB/WR/TE, flagged when their recent red-zone touch
volume implies more touchdowns, at the league-wide rate for their
position, than they've actually scored recently:

```
expected_tds_per_game = league_td_rate_per_redzone_touch(position) × player's_recent_redzone_touches
regression_gap = expected_tds_per_game − player's_recent_avg_touchdowns
```

TDs are the highest-variance part of fantasy scoring — the same real
opportunity can produce 0 TDs one stretch and 2 the next purely from
randomness (a tipped pass, a goal-line fumble, a play call). A player
with real volume but a below-average TD rate has more expected
production than their box scores show, independent of this week's
salary, matchup, or ownership — a genuine "buy low" signal rather than
a favorable-matchup one.

Conservative by design: only counted for RB/WR/TE (QB rushing TDs and
K/DST don't fit this touch-driven model), requires at least 1.5
red-zone touches/game before trusting a player's own rate at all (a
rate from 2-3 touches over 5 games is mostly noise), and requires at
least a 0.15 expected-TD/game gap to flag — small, non-actionable gaps
are filtered out rather than padding the list. The league rate itself
is touch-weighted (total recent TDs ÷ total recent red-zone touches
across the position pool), not an average of individual player rates,
so a few high-volume players don't get drowned out by noisy low-volume
ones. Verified against real 2025 data: Christian McCaffrey (7.2 RZ
touches/game, elite volume) showed a small, sensible 0.2 TD/game gap;
lower-volume bench-tier players near the touch threshold showed larger
relative gaps, as expected from a smaller, noisier sample.

Player rows in `players.json` carry an `is_regression_candidate`
boolean, and the dashboard shows a dedicated panel plus a 📈 badge.

## What's automated vs. manual

- **Automated**: nflverse stats/schedule pull + vulnerability scoring,
  via `.github/workflows/refresh-vulnerability.yml` (runs Tue/Fri on a
  schedule, or trigger manually from the Actions tab). Writes
  `output/vulnerability.json`.
- **Manual, weekly**: downloading FanDuel's salary CSV and running
  `nfl_dfs.main` locally — FanDuel has no public salary API, so this
  step can't be scheduled without scraping their site, which is more
  fragile and gray-area than a 10-second manual download.

## Current limitations (Phase 1, by design)

- **Projection model combines four signals**: recent 5-game player
  average, adjusted by (1) opponent vulnerability, (2) game script —
  Vegas-implied team total from `spread_line`/`total_line` — (3) pace
  — the team's own recent offensive play volume (`attempts +
  sacks_suffered + carries` per game from
  `stats_team_week_{season}.csv`) — and (4), ceiling only, WOPR/red
  zone usage (see "Advanced usage metrics" above). No
  injury-status weighting yet.
- **Game context lookup is by (team, opponent) pair for the season**:
  if two teams meet twice in a season (rare, division rematches),
  it prefers the upcoming/unplayed game's line; if teams didn't play
  that season at all (can happen with bye weeks/scheduling), the
  multiplier is just skipped for that player rather than guessed.
- **Name matching**: nflverse has no FanDuel-specific player ID (their
  players.csv tracks gsis/pfr/espn/otc/pff IDs but not DFS-site IDs),
  so `name_matching.py` normalizes both sides (strips periods,
  apostrophes, Jr/Sr/II/III suffixes) and exact-matches on that; if
  nothing matches, it falls back to fuzzy string matching above an
  0.85 similarity cutoff. Every row in the output JSON carries a
  `name_match_quality` of `exact`, `fuzzy`, or `unmatched` — the
  dashboard flags anything that isn't `exact` so you can sanity-check
  it rather than silently trusting a fuzzy guess or missing player.
  Red-zone/WOPR data avoids this problem entirely by joining
  internally via nflverse's own `player_id` instead (see above).
- **Kicker/DST scoring is unreliable**: nflverse's `fantasy_points_ppr`
  doesn't cleanly capture kicking or team-defense scoring the way
  DraftKings/FanDuel compute it. Fine for QB/RB/WR/TE now; K/DST use
  the separate rough heuristic described above.
- **Projected ownership is a heuristic, not real data** — see its own
  section above.

## Data source confirmed working (as of this writing)

- `https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv`
- `https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv`
- `https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv`
- `https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz` (streamed, not fully loaded into memory — see `_stream_pbp_rows` in `nflverse.py`)
