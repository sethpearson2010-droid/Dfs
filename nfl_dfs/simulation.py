"""Correlated Monte Carlo layer and tournament-portfolio selection.

What the commercial 'sim' tools do, in stdlib Python:
  1. Treat each player's score as a distribution (lognormal, mean =
     projection, spread from the floor/ceiling), not a point.
  2. Correlate players within a game (QB with his own pass-catchers,
     small shared game-environment effect, defense against the opposing
     offense). The correlations are estimated from Weeks 1-7 of 2026:
     QB-own WR 0.24, QB-own TE 0.28, QB-own RB 0.13, WR-WR ~-0.05,
     opposing players ~0.0-0.08.
  3. Simulate a field of lineups (ownership-weighted, ~45% stacked) and
     take, per simulation, the score that would finish in the top 1%.
  4. Score a pool of candidate lineups in the same simulations and pick
     the n lineups that maximise the chance AT LEAST ONE finishes top 1%
     (greedy cover, with a small bonus for lineups that hit often).

Caveat: ownership here is the heuristic estimate in ownership.py, not
real crowd data, so the simulated field is only a rough stand-in.
"""
from __future__ import annotations

import math
import random
from collections import Counter

from nfl_dfs.models import Lineup, PlayerValue, Position

CORR_QB_WR = 0.24
CORR_QB_TE = 0.28
CORR_QB_RB = 0.13
CORR_WR_WR = -0.03
CORR_WR_TE = 0.03
CORR_RB_RB = -0.05
CORR_OPPOSING = 0.04
CORR_DST_OPP_OFFENSE = -0.15
CORR_DST_OPP_QB = -0.20
CORR_DST_OWN_OFFENSE = 0.08

FIELD_STACK_RATE = 0.45
FIELD_MIN_SALARY = 57000
SALARY_CAP = 60000
TOP_FRACTION = 0.01
SUPPORT_BONUS = 0.2  # per-hit bonus relative to a newly covered simulation


def marginal_params(projection: float, ceiling: float) -> tuple[float, float]:
    """Lognormal (mu, sigma) with mean = projection. The ceiling is
    treated as ~the 75th percentile (Weeks 4-7: ~25% of cheap players
    beat their ceiling), so sd ~ (ceiling - projection) / 0.7."""
    mean = max(0.3, projection)
    sd = max((ceiling - projection) / 0.7, 0.45 * mean)
    cv = min(1.8, sd / mean)
    sigma2 = math.log(1.0 + cv * cv)
    return math.log(mean) - sigma2 / 2.0, math.sqrt(sigma2)


def _pair_corr(a: PlayerValue, b: PlayerValue) -> float:
    same_team = a.team.upper() == b.team.upper()
    pa, pb = a.position, b.position
    if Position.DST in (pa, pb):
        dst, other = (a, b) if pa == Position.DST else (b, a)
        if other.position == Position.DST:
            return 0.0
        if other.team.upper() == dst.team.upper():
            return CORR_DST_OWN_OFFENSE
        return CORR_DST_OPP_QB if other.position == Position.QB else CORR_DST_OPP_OFFENSE
    if not same_team:
        return CORR_OPPOSING
    pair = frozenset((pa, pb))
    if pa == Position.QB or pb == Position.QB:
        other = pb if pa == Position.QB else pa
        return {Position.WR: CORR_QB_WR, Position.TE: CORR_QB_TE, Position.RB: CORR_QB_RB}.get(other, 0.0)
    if pa == pb == Position.WR:
        return CORR_WR_WR
    if pa == pb == Position.RB:
        return CORR_RB_RB
    if pair == frozenset((Position.WR, Position.TE)):
        return CORR_WR_TE
    return 0.0


def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
    """Cholesky factor; if the matrix isn't positive-definite, shrink
    the off-diagonals toward 0 until it is."""
    n = len(matrix)
    shrink = 1.0
    for _ in range(25):
        a = [[matrix[i][j] * (1.0 if i == j else shrink) for j in range(n)] for i in range(n)]
        low = [[0.0] * n for _ in range(n)]
        ok = True
        for i in range(n):
            for j in range(i + 1):
                s = sum(low[i][k] * low[j][k] for k in range(j))
                if i == j:
                    d = a[i][i] - s
                    if d <= 1e-9:
                        ok = False
                        break
                    low[i][j] = math.sqrt(d)
                else:
                    low[i][j] = (a[i][j] - s) / low[j][j]
            if not ok:
                break
        if ok:
            return low
        shrink *= 0.85
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def simulate_outcomes(players: list[PlayerValue], sims: int, rng: random.Random) -> dict[str, list[float]]:
    """name -> list of `sims` simulated fantasy scores (correlated within
    each game, independent across games)."""
    groups: dict[frozenset, list[PlayerValue]] = {}
    for p in players:
        groups.setdefault(frozenset((p.team.upper(), p.opponent.upper())), []).append(p)
    out: dict[str, list[float]] = {}
    for group in groups.values():
        n = len(group)
        corr = [[1.0 if i == j else _pair_corr(group[i], group[j]) for j in range(n)] for i in range(n)]
        low = _cholesky(corr)
        params = [marginal_params(p.projection, p.ceiling_projection) for p in group]
        cols: list[list[float]] = [[] for _ in range(n)]
        for _ in range(sims):
            e = [rng.gauss(0.0, 1.0) for _ in range(n)]
            for i in range(n):
                z = sum(low[i][k] * e[k] for k in range(i + 1))
                mu, sigma = params[i]
                cols[i].append(math.exp(mu + sigma * z))
        for p, col in zip(group, cols):
            out[p.player_name] = col
    return out


