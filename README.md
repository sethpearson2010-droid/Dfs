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

**A real gotcha worth knowing**: GitHub's workflow-dispatch form
**remembers the last value you typed into each field across separate
runs** — it does not reset to the YAML-defined default automatically.
If you test a small `num_lineups` value once, it'll silently stay in
that field on your next run even with a completely different
`risk_scale`. This caused real confusion (a "successful" run that only
built 4 lineups despite a high risk setting). `main.py` now prints a
`::warning::` GitHub Actions annotation (shows up highlighted in the
run's log) whenever an explicit `num_lineups` is suspiciously small
relative to what `risk_level` alone would derive — but the safest habit
is to explicitly clear/re-check every field before each run rather
than assume it reset.

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

**Now actually influences lineup construction, not just an
informational panel**: `is_sleeper` (and `is_regression_candidate`,
below) are set directly on each player by `pipeline.py` after
detection runs, and `lineup_builder.py`'s objective gives either flag
an 8% boost (`SLEEPER_BONUS_WEIGHT`) — not risk-scaled, since good
value is good value in cash as much as GPP. Verified this actually
moves the selection score (direct test: +8.0% exactly as designed).
Whether a specific sleeper ends up in a real lineup still depends on
whether it's competitive even with that boost — a 5-6 point bench
player usually won't out-score a real starter's ceiling even boosted,
and that's correct: the bonus nudges toward already-good options, it
doesn't override real point differences. Lineup slots in `lineups.json`
now carry `is_sleeper`/`is_regression_candidate` too, shown with 💤/📈
badges directly in the dashboard's lineup panel, not just the main
player table.

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

## Lineup builder (risk level and lineup count are independent)

`nfl_dfs.main` writes `output/lineups.json` in one of two modes:

**Normal mode** (default): pick a risk level (`--risk-scale`, 1-10,
default 5) and a lineup count (`--num-lineups`, default 1) —
**completely independent settings**. Every lineup in the batch is
built at *exactly* the risk level you picked; the count has zero
effect on risk, and the risk level has zero effect on count.
`--risk-scale 5 --num-lineups 50` builds 50 lineups that are all at
risk level 5 — not a spread across levels, not an auto-scaled count.

This used to work differently — a single "slider" input controlled
both risk *and* lineup count together (dragging toward max GPP
auto-built up to 50 lineups; dragging toward cash auto-built just 1),
and leaving that input blank silently fell back to a 5-different-
risk-levels exploration mode. That coupling caused real confusion (a
"successful" run building far fewer lineups than expected because the
risk value picked implied a smaller auto-derived count) and made the
whole system harder to reason about. Risk level and count are now two
plain, independent parameters, with no auto-derivation between them.

**Explore mode** (`--explore`, explicit opt-in only): one legal
lineup at each of 5 preset risk levels (cash through max GPP) — see
table below. Good for a quick look at how the model's picks shift
across the risk spectrum. This is no longer the default fallback;
you have to ask for it.

Diversity across a batch is enforced by rejecting any candidate that
shares more than 6 of 9 players with an already-accepted lineup — if
your player pool is thin (as in `test_data/full_slate.csv`'s 49
players — a real slate has hundreds) or further constrained by
`--max-player-salary`, you may get fewer than requested rather than
near-duplicate lineups; the output is honest about the shortfall
rather than padding it. Verified: without a salary cap, a real
735-player slate reliably returns the full requested count (tested at
50/50); with a tight `--max-player-salary 6000`, the same request
returns fewer (11-13 typically) — a real diversity-vs.-constrained-pool
limit, not a bug (see "Salary constraints" below for the full
investigation).

| risk_level | style | optimizes toward |
|---|---|---|
| 0.0 | cash | each player's `floor_projection` |
| 0.25 | safe GPP | mostly floor, some ceiling |
| 0.5 | balanced | even blend |
| 0.75 | risky GPP | mostly ceiling |
| 1.0 | max upside | each player's `ceiling_projection`, plus ownership leverage + stacking (below) |

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

### Smash factor

Every player row carries `smash_score` (ceiling ÷ $1000 salary — the
same convention as `value_score`, but using upside instead of the
mean) and `smash_alignment` (0-4: how many of vulnerability, game
script, pace, and the opportunity ceiling adjustment are individually
pointing up for this player, each exposed as its own multiplier in
`signal_multipliers`). A high smash_score with high alignment is a
player where every signal agrees, not just one metric happening to be
favorable — the dashboard shows this as a star rating (★☆☆☆ to
★★★★). This doesn't change the lineup-building objective by itself;
it's a transparency/diagnostic layer so you can see *why* a pick
looks good, not just that it does.

### Stacking

At `risk_level > 0`, the local-search phase of lineup construction
rewards rostering one of the QB's own pass-catchers (`STACK_BONUS_PER_PLAYER`,
scaled by risk_level) and, on top of that, a smaller bonus for also
rostering a player from the QB's opponent — a full "game stack" /
bring-back (`BRING_BACK_BONUS`). The reasoning: a QB's passing TD and
his receiver's receiving TD are the *same play* — correlated players
raise a lineup's ceiling (the good games get better together) even
though they don't raise its average, which is exactly the tradeoff GPP
wants and cash doesn't. That's why the bonus is zero at `risk_level=0`
— correlation is irrelevant-to-mildly-harmful for cash's pure
expected-value optimization. Verified: a strengthened bonus (bumped
from an initial value that was too weak to actually influence
construction) produced real double-stacks (QB + 2 teammates) and full
game stacks with bring-back in test batches. Each lineup's `slots`
output includes `stack_players` and `bring_back_players` so you can
see exactly what's correlated, not just trust a black-box score;
cash-tier lineups always report these as empty (see the code comment
in `_to_lineup_model` — at risk_level=0 the bonus never applied, so
any coincidental same-team roster overlap isn't a deliberate stack and
shouldn't be labeled as one).

### Randomness

`build_many()`'s per-candidate noise (needed for genuine diversity
across a batch — see below) scales with `risk_level` by design: cash
batches stay close to "the" single best lineup, GPP batches
differentiate much more. `--randomness` (default 1.0) is an additional
multiplier on top of that scaling — pass `>1` for wilder, more
contrarian batches, `<1` for tighter ones closer to the model's single
best pick at that risk level, or `0` to disable noise-driven diversity
entirely (only genuinely different local-search optima, if any, would
then differ between lineups).

**Why greedy + local search, not an exact solver**: getting a
provably-optimal lineup needs a real ILP solver (PuLP/mip), which is a
dependency this project deliberately avoids (see `requirements.txt`).
The greedy-fill-then-swap-improve approach reliably finds a strong
lineup and is transparent/debuggable, at the cost of not guaranteeing
the mathematical optimum. Stacking bonuses and ownership leverage only
affect the local-search phase (not the initial greedy fill, which
scores players independently) — in practice this is enough, since
local search runs thousands of swap trials and reliably finds and
locks in a stack once it's worth more than the alternative.
`build_many()` also uses fewer local-search iterations per candidate
than a single-lineup build — quantity of diverse attempts matters more
than per-candidate perfection when generating many lineups, and this
keeps a 50-lineup request from taking an unreasonable amount of time
(verified: a handful of seconds against the 49-player test slate,
including diversity rejection-sampling).

Verified against `test_data/full_slate.csv` (49 real 2025 nflverse
players): all 5 exploration-mode risk levels produced legal lineups
with genuinely different rosters (cash: Stafford/Kittle/McCaffrey;
max upside: Lawrence/Pitts/Bijan Robinson), slider-driven GPP mode at
`risk_level=1.0` correctly attempted the full 50-lineup batch and
honestly reported a smaller real count once the thin test pool's
diversity limit was reached, and stacking produced genuine correlated
rosters (QB + 2 teammates; full game stacks with bring-back) once the
bonus was strong enough to matter.

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

**Feeds into lineup construction, scaled by risk level — a flat bonus
here was verified to be too weak to matter.** Positive TD regression
is fundamentally a mean-reversion/variance bet (betting a player's TD
rate normalizes upward), which is exactly the kind of correlated
volatility a GPP lineup wants and a cash lineup doesn't — so unlike
the sleeper bonus (see "Sleeper picks" below, which stays flat since
sleeper value is genuinely cash-relevant too), this one scales with
`risk_level`: a no-op at cash, reaching `REGRESSION_BONUS_WEIGHT_AT_MAX_GPP`
(45%) only at risk_level=1.0 (risk scale 10). A first version used the
same flat 8% as the sleeper bonus and was confirmed too weak to ever
change a selection — 9 real regression candidates identified, 0
appearances across a 20-lineup max-GPP batch. Re-verified after
switching to the risk-scaled version: 7 of 20 lineups in the same
scenario, and correctly back to 0 at cash (risk scale 1), where the
bonus is a deliberate no-op.

## Minimum-salary flyers (distinguishing a real dart throw from a scrub)

Real case that motivated this: a $4,000 WR (Darius Cooper) got
selected with **zero signal on every metric** — `smash_alignment` 0/4
(every matchup multiplier at or below neutral), zero red-zone
involvement, a literal 0-point real game, and his own auto-generated
explanation said outright "no single standout signal, just a solid
baseline play." He wasn't a deliberate pick — the optimizer needed
*something* cheap to make the salary math work, and an
undifferentiated bottom of the salary barrel gave it nothing to
prefer one totally unremarkable option over another.

`flyers.py` fixes this by flagging genuinely cheap players
(`MAX_SALARY_FOR_FLYER`, $5,000) whose underlying opportunity — target
share, WOPR, red-zone share — clears a real, **absolute** minimum bar
(`MIN_TARGET_SHARE` 0.12, `MIN_WOPR` 0.18, `MIN_REDZONE_SHARE` 0.15;
any one clearing qualifies), same conservative philosophy as
`regression.py`: not just "relatively better than other equally-thin
cheap players," since if the whole bottom of the barrel has equally
thin usage, nobody there deserves the label just for being the
least-bad of a bad bunch. Verified against the real case: Darius
Cooper correctly does **not** qualify, while real candidates in the
same pool — Denzel Boston (WOPR 0.623, real target share), Elic
Ayomanor (WOPR 0.511) — were correctly identified.

The reasoning this targets specifically: recent-scoring-based
projections structurally can't detect an about-to-break-out player —
a rookie stepping into a bigger role, a practice-squad promotion,
someone getting real targets the ball hasn't bounced their way on
yet. Their recent POINTS won't show it, since points are downstream of
opportunity that hasn't converted into production. Opportunity metrics
(already computed in `advanced_stats.py` for other purposes) are a
leading indicator that doesn't have that lag — which is why a flagged
flyer gets a **ceiling boost specifically** (`FLYER_CEILING_BOOST`,
30%), not a floor boost: the whole point is that the upside is real
even though the points haven't caught up yet, not that the median
outcome has improved. Player rows carry an `is_flyer` boolean, the
dashboard shows a dedicated 🚀 panel plus badge, and `flyers.json` is
a new output file alongside the existing sleeper/regression ones.

**Reported immediately after shipping: "they aren't worked into
lineups even at risk 10."** Investigating this surfaced two real,
separate, more fundamental bugs in the core projection model, plus
led to a genuine "the percentage bonus alone can't work" finding that
needed a different kind of fix entirely.

**Bug 1 — matchup multipliers were stacking multiplicatively, not
additively.** `_project` computed `projection = base * vuln_mult *
script_mult * pace_mult` — three sequential multiplications. Three
individually-modest +30% effects multiply to +120% combined (1.3 ×
1.3 × 1.3 = 2.197), not the +90% you'd expect from summing three +30%
boosts. Confirmed directly: Bijan Robinson's raw *projection* (not
even ceiling) reached 47.2 — an unrealistic median expectation for
any RB. Fixed by combining the multiplier *effects* additively
(`1 + (vuln-1) + (script-1) + (pace-1)`) instead of multiplying the
raw multipliers — same result for the common case (one signal
notably favorable), only reduces the effect specifically when
multiple signals stack at once.

**Bug 2 — no cap on how far a single signal could swing a
projection.** Even after fixing the stacking, Derrick Henry's numbers
barely moved, because his inflation came from `vulnerability=1.645` —
a single signal, on its own, producing a genuine +64.5% swing (the
underlying differential really was extreme: an opponent defense
allowing 129% more than league-average points to RBs). Fixed with
`MULTIPLIER_SWING_CAP` (0.35) — no individual matchup signal can move
a projection more than 35% in either direction, however extreme the
real underlying differential.

**The deeper finding, after both fixes: still 0 flyer appearances
even in a 50-lineup max-GPP batch.** The remaining inflation traced to
a harder, more fundamental issue — this early in a season (2 real
weeks so far), one legitimately huge real game can dominate even a
*median*-based estimate with such a small sample, for ANY player who's
had one, not just elite ones. A flyer's whole premise is real
opportunity that *hasn't* converted into a big game *yet* — meaning
this dynamic systematically favors "already had one huge game" over
"has real opportunity but hasn't broken out," which is close to the
exact "chasing previous week high scorers" bias asked to be reduced a
few sessions ago. Even similarly-*priced* competition (not just $9k
studs) showed 2-3x higher ceilings than a genuine flyer's realistic
14-19 range, purely from this small-sample dynamic — closing that gap
with an ever-larger percentage bonus would stop being a real signal
and become an unprincipled hack.

**Fixed with a real minimum-exposure guarantee instead of a bigger
bonus**: `build_many` now locks the single best flyer per position
(by ceiling) into `FLYER_MIN_EXPOSURE_PCT` (15%) of a batch, but only
at real GPP risk levels (`FLYER_MIN_EXPOSURE_RISK_THRESHOLD`, 0.7) —
reusing the exact same lock machinery already built and tested for
force-included players, rather than a new mechanism. A low-risk/cash
batch isn't forced to gamble on unproven opportunity; a genuine GPP
batch is guaranteed real exposure to it. Verified across the full risk
spectrum on real data: 0 forced appearances below the threshold (risk
scale 1 and 5 still show a few organic appearances from the existing
bonus, just not guaranteed), jumping to 9 of 20 lineups (the top
flyer at each of 3 positions, each hitting its 15% target) at risk
scale 8 and 10. Full salary-cap and diversity re-verification across
risk scales 1/5/8/10 and both seasons: every one still returns the
full requested count with complete uniqueness and zero cap violations.

**The exact same fix, and the same 0-appearance problem, applied to
`regression.py`'s candidates too** — confirmed directly: 9 real
candidates identified, 0 appearances in a 20-lineup max-GPP batch,
even after the two multiplier bugs above were fixed. Added
`REGRESSION_MIN_EXPOSURE_RISK_THRESHOLD`/`REGRESSION_MIN_EXPOSURE_PCT`
(same 0.7/15% as flyers), reusing the identical lock mechanism.

**But regression and flyers share the same 3 eligible positions
(RB/WR/TE)** — running both guarantees independently caused a real,
separate problem: one lineup ended up with 6 of 9 slots locked (a
flyer *and* a regression candidate at each position simultaneously),
leaving too few free slots for salary-floor enforcement to work with
($6,700 left unused, and diversity dropped to 18/20 unique). Two
follow-up attempts at coordinating them both failed the same way for
different reasons: flat priority for one mechanism over the other
swept all 3 positions every time regardless of which was checked
first, and ranking combined candidates by raw ceiling just shifted
*which* mechanism swept, since flyers' ceiling-boost formula reliably
produces bigger absolute numbers than regression's points-adjustment
formula — neither comparison was really "which player is better," just
an artifact of which formula happens to output bigger numbers.

Settled on a fixed position split at the time — regression gets RB and
TE, flyers get WR — which had a real rationale beyond just resolving
the conflict: regression's signal (red-zone *touches* converting to
TDs) fits RB/TE's more touch-concentrated usage naturally, while
flyers' signal (target share/WOPR) fits WR's more target/route-based
profile. Verified working at the time: both signals getting real,
simultaneous representation once risk crossed into GPP territory.