def _weighted_choice(rng: random.Random, items: list[PlayerValue], weights: list[float]) -> PlayerValue:
    return rng.choices(items, weights=weights, k=1)[0]


def _field_lineups(players: list[PlayerValue], size: int, rng: random.Random) -> list[list[str]]:
    pool = [p for p in players if p.projection > 0 and not p.is_out and p.salary > 0]
    by_pos = {pos: [p for p in pool if p.position == pos] for pos in (Position.QB, Position.RB, Position.WR, Position.TE, Position.DST)}
    w = lambda p: max(0.5, p.projected_ownership_pct)
    flex_pool = by_pos[Position.RB] + by_pos[Position.WR] + by_pos[Position.TE]
    lineups: list[list[str]] = []
    guard = 0
    while len(lineups) < size and guard < size * 60:
        guard += 1
        qb = _weighted_choice(rng, by_pos[Position.QB], [w(p) for p in by_pos[Position.QB]])
        chosen = [qb]
        names = {qb.player_name}

        def take(candidates):
            c = [p for p in candidates if p.player_name not in names]
            if not c:
                return None
            p = _weighted_choice(rng, c, [w(x) for x in c])
            names.add(p.player_name)
            chosen.append(p)
            return p

        wr_slots, te_slots, rb_slots = 3, 1, 2
        if rng.random() < FIELD_STACK_RATE:
            mates = [p for p in by_pos[Position.WR] + by_pos[Position.TE] if p.team.upper() == qb.team.upper()]
            m = take(mates)
            if m is not None:
                if m.position == Position.WR:
                    wr_slots -= 1
                else:
                    te_slots -= 1
                if rng.random() < 0.33:
                    backs = [p for p in by_pos[Position.WR] if p.team.upper() == qb.opponent.upper()]
                    b = take(backs)
                    if b is not None:
                        wr_slots -= 1
        for _ in range(rb_slots):
            take(by_pos[Position.RB])
        for _ in range(max(0, wr_slots)):
            take(by_pos[Position.WR])
        for _ in range(max(0, te_slots)):
            take(by_pos[Position.TE])
        take(flex_pool)
        take(by_pos[Position.DST])
        if len(chosen) != 9:
            continue
        salary = sum(p.salary for p in chosen)
        if FIELD_MIN_SALARY <= salary <= SALARY_CAP:
            lineups.append([p.player_name for p in chosen])
    return lineups


def _scores(outcomes: dict[str, list[float]], names: list[str]) -> list[float]:
    return list(map(sum, zip(*[outcomes[n] for n in names])))


def select_portfolio(
    players: list[PlayerValue],
    pool: list[Lineup],
    n_select: int,
    sims: int = 1500,
    field_size: int = 1500,
    seed: int = 7,
    max_exposure: float = 0.35,
    dst_max_exposure: float = 0.15,
) -> list[Lineup]:
    """Pick `n_select` lineups from `pool` that maximise the simulated
    chance that at least one finishes in the top 1% of a simulated field."""
    if len(pool) <= n_select:
        return list(pool)
    rng = random.Random(seed)
    live = [p for p in players if p.projection > 0 and not p.is_out]
    outcomes = simulate_outcomes(live, sims, rng)
    field = _field_lineups(live, field_size, rng)
    field_scores = [_scores(outcomes, f) for f in field]
    k = max(0, int(TOP_FRACTION * len(field_scores)) - 1)
    thresholds = [sorted(col, reverse=True)[k] for col in zip(*field_scores)]

    masks: list[int] = []
    hit_counts: list[int] = []
    for lineup in pool:
        names = [slot.player.player_name for slot in lineup.slots]
        sc = _scores(outcomes, names)
        bits = "".join("1" if a >= t else "0" for a, t in zip(sc, thresholds))
        m = int(bits, 2)
        masks.append(m)
        hit_counts.append(bits.count("1"))

    cap = max(1, math.ceil(max_exposure * n_select))
    dst_cap = max(1, math.ceil(dst_max_exposure * n_select))
    usage: Counter = Counter()
    covered = 0
    chosen: list[int] = []
    remaining = set(range(len(pool)))

    def allowed(i: int) -> bool:
        for slot in pool[i].slots:
            c = dst_cap if slot.player.position == Position.DST else cap
            if usage[slot.player.player_name] >= c:
                return False
        return True

    while len(chosen) < n_select and remaining:
        best, best_gain = None, -1.0
        for i in remaining:
            if not allowed(i):
                continue
            gain = (masks[i] & ~covered).bit_count() + SUPPORT_BONUS * hit_counts[i]
            if gain > best_gain:
                best, best_gain = i, gain
        if best is None:
            break
        chosen.append(best)
        remaining.discard(best)
        covered |= masks[best]
        for slot in pool[best].slots:
            usage[slot.player.player_name] += 1
    if len(chosen) < n_select:  # exposure caps blocked the rest: fill by hit rate
        for i in sorted(remaining, key=lambda i: -hit_counts[i])[: n_select - len(chosen)]:
            chosen.append(i)
            covered |= masks[i]

    p_any = covered.bit_count() / sims
    mean_hit = sum(hit_counts[i] for i in chosen) / len(chosen) / sims
    pool_hit = sum(hit_counts) / len(pool) / sims
    print(
        f"Simulation: {sims} sims, field {len(field)}; selected {len(chosen)} of {len(pool)} candidates | "
        f"P(at least one top-1%) {p_any:.0%} | avg per-lineup top-1% rate {mean_hit:.2%} (pool avg {pool_hit:.2%})"
    )
    return [pool[i] for i in chosen]