**That fixed split had a real limitation of its own, reported as
"still not really including TD regression and low-salary flyers in
lineups"**: confirmed directly that a week can have regression
candidates show up at WR (not RB/TE at all) while flyers spread across
all 3 positions — the hardcoded split simply couldn't adapt to that,
so regression's WR candidates got zero guaranteed exposure regardless
of merit (4/20 real appearances that week, versus flyers' 18/20 from a
mix of the guarantee and raw competitiveness at positions the split
hadn't even reserved for them). Replaced with an adaptive version:
for each of the 3 shared positions, whichever mechanism has a real
candidate *there* gets the guarantee; only when both have one at the
exact same position does the assignment alternate (regression first),
so a real candidate from either signal always has a shot regardless of
which position it happens to occupy that particular week. Verified:
regression's WR candidate (previously locked out entirely) now
reliably wins its fair share of position conflicts (e.g. Woody Marks
winning the RB slot in one real test), while flyers still show up more
often overall in some weeks — confirmed this reflects real, honest
differences in candidate strength that week (flyers' ceiling-boost
formula can produce a bigger practical swing than regression's
points-adjustment for a similar-magnitude signal), not an unfair
mechanism, since flyer appearances beyond their guaranteed share come
from winning fairly through normal, non-guaranteed competitive
selection. Full lineup count, diversity, and salary-cap integrity
confirmed across the full risk spectrum and both seasons.

## Learning from real GPP-winning lineups (weekly maintenance)

`inputs/gpp_winners.json` stores real, manually-imported winning GPP
lineups. `nfl_dfs/gpp_winner_analysis.py` computes real, concrete
patterns from whatever's stored — salary distribution by roster slot,
FLEX position tendencies, QB salary tier, and stacking rate (a QB
paired with a same-team WR/TE, the classic GPP correlation play) —
regenerated automatically on every pipeline run into
`output/gpp_winner_analysis.json`, with a dashboard panel (🏆 Real GPP
Winner Patterns) showing both the aggregate numbers and a week-by-week
trend table.

**This is explicitly not a model that predicts winners** — it's a
small, growing dataset of real outcomes, and every statistic is
reported with its sample size attached rather than treated as a
precise target. `analyze()` needs at least 3 imported weeks before
calling anything a real trend rather than noise — below that, the
per-week list still shows (always useful), but the trend note says
plainly that it's too early to draw a conclusion either way.

### The weekly workflow

Each week, once that week's contest results are in:

1. **Paste the winning lineup** (or however many you have) to Claude —
   include, for each of the 9 players: name, team, position, salary,
   and points scored, plus the week/contest. Any note about *why* a
   player had a big or small game (an injury elsewhere, a blowout
   script) is useful context even though it isn't a separate field.
2. **Claude appends it** to `inputs/gpp_winners.json` in the existing
   shape and re-runs the pipeline, which regenerates the analysis
   automatically.
3. **Review what changed** — the per-week trend table makes it obvious
   whether a pattern (salary utilization, stacking, FLEX position,
   QB salary tier) is holding steady, drifting, or was a one-off. A
   pattern seen in literally every imported week so far is worth a
   real tuning change to a `lineup_builder.py` constant (with the
   before/after verified against real lineup output, the same
   standard as every other change in this project); a pattern that's
   inconsistent week to week is worth watching, not acting on yet.

Nothing about this is automatic tuning — every constant change is a
deliberate, reviewed decision each week, verified against real output
before and after, not a self-adjusting system drifting on noisy
few-data-point weekly signals without oversight.

### What the first two imported weeks showed, and what changed

Two real winners from the "NFL Sunday Million" were imported to start:

- **Both spent $59,900 of the $60,000 cap** — exactly $100 left over,
  in both cases. Far tighter than the $2,000 default this project had
  been using.
- **Both had a real same-team QB+WR stack** — Bryce Young + Jalen
  Coker (both CAR) in Week 1, Dak Prescott + CeeDee Lamb (both DAL) in
  Week 2. The Week 1 stack wasn't obvious from a manual read-through —
  the analysis code caught it. A 2/2 (100%) rate.
- **Directly validated the injury-replacement feature** (see above):
  the two players noted as having benefited from a teammate's injury —
  Dalton Schultz (Nico Collins out) and Rashod Bateman (Zay Flowers
  out) — are exactly the pattern that feature already targets.

Two real tuning changes made from these findings, both verified
against real data before and after:

1. **`DEFAULT_MAX_SALARY_LEFTOVER`** tightened from $2,000 to $500 —
   but confirmed directly that applying this uniformly caused a severe
   diversity collapse at pure cash (risk-scale 1: 9/20 lineups built,
   only 1 unique composition). Root cause understood via `git stash`:
   this collapse is actually a **pre-existing** characteristic of
   `risk_level=0.0` specifically (cash's pool of near-best floor
   options has much less spread between alternatives to begin with),
   confirmed present with the OLD $2,000 default too, not something
   these changes introduced. Fixed properly with `CASH_MAX_SALARY_LEFTOVER`
   (2000) and linear risk-scaling in `build_many` — genuine GPP builds
   get the tight $500 target the real data supports, cash builds keep
   the safer $2,000, and an explicit `--max-salary-leftover` override
   is still respected exactly as given, with no scaling applied.
2. **`STACK_BONUS_PER_PLAYER`/`BRING_BACK_BONUS`** raised (8.0/4.0 →
   20.0/8.0, in two verified steps) after confirming our own lineups
   were only producing a real stack 55% of the time (11/20) at max
   GPP, versus the real winners' 100%. Landed at ~80-85% in testing —
   a real, meaningful improvement without chasing an exact match to a
   2-lineup sample, which could easily look very different once more
   winners are imported.

Verified after both changes: full lineup count and complete diversity
maintained across risk scales 3/5/8/10 and both seasons, zero salary-
cap violations throughout.

## What's automated vs. manual

- **Automated**: nflverse stats/schedule pull + vulnerability scoring,
  via `.github/workflows/refresh-vulnerability.yml` (runs Tue/Fri on a
  schedule, or trigger manually from the Actions tab). Writes
  `output/vulnerability.json`.
- **Manual, weekly**: downloading FanDuel's salary CSV and running
  `nfl_dfs.main` locally — FanDuel has no public salary API, so this
  step can't be scheduled without scraping their site, which is more
  fragile and gray-area than a 10-second manual download.

## Guarding against non-starters and lineup repetition

Two real issues surfaced from actual use, fixed as follows:

**A backup QB (or any player) with one huge outlier game looking
falsely playable**: recent-form scoring used a plain mean over the
last 5 games. A backup who got mop-up duty in a Week 18 game teams
don't try hard in (this happened with real 2025 data: three sub-1-point
games plus one 28.86-point outlier) had that outlier drag his mean up
to a misleadingly playable ~7.8. Fixed by switching to the **median**
instead (`value.py`'s `_build_player_averages`) — a median only moves
if *multiple* recent games support the higher number, so a single
fluke game can't dominate a small sample the way a mean can.

**Follow-up bug from the same root cause, on ceiling this time**: even
after the median fix above, that same outlier game was still inflating
the player's *ceiling* — floor/ceiling are built from a standard
deviation around the point estimate, and one 28.86-point outlier in an
otherwise sub-1-point game log produces a population stdev of 12.17
(vs. a typical spread of well under 1). That's a huge, misleading
ceiling (18.24 in the real case) that made the exact same player look
like a legitimate GPP boom-or-bust play even though he was correctly
filtered out of cash lineups by the median fix. Fixed by switching the
spread calculation to **Median Absolute Deviation** (MAD, scaled by
the standard 1.4826 consistency constant) instead of population
stdev — the same "resist a single outlier" property as the median fix,
applied consistently to the spread as well as the center. Verified:
the real case's ceiling dropped from 18.24 to 1.5, correctly
unattractive across the entire risk spectrum, not just at cash.

**A related but distinct case: genuinely volatile small-sample players
(not a single-outlier artifact)**: a committee-role RB with a real
boom/bust game log — e.g. 3.0/4.4/0.1/11.4/0.1/6.7 across 6 games —
isn't the same problem as Trubisky's one-fluke-game case (MAD already
handles that correctly), but with only 5 recent games, MAD is itself a
somewhat unstable estimator, and one real boom game among a small
sample can still push the ceiling to 3x+ the point projection. Added
a straightforward sanity clamp: ceiling can't exceed
`CEILING_TO_PROJECTION_CAP` (3.0x) times the point projection,
regardless of the raw spread estimate. Modest by design (this isn't
claiming the underlying volatility is fake, just bounding how far a
noisy small-sample spread estimate can run) — verified reducing one
real case from 16.57 to 15.66. For genuinely borderline picks like
this where the model's call doesn't match your own read on a player,
the manual exclude button (below) is the more reliable fix than
chasing every individual edge case with a model tweak.

**A season-ending injury not being reflected**: a player who got hurt
in Week 10 and hasn't played since would still show a perfectly
reasonable-looking average computed from Weeks 1-10, since there's no
newer data to show they're out. Fixed with a **staleness gate**: any
player whose most recent recorded stat line is more than 2 weeks
behind the latest week in the dataset (`STALE_WEEK_THRESHOLD` in
`value.py`) gets zeroed out entirely and excluded from lineup
building, tagged `is_stale` in the output. Verified against real 2025
data: correctly caught Garrett Wilson (last played Week 10 of 18),
Jayden Daniels (Week 14), Tua Tagovailoa (Week 15) — 82 players
flagged across a full 735-player slate, all genuine.

**A backup QB who filled in for an injury, then reverted to the bench
once the real starter returned**: Davis Mills started 4 straight games
(Weeks 9-12) with legitimately strong production while Houston's real
starter was out, so his median-based projection stayed high — correct
given that real recent history, but wrong given his current actual
role once benched again. The staleness gate above doesn't catch this:
he *did* record a stat line recently (Week 18), just at a much smaller
share of the game. Added `fetch_snap_counts` (a new nflverse data
source, joined by *normalized name* rather than player_id — this file
uses PFR-style IDs, a different namespace than the GSIS IDs used
elsewhere) to check each QB's offense-snap-% in their single most
recent game — confirmed Mills' last game was 35% of snaps vs. 100% in
his real starts.

**This does NOT auto-exclude, only flags — a real false positive
caught it before shipping**: an early version zeroed out any QB below
a 50% recent-snap threshold, the same treatment as `is_stale`. Testing
it broadly immediately surfaced a dangerous case: Josh Allen — an
unambiguous elite starter — showed just 1% snaps in his most recent
game, because Buffalo had already clinched playoff seeding and pulled
him from a Week 18 game with nothing riding on it. That's structurally
identical, from snap-share data alone, to Davis Mills' real bench
case — there's no way to tell "genuinely lost the job" apart from
"coach rested him in a meaningless game" without real depth-chart or
news context this tool doesn't have. Auto-excluding a real starter
would be a far worse failure than occasionally missing a genuine
bench case, so `is_backup_qb` is now a visible warning badge (🪑,
same treatment as a Questionable/Doubtful injury tag) rather than a
hard exclusion — verified Josh Allen's projection stays fully intact
(23.46, unaffected) despite carrying the flag, while Davis Mills also
shows a real (not zeroed) projection with the same warning, so you can
judge for yourself and use the manual exclude button (below) when you
know for certain, rather than trust an automated call that's been
directly shown to misfire on real starters.

**The same top player appearing in nearly every lineup of a GPP
batch** (worst at TE, where real slates often have fewer viable
options than RB/WR): the overlap-based diversity check in
`build_many()` only looks at *total* shared players across a lineup's
9 slots, so a batch could pass that check while still repeating the
single best option at a thin position almost every time. Fixed with
an **exposure cap** (`DEFAULT_MAX_EXPOSURE_PCT`, default 50%) — once
a player hits their share of the batch, they're heavily (not
absolutely) discouraged from further lineups, forcing genuine
variation into cheaper alternatives. Verified: a 20-lineup batch that
previously repeated one TE went to a spread across 5 different TEs,
with the top one capped at exactly 10/20 as designed.

**Same problem, different cause, showed up worst at WR**: even with
the exposure cap above, WR fills up to 4 of 9 roster slots (WR1/WR2/
WR3/FLEX) — the *total*-overlap check (max 6 of 9 shared) doesn't
stop two lineups from sharing all 4 WRs, since that's only 4 of the 6
allowed shared slots. Confirmed on real data: consecutive lineups
were sharing 3-4 of 4 WRs. Fixed with a **per-position overlap cap**
(`DEFAULT_MAX_POSITION_OVERLAP`, default 2) — independent of the
total-overlap check, no two lineups can share more than 2 players at
the *same* position. Verified: WR overlap between consecutive lineups
dropped to 1-2, and a 20-lineup batch went from ~4-5 unique WRs used
total to 11, with real depth options (not just the top 2-3 "obvious"
picks) getting genuine playing time.

**Follow-up bug this introduced**: the per-position cap is checked
against *every* already-accepted lineup, so it gets combinatorially
harder to satisfy as a batch grows — a 50-lineup request on a healthy
735-player real slate (not a thin pool) only returned 44-37 lineups,
under-delivering the requested count even though there was no real
shortage of players. Fixed with **adaptive relaxation**: after a
stretch of consecutive rejected attempts (`RELAX_AFTER_REJECTIONS`)
with no new lineup accepted, the position cap loosens by 1 (up to a
ceiling), then resets to the strict default the moment a lineup is
successfully accepted again. This keeps position variety as the
default behavior for the easy majority of a batch while guaranteeing
the requested count is still honored once genuine diversity is
exhausted, rather than the constraint itself becoming the bottleneck.
Also caught and fixed a real bug while implementing this: the
exposure-cap usage counter was accidentally wired to update on
*rejected* attempts instead of *accepted* ones, which would have
silently broken the exposure cap entirely — fixed before it shipped.
Verified: a 50-lineup request on the real slate now reliably returns
all 50, with WR variety still strong (14 unique WRs used, average
overlap of 1.24 between consecutive lineups — the strict cap of 2
held for the large majority of the batch).

## What happens in Week 1 of a new season

The entire projection model is built on recent game history — with
zero games played yet in a season, every player's recent-form average
would compute from an empty list, a hard 0 across the board.
Confirmed directly: `--season 2026` currently 404s on every nflverse
endpoint (player stats, vulnerability, pace, red zone, snap counts),
since nflverse doesn't publish a season's file until there's at least
one game to put in it.

**Fixed for player-level projections specifically**: when the current
season has fewer than `RECENT_FORM_WINDOW` (5) weeks of data,
`pipeline.py`'s `_fetch_weekly_stats_with_carryover` pulls in each
player's last 5 games from the *previous* season, tagged with negative
week numbers so they sort strictly before the new season's real games.
Because the recent-form calculations always take the last 5 entries
chronologically, this phases out naturally as real games accumulate —
Week 0 (before the season starts) uses last season's tail outright,
Week 1 blends in 1 real game, and by Week 5+ it's entirely real
current-season data with no special-casing needed anywhere else.
Verified against the actual live 2026 season (genuinely zero games
played): projections came back sensible and correctly ranked (Bijan
Robinson, Ja'Marr Chase, Josh Allen at the top) instead of every
player showing 0.

**Deliberately NOT extended to vulnerability, pace, red zone, or
snap-share data** — those still use the current season only, and
gracefully degrade to empty (rather than crashing, which an earlier
version of this fix did) when it has no games yet. Roster turnover and
scheme changes between seasons make cross-season defense/pace
carryover a much shakier assumption than an individual player's own
recent scoring — there's no good substitute for real current-season
matchup and usage data, so this doesn't try to fake one. The honest
consequence: matchup-based signals (vulnerability multiplier, pace,
WOPR/red-zone opportunity, backup-QB snap-share detection) are
genuinely uninformative for the first few weeks of a season, and
lineups built then will be more dependent on last season's raw scoring
than an established roster would be later in the year.

**A real gap in that "deliberately not extended" list, reported as
"red zone targets appear to be season, not last week"**: target
share and WOPR (sourced from `weekly_stats`, which already gets
carryover) were correctly blending in prior-season data, but red-zone
touches specifically come from a separate play-by-play fetch
(`fetch_redzone_data`) that wasn't getting the same treatment. Early
in a season, "last `RECENT_FORM_WINDOW` real games" for red-zone
touches was just "however many real games have been played so far" —
indistinguishable from the season total precisely when the season is
young enough for that distinction to matter. Unlike defense
vulnerability and pace (genuinely excluded on purpose — team scheme
and roster turnover make cross-season carryover shakier for those),
red-zone touches are a property of the *individual player's own
role*, exactly like their scoring average — the same justification
for carrying that over applies here too. Fixed with
`_fetch_redzone_data_with_carryover`, mirroring the existing
weekly-stats carryover pattern (negative week numbers, phases out
naturally as real games accumulate). Verified directly: a player with
zero red-zone touches in the 2026 games so far but real 2025 usage
went from showing nothing to correctly reflecting last season's tail;
a player with data in both seasons showed a correctly blended,
properly-ordered sequence (prior-season games at negative weeks,
current-season games following).

**A real follow-on bug, reported as "why is Kimani Vidal / Tyrone
Tracy Jr. / Bam Knight popping on TD regression and lineups — most
haven't had a carry and reception"**: the transparency field added for
the clarity gap above (`real_games_in_touches_sample`) turned out to
expose a real, separate correctness bug once put to use. All three
players' entire red-zone-touch signal was 100% carried over from last
season — confirmed directly: Kimani Vidal showed 3.2 redzone
touches/game, zero of it from any real 2026 game, while his actual
2026 production so far was 0.0 points. `regression.py` was flagging
him as "due for positive TD regression" based entirely on that stale
volume — but the whole premise of that signal is *current*
opportunity that hasn't converted yet, not a role that may not even
exist anymore. The same underlying gap existed in three separate
places, since each computes its own read on red-zone opportunity: 
`regression.py`'s candidate detection, `flyers.py`'s "clears a real
bar" check, and — the most fundamental instance, since it touches
every player's ceiling directly, not just flagged candidates — 
`value.py`'s `_apply_opportunity_ceiling`. Fixed in all three by
skipping (or zeroing out) any red-zone-share contribution when
`real_games_in_touches_sample == 0`; target share and WOPR didn't need
the same guard, since those come from `weekly_stats`, which the
existing `is_stale` check already protects. Verified: all three
reported players dropped out of both the regression and flyer lists,
and interestingly, risk-scale 1 (cash) went from partial to full 20/20
diversity too — suggesting this bug had been inflating several
min-salary/thin-data players' apparent competitiveness beyond what
their real 2026 role actually supported.

**A real follow-on clarity gap, reported as "Tez Johnson showing 2
red zone targets but only had 1 target"**: the math itself checks out
(his `redzone_touches_per_game` of 2.0 is the correct average of his
5-game window — `[1, 3, 2, 3, 1]`), but nothing indicated that 4 of
those 5 games were carried over from *last* season, not this one — so
comparing "2.0 average" against "1 target this specific week" looked
like a discrepancy when it's actually just an average blending in
better prior games, working as designed. Fixed by adding
`real_games_in_touches_sample` to `AdvancedMetrics` (how many of the
up-to-5 games behind the average are real current-season games vs.
carryover) and surfacing it in the dashboard's tooltip for that
column, so this kind of comparison is clear rather than looking like
a bug.

**Two real, serious bugs found once this actually got used on a real
Week 2 slate**, both reported as "GPP lineups still including
non-starter QB and FLEX players":

1. **A sentinel-value collision silently broke staleness detection
   for exactly the players it exists to catch.** `_build_last_played_week`
   tracked each player's most recent week using a default/sentinel
   value of `-1` for "no data yet" — which collides with carryover's
   own use of negative week numbers. A player whose *entire* history
   is carryover (weeks -5 through -1) would never register in the
   dict at all: every comparison against the -1 sentinel fails, even
   for their most recent game at week -1, since -1 is not greater than
   -1. Confirmed directly: Marcus Mariota, J.J. McCarthy, and other
   players with **zero real 2026 games** were passing the staleness
   check and getting selected into GPP lineups. Fixed by using
   negative infinity as the sentinel instead, so any real or
   carryover week number correctly registers.
2. **A related boundary bug**: even with the sentinel fixed, a player
   whose own most recent data point is a carryover game (week ≤ 0)
   while *other* players already have real current-season games
   could still narrowly escape the generic gap-threshold check —
   e.g. `max_week_overall=1` (from other players' real Week 1) minus
   `last_played=-1` (this player's own carryover) computes a gap of
   exactly 2, not *greater than* 2, so it doesn't trigger. Fixed with
   an explicit rule: once real current-season games exist anywhere in
   the pool, a player with zero real appearances of their own is
   always stale, regardless of the exact gap size — "the season has
   started and this player has never shown up in it" is a stronger
   signal than the generic threshold captures. Verified: both fixes
   together correctly excluded every zero-real-data player tested
   (Mariota, McCarthy, and others) while leaving genuine Week 1
   starters (Burrow, Wentz, Caleb Williams) completely unaffected.

## Weighting matchups and regression over raw recent scoring

A stated preference: don't just chase what a player scored last
week — weight matchup quality (vulnerability/game script/pace) and
positive-regression probability more heavily instead. Two real
changes, plus two follow-on issues found and fixed while verifying
them:

**1. Matchup multiplier weights raised** (`value.py`):
`VULNERABILITY_WEIGHT` 0.35→0.50, `GAME_SCRIPT_WEIGHT` 0.25→0.35,
`PACE_WEIGHT` 0.15→0.22 — these were originally kept deliberately
modest specifically so recent-form scoring stayed dominant, which is
exactly the opposite of what's wanted here. A first attempt roughly
doubled these and produced an unrealistic 57-point ceiling for a
TE — the SAME combined multiplier that scales the point estimate also
scales the MAD-based floor/ceiling spread (see `scale` in
`_value_one`), so a larger multiplier compounds on the spread as well
as the projection, not just the projection alone. Settled on a more
measured ~1.4x increase instead of ~2x.

**2. Positive TD regression now moves the actual projection, not just
lineup selection**: previously `is_regression_candidate` only fed a
selection-time objective bonus in `lineup_builder.py` — real signal,
but invisible in the projection numbers themselves. Now
`pipeline.py` converts `regression_gap` (expected minus actual TDs
per game, already computed by `regression.py`) directly into points
using `REGRESSION_TD_POINT_VALUE` (6.0 — the real FanDuel value of a
rushing/receiving TD, correct here since regression.py scopes to
RB/WR/TE only) and adds it to projection/floor/ceiling. A player whose
recent scoring looks low only because their TDs haven't hit yet,
despite real volume that supports more, now shows a projection that
reflects the expected regression rather than their currently-unlucky
recent total.

**Follow-on issue found while re-verifying the known edge cases**:
the McBride case above, while not literally a bug (traced back to his
own real regression gap and genuine game-log volatility, not the
weight change itself — confirmed by the number barely moving between
two different weight settings), was still worth a second look;
decided the underlying volatility was real enough not to chase
further with another model change, especially having already tuned
`CEILING_TO_PROJECTION_CAP` carefully against other specific cases in
earlier sessions.

**Follow-on bug actually found and fixed**: stronger matchup weights
made objectively-best-matchup players converge more consistently
across noise-randomized candidates in a batch — confirmed directly
with instrumentation that 0% of attempts were failing as infeasible
(so the earlier `_cheapest_remaining_cost` fix held), but batches were
still topping out well short of the requested count purely on
diversity rejections (e.g. 7 of 20 at risk-scale 5). Root cause: the
existing stuck-batch relaxation only loosened the *per-position*
overlap cap, never the *total*-overlap cap, so total overlap became
the binding constraint once matchup convergence got strong enough.
Fixed by relaxing both together when a batch is stuck. Re-verified
across risk scales 1/3/5/7/10 and both seasons: every one now returns
the full requested count (20/20) with genuine full diversity (20/20
unique compositions each) — actually an improvement over the
pre-existing baseline, since risk scales 1 and 3 previously topped out
below 20 even before this session's changes.

## Injury flagging (real data, not just heuristics)

FanDuel's salary CSV already includes real `Injury Indicator` /
`Injury Details` columns (`O`, `Q`, `D`, `IR`, etc.) that were sitting
unused. Now parsed in `salary.py` and threaded through as
`injury_status`/`injury_details`:

- **`O` (Out), `IR`, `NFI`, `SUSP`, `PUP`** (`OUT_INJURY_STATUSES`):
  excluded from lineup building entirely, same treatment as a stale or
  unmatched player — tagged `is_out` in the output.
- **`Q` (Questionable), `D` (Doubtful)**: still eligible (these
  players often do play), but flagged with a visible dashboard badge
  so you can judge the risk yourself rather than the tool silently
  either including or excluding them.

This is a more direct, authoritative signal than the staleness gate
above — the staleness gate only knows about past games, while
FanDuel's own injury designation reflects this week's actual status.
Verified against real data: 17 players correctly excluded (IR,
Out) on one real slate.

### Automatic injury-replacement detection

Beyond excluding the injured player themselves, `pipeline.py` now
uses the same real injury data to automatically identify who benefits
from their absence — no need to manually `--include-players` a
backup every week the news breaks:

- **QB**: if a team's highest-salaried QB (FanDuel's own pricing is a
  reasonable proxy for "the starter") is marked Out/IR, the team's
  other rostered QB is automatically force-included — the same real
  lock mechanism `--include-players` uses, just triggered by real
  injury data instead of a manual name. Verified: correctly finds
  nothing when the only QBs on IR in a real slate are clear
  3rd-stringers, not presumptive starters — it only acts when the
  *actual* starter is out.
- **RB/WR/TE**: if a team's highest-salaried player at that position
  is marked Out/IR, the next-highest-salaried *healthy* teammate at
  the same position gets a floor/ceiling/projection boost
  (`INJURY_REPLACEMENT_BOOST_BY_POSITION` — 25% for RB, since a
  backup RB's role typically jumps the most of any position when the
  lead back is out; 15% WR, 12% TE, since those targets are usually
  already spread across more players). This is a salary-as-proxy-for-
  role heuristic, not a real depth-chart model — it can be wrong (a
  true committee with no single clear beneficiary), so it's a modest
  *boost* like the sleeper/regression bonuses, not a guarantee of
  selection, and tagged `injury_replacement_for` on the output so
  it's visible and distinguishable from an organically-earned
  projection. Verified against a real slate: correctly identified and
  boosted two real cases (a Saints and a Browns RB, each filling in
  for an Out teammate) with sensible, real player names — but neither
  ended up in an actual GPP lineup in that specific test, since their
  underlying baseline opportunity was still fairly low even boosted,
  same honest limitation the sleeper bonus has.

**A separate, pre-existing bug found while testing this**: one
boosted player showed floor (1.5) *higher* than ceiling (1.4) — traced
back to the player's numbers *before* any boost was even applied
(floor=1.33, ceiling=1.12 pre-boost), a real edge case for very
low-projection players where the opportunity multiplier and/or the
ceiling cap can combine to push ceiling below floor. Fixed with a
straightforward clamp in `value.py` (`ceiling_projection = max(ceiling_projection, floor_projection)`)
applied to every player, not just injury-boosted ones — verified zero
inversions across a full real player pool after the fix.

`injury_status`/`injury_details` are also included on every lineup
slot in `lineups.json` now, not just the main player table — a
Questionable/Doubtful player who made it into a built lineup shows a
🩹 badge right there in the dashboard's lineup panel, so you don't
have to cross-reference the player table separately to notice.

### Manual exclusion (`--exclude-players`)

FanDuel's injury designation doesn't always catch up to real-time
news, and sometimes you just want to override the model's judgment on
a specific player regardless of any injury tag. `--exclude-players`
takes a comma-separated list of names and excludes them from lineup
building entirely — completely independent of the automatic `is_out`
exclusion above:

```bash
python -m nfl_dfs.main --season 2026 --salary-csv salaries.csv --exclude-players "Mitchell Trubisky, Christian McCaffrey"
```

Matching is normalized and substring-tolerant (via the same
`normalize_name` used for the FanDuel-to-nflverse join), so a bare
last name is enough — `--exclude-players "Trubisky"` matches "Mitchell
Trubisky" without needing the full name or worrying about Jr./II
punctuation. Verified: excluded players are confirmed absent from
every lineup in a real multi-lineup batch, and matching works
correctly both with a full name and a last name alone.

Excluded players are tagged `manually_excluded: true` in
`players.json` for transparency (distinct from `is_out`, so you can
tell "the model auto-excluded this from injury data" apart from "I
manually removed this").

The GitHub Actions workflow exposes this as the `exclude_players`
input. **Implementation note**: this value commonly contains spaces
and commas ("Mitchell Trubisky, Christian McCaffrey"), so it's passed
to the pipeline as a separately-quoted argument rather than folded
into the same unquoted `$ARGS` string used for every other input
(which are all single tokens like numbers) — folding a multi-word
value into that pattern would break bash's word-splitting.

### Forcing a player back in (`--include-players`) — a real lock, not just "made eligible"

The flip side of exclusion: a backup who's about to start because the
real starter got hurt elsewhere doesn't show up that way in past box
scores — the model would otherwise zero them out via the staleness
gate (or `is_out`, if FanDuel's injury tag hasn't updated for the
*starter* who's now hurt). `--include-players` overrides both:

```bash
python -m nfl_dfs.main --season 2026 --salary-csv salaries.csv --include-players "Jayden Daniels"
```

Same normalized, substring-tolerant matching as `--exclude-players`.
If the player has real recent history, it's used normally once the
exclusion is bypassed. If they have **no** matched historical data at
all (a true unknown — someone who's barely played, suddenly thrust
into a starting role), there's nothing real to fall back on, so
`_league_avg_scoring_by_position` supplies a rough "typical player at
this position" baseline instead of leaving them at a hard 0 — verified:
a genuinely unmatched QB got an 8.65-point baseline instead of 0. This
baseline is explicitly not a real projection for that specific
player — it's the minimum needed to make them a real candidate.

**A real bug found and fixed getting this right**: the first version
only bypassed the zero-out — it made a forced player's projection
nonzero but left selection entirely up to the normal optimizer, same
as everyone else. Reported back as "force included players not
showing up in lineups", which was correct: if their now-real value
still didn't compete with better options, they simply lost, same as
any other player — not what "force include" should mean. Fixed with an
actual **lock**: `_greedy_fill` now assigns every `force_included`
player to a roster slot *first*, before any noise/objective-driven
selection touches the rest of the roster, and `_local_search` /
`_enforce_salary_floor` are told which slots are locked so they never
touch them afterward. Verified: force-including Jayden Daniels across
a 15-lineup batch put him in all 15, not just however many the
optimizer happened to prefer him in — while the other 8 slots per
lineup still showed real diversity (15 unique full compositions) and
the salary cap held throughout.

**A second bug found while testing that fix**: locking correctly
worked for a player with real matched history, but a genuinely
unmatched player (using the position-average fallback above) still
got silently dropped — `lineup_builder.py`'s own eligibility filter
independently excludes `unmatched` players, a check `value.py`'s
`force_included` bypass never touched. Fixed by letting
`force_included` clear that filter too. Verified: the previously-silent
unmatched case now locks in correctly (10/10 lineups) same as the
matched case.

**A player who can't fit anywhere** (you locked more players at one
position than there are eligible slots for it, or their salary alone
exceeds the whole $60,000 cap) is skipped rather than failing the
entire build — `_greedy_fill` tries the most position-specific slot
first (e.g. a locked RB tries RB1/RB2 before FLEX, so it doesn't
needlessly claim the FLEX slot another position might need), and
silently moves on if nothing fits. `force_included: true` is tagged in
the output regardless, so you can check whether a lock actually took.

**Partial lock counts for batches** (`--num-lineups` > 1): append
`:N` to a name — `--include-players "Jayden Daniels:20"` locks them
into 20 of a 50-lineup batch, not all 50, so a forced player doesn't
structurally occupy one roster slot across the *entire* batch when
you only wanted a partial guarantee. Omit the suffix to lock into
every lineup (the default). Once a player hits their target count,
the remaining lineups in the batch treat them as a normal — still
viable, just no longer guaranteed — player. On the dashboard, this is
a "Lock into how many lineups?" field next to the force-include
chips; leave it blank to lock into all. Verified: `Jayden Daniels:8`
on a 20-lineup batch put him in exactly 8, not 20.

**Debugging note**: both this and `--exclude-players` now print
exactly what was requested vs. what was actually matched to a real
player (`--include-players requested [...], matched: [...]`) — a
request that matches 0 real players looks identical to "did nothing"
from the outside otherwise. The GitHub Actions workflow also passes
both through environment variables (`EXCLUDE_PLAYERS`/`INCLUDE_PLAYERS`)
rather than interpolating `${{ }}` directly into the shell script —
GitHub's own recommended pattern for free-text inputs, safer around
quoting than the direct-interpolation form used originally.

The GitHub Actions workflow exposes this as `include_players`, quoted
separately from `$ARGS` for the same reason as `exclude_players`
above. On the dashboard, every player row has a ✅ button alongside
the 🚫 exclude button — tapping either queues that player and clears
them from the other list (a player can't be both queued for exclusion
and inclusion at once). The rebuild request sends both lists.

## Risk scale (1-10) and matchup-depth weighting

`--risk-scale` (1-10, 1=safest/cash, 10=riskiest/max GPP, default 5)
is a friendlier alternative to the raw `--risk-level` (0.0-1.0) —
converts internally via `risk_level = (scale-1)/9`. It is completely
independent of `--num-lineups` (default 1) — see "Lineup builder"
above for why that independence matters. The GitHub Actions workflow
exposes `risk_scale` and `num_lineups` as two separate primary inputs,
with the raw `risk_level` float kept available as an "advanced"
fallback.

**"Risky lineups draw from deeper picks based on matchups"**: added a
new `MATCHUP_DEPTH_WEIGHT` term to the lineup-building objective,
distinct from the existing ceiling-chase and ownership-leverage terms
— it specifically rewards a genuinely favorable matchup
(`vulnerability_multiplier > 1`, meaning the opponent is soft against
this position) more heavily as risk_level climbs. This pulls in
cheaper/less-obvious players whose case is "great matchup, not just
name recognition," scaling to zero at cash (risk_level=0) same as the
other risk-scaled bonuses.

## Salary constraints

**A real, serious bug found and fixed: lineups could actually exceed
the $60,000 cap.** Confirmed with a live screenshot showing a "Cash"
lineup at $62,000 (-$2,000 "left"). Root cause: in `_greedy_fill`,
when no candidate for a slot fit within the reserve-padded budget
(the safety margin held back for filling future slots), the fallback
was `affordable = candidates` — considering every remaining candidate
regardless of whether it fit the *actual* remaining budget at all,
not just the reserve margin. With a pool heavy on expensive proven
stars (exactly what a floor-optimized cash build gravitates toward),
this could pick a player that pushed the total over the real cap
outright — and nothing downstream ever corrected it, since local
search only chases a higher objective score and salary-floor
enforcement only pushes spend *up*, never down. Fixed: the fallback
now relaxes only the future-slots safety margin, never the actual
cap (`affordable = [p for p in candidates if p.salary <=
remaining_budget]`), and if truly nothing fits even that, the
candidate fails outright (returns `None`, the same "couldn't build a
legal lineup" signal used elsewhere) rather than picking something
over budget. Verified: 162 lineups built across 5 risk levels (50
each) with zero cap violations, versus a confirmed real violation
before the fix.

**A second, independent safety net was also added**: every lineup
now passes through a hard validation right before being returned —
total salary, exactly 9 slots filled, no duplicate player — regardless
of which code path built it. If it ever fails, that specific candidate
is discarded (with a loud `::error::` log) rather than served, so a
bug in any *other* untested path still can't result in an illegal
lineup reaching the dashboard. Confirmed this net never actually
fires post-fix (0 triggers across the same 162-lineup stress test) —
meaning the real fix works on its own; the net is insurance, not a
patch over an unfixed root cause.

**Another follow-on issue, reported as "leaving $4-5k in salary that
should be used"**: once flyers got a real minimum-exposure guarantee
(see "Minimum-salary flyers" below), lineups with a locked flyer
started leaving noticeably more unused salary than normal ones —
confirmed directly: $2,400-$4,200 unused on flyer-locked lineups vs.
under $2,200 (mostly under $1,200) without one. Root cause: strict
salary-floor enforcement (`_enforce_salary_floor`) was unconditionally
disabled for batches (see the diversity-collapse investigation
above), but that decision was made in the context of a *tight*
`--max-player-salary` making the pool of good expensive upgrades too
thin — a completely different scenario from "a locked flyer freed up
its own salary and the rest of the roster didn't absorb it." With no
per-player cap set (the common case), that collapse risk doesn't
apply, and `_enforce_salary_floor` already correctly skips locked
slots, so re-enabling it only touches the other 8 (unlocked) slots —
safe even with a flyer locked in. Fixed by making the disable
conditional on whether `max_player_salary` is actually set, rather
than always-off. Verified: flyer-locked lineups now leave $400-$1,500
(down from $2,400-$4,200), full lineup count and diversity holding
across risk scales 1/5/8/10 and both seasons, and the original tight-
`max_player_salary` protection re-confirmed still intact when that
flag *is* set.

**That fix had a real, serious side effect of its own, reported as
"risk 5 only generating 3 lineups" and "not seeing a lot of
variations"**: correctly refusing to overspend meant `_greedy_fill`
started returning "infeasible" (`None`) far more often than it should
have — confirmed directly with instrumentation: 1960 of 2000 attempts
(98%) were failing this way on a healthy, non-thin real pool (310
usable players, 69 RBs, 120 WRs — nowhere near a legitimate
"the pool is just this thin" case). Root cause: `_cheapest_remaining_cost`
(the reserve-budget estimate for slots not yet filled) summed the N
*cheapest players overall*, without checking whether those cheap
players were actually eligible for the *specific positions* the
remaining slots need. If cheap WRs dominate the "N cheapest" list
while a remaining slot actually needs a QB or DST (both of which
typically have a higher minimum salary than the cheapest skill-position
options), that generic estimate understates the true reserve needed —
letting greedy fill overspend on earlier slots, then run out of
affordable options once it reaches the position that actually needed
more room held back. Fixed by computing the cheapest *eligible* player
for each specific remaining slot instead of a generic N-cheapest
estimate. Verified: the exact reported case (risk scale 5, 20
lineups) went from 3 built (98% of attempts failing as infeasible) to
20/20 (0% failing as infeasible, all genuinely unique) — confirmed
with the same direct instrumentation before and after.

**The salary cap is $60,000, always** (`SALARY_CAP` in
`roster_rules.py`) — lineups now reach $54,800-$60,000 by default
(verified on real data). If they were landing around $45,000-$50,000
instead, that wasn't a wrong cap value — it was `--max-player-salary`
being set to $6,000 by default in an earlier version, from a genuine
miscommunication: a request for the standard "$60,000 salary cap" was
initially misread as a *per-player* $6,000 price limit. That default
has been removed — `--max-player-salary` is now unset (no per-player
cap) unless you explicitly opt into one for a punt-style build.

`--max-salary-leftover` (default **$2000**) still applies by default:
for a single lineup (`--num-lineups 1`), it actively pushes spend
close to the $60,000 cap via `_enforce_salary_floor` — a greedy pass
that upgrades players to more expensive same-slot alternatives
(preferring whichever upgrade costs the least objective, or gains the
most) until the target is hit or no upgrade is left that fits under
the cap. For a batch (`--num-lineups` > 1), spend efficiency comes
from a gentler always-on bias in `_objective()`
(`SALARY_UTILIZATION_WEIGHT`) instead — see the note below for why.

**If you do use `--max-player-salary`** (e.g. for a deliberate
punt/no-studs build), worth knowing what was found investigating it
under a tight value like $6,000 — this doesn't apply at all with no
cap set, which is the default: applying strict `--max-salary-leftover`
enforcement to every candidate in a multi-lineup batch collapsed a
20-lineup request under `--max-player-salary 6000` to **1** unique
lineup — confirmed by direct debugging that under a tight cap, the
pool of genuinely *good* expensive upgrade options is itself small
enough that any thorough salary-maximizing search funnels toward the
same few, regardless of starting point or tie-breaking randomness. A
real structural tension between "spend near the cap" and "stay
diverse" in a constrained pool, not a bug. Fixed by having
`build_many()` skip the strict pass for batches and rely on the softer
always-on bias instead — real diversity preserved (20/20 unique
lineups), at the cost of leftover being looser than the $2,000 target
when a tight `--max-player-salary` is also in play. With no
`--max-player-salary` set (the default), this tension doesn't arise —
verified: a 50-lineup batch at a fixed risk level reliably returns all
50, with salaries reaching the full $54,800-$60,000 range.

## Excluding a player and rebuilding, directly from the dashboard

The dashboard is a static GitHub Pages site with no backend of its
own — "rebuild without this player" has to mean triggering a real new
GitHub Actions run from the browser, which is what this does, using
GitHub's REST API directly against your own repo.

**One-time setup**: generate a **classic** Personal Access Token
(github.com/settings/tokens → Generate new token (classic)) with the
**`repo`** and **`workflow`** scopes checked. Unlike the one-time
tokens used earlier in this project's setup, this one needs a longer
expiration since the dashboard reuses it for every rebuild — paste it
into the red "🚫 Exclude players & rebuild" panel at the top of the
page and tap Save. It's stored in `localStorage`, scoped to your
browser only — never sent anywhere except `api.github.com` requests
you trigger yourself. Tap "Clear" in that panel to remove it.

**Using it**: every player row in the main table has a small 🚫 button
— tapping it queues that player for exclusion (shown as a chip in the
panel, persisted across page reloads via `localStorage` too, so an
in-progress selection survives a refresh). Once you've queued
whichever players you want out — Kenny Gainwell showing up in most of
your GPP batch, say — tap "Rebuild without these players". This POSTs
to GitHub's `workflow_dispatch` API
(`/repos/{owner}/{repo}/actions/workflows/run-and-deploy.yml/dispatches`),
replaying the **exact same settings as the run currently on screen**
(risk scale, lineup count, salary constraints, randomness — all read
from `output/run_config.json`, a new file `pipeline.py` writes every
run specifically so this replay is possible) plus your new
exclusions **added to** any exclusions that run already had, not
replacing them.

The dashboard then polls the Actions API every 10 seconds for up to
~7 minutes and auto-reloads the page once the new run completes — you
don't have to manually check the Actions tab and refresh. Verified:
the request format matches GitHub's real API exactly (tested against
the live endpoint — confirmed a bad token correctly returns 401 with
a clear error, and a well-formed request produces the exact
`{ref, inputs}` body GitHub's `workflow_dispatch` API expects, with
every input name cross-checked against the workflow YAML's actual
declared inputs).

**Owner/repo are hardcoded** in `frontend/index.html`
(`GITHUB_OWNER`/`GITHUB_REPO`/`GITHUB_WORKFLOW_FILE` constants near
the top of the script) — if you ever fork or rename this repo, update
those three constants to match.

**Stale data after a rebuild — a real bug found and fixed**: GitHub
Pages sets `Cache-Control: max-age=600` on static files by default,
which means a `fetch()` shortly after a rebuild completes can return
the *previous* run's cached JSON instead of the fresh one — exactly
when freshness matters most. All data fetches now go through a
`freshFetch()` helper that appends a cache-busting timestamp query
string and sets `cache: "no-store"`, forcing both the browser and any
intermediate CDN to treat each load as a new request. If exclusions
still seem to not be taking effect after a rebuild, a hard refresh
(clear browser cache) rules out any remaining caching layer this
doesn't cover.

## Why each player was picked, and their recent game log

Every player in the main player list carries a short, plain-language
explanation and a recent game log, both baked into the JSON at build
time — `explanations.py` generates these once during the Python run,
since the dashboard is a static site with no backend to compose them
dynamically. Tap the ℹ️ button next to a player's name to expand a
detail row showing both; this lives in the player list specifically
(not the lineup panel above it) so the lineup view stays compact and
scannable, with the deeper detail one tap away when you want it.

**The explanation is deliberately rule-based, not a model call**:
every reason it states traces to a specific field already computed
elsewhere in the pipeline (a matchup multiplier, a sleeper/regression
flag, a stack, low projected ownership) — it can't claim something
the rest of the output doesn't also support. If nothing stands out,
it says so plainly ("no single standout signal, just a solid baseline
play") rather than reaching for a reason that isn't really there.
Example: *"Selected for a favorable matchup (this defense has given
up extra fantasy points to the position recently), and real target
share / red-zone opportunity beyond his raw scoring average.
Projected range: 10.7 (floor) to 31.4 (ceiling)."* Stack context
(who a player is stacked with) only applies within the lineup panel,
where the pairing is meaningful — the player list's explanation
omits it since a player isn't part of any particular lineup's stack
in that general view.

**The game log shows real current-season games only** — a first
version showed every recent game including carried-over ones from the
previous season (see "What happens in Week 1" above), labeled
generically as `LastYr` since the negative week value used internally
for sorting isn't a real week number. In practice this meant early in
a season (when most or all of the recent-form window is still
carryover data), the log was mostly or entirely `LastYr` entries —
cluttered and not the current season's story. Fixed: `format_game_log`
now filters to `week > 0` only, so the display shows either real
current-season weeks (`Wk14: 21.3, Wk15: 18.1, ...`) or, honestly, no
log at all yet if the season hasn't produced enough real games —
never a log padded with entries that don't have a real week to show.
The underlying projection calculation is unaffected either way; only
the display was filtered.

## Probability-flavored consistency rating

A request to "fine-tune based on probability" is a real, substantial
ask — but the existing floor/ceiling/projection model has been
through many carefully-verified fixes in this project (the median vs.
mean fix, the MAD-based spread fix, the ceiling cap, each validated
against specific real cases like a backup QB's outlier game or a
committee RB's genuine week-to-week swings). A wholesale replacement
of that model risked reintroducing bugs in all of those without time
to re-verify each one properly, so this starts with something purely
additive instead: `volatility_label` ("Consistent" / "Moderate
volatility" / "Boom/bust"), based on the coefficient of variation
(scaled spread ÷ point projection) — normalized so a $4,000 player and
a $9,000 player are compared on *relative* volatility, not raw point
range, and doesn't touch any existing floor/ceiling/projection number.
Shown as part of each player's explanation (tap the ℹ️ in the player
list) — e.g. *"...Projected range: 2.3 (floor) to 35.2 (ceiling).
Boom/bust week-to-week."*

This is a first step, not the full answer to "predict based on
probability." Worth discussing directly since the real options differ
a lot in scope and risk:
- **Empirical percentiles**: replace the MAD-multiplier floor/ceiling
  with actual 25th/75th/90th percentiles computed from each player's
  real recent games (already collected in `recent_game_log`) — more
  genuinely "probability-based" since it's real observed outcomes
  rather than a formula-derived spread, but touches the core numbers
  every other fix in this project has been built around, so it needs
  real time to re-verify against the known edge cases before shipping.
- **Explicit "probability of X+ points"** for a couple of useful
  thresholds (e.g. "68% chance of 15+"), fitted from the recent game
  log — additive like the volatility label, doesn't touch existing
  numbers, more work than the label but a natural next step from it.
- **Simulation-based lineup construction**: build lineups by sampling
  many possible outcomes (accounting for the correlation a stack is
  supposed to capture) rather than a single floor/ceiling blend — the
  most rigorous option, and also the biggest rewrite of the lineup
  builder's core logic.

## Exporting lineups

The lineup panel has two export buttons, both producing a CSV in
FanDuel's bulk-upload format (one column per roster slot — `QB,RB,RB,
WR,WR,WR,TE,FLEX,D` — each cell holding that player's FanDuel ID):

- **Export current lineup** — just the one shown at the current
  slider position
- **Export all lineups** — every lineup in the current batch, one per
  row, ready for FanDuel's "Upload Lineups from CSV" on a multi-entry
  contest's draft screen

This uses each player's real FanDuel ID (parsed from the salary CSV's
`Id` column, threaded through as `fanduel_id`), not just their name.
**FanDuel's exact expected format has drifted before** (community
tooling has hit this — see the linked GitHub issue in the dev notes)
— if a real upload is rejected, download a fresh template from
FanDuel's own CSV upload screen and compare headers before assuming
this tool's export is broken.

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
