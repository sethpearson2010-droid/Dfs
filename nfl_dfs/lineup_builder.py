"""
Builds a salary-cap-legal FanDuel lineup at a chosen point on the
cash-to-GPP risk slider.

risk_level=0.0 optimizes toward each player's floor_projection (safer,
consistent, cash-game style). risk_level=1.0 optimizes toward
ceiling_projection (boom-or-bust, GPP style). Anything in between
blends the two linearly. At higher risk levels, the objective also:

  - rewards lower projected_ownership (see ownership.py) — "leverage",
    a strong play few others have, matters far less in cash where you
    just want raw expected points regardless of chalk
  - rewards stacking: a QB plus one of their own pass-catchers scores
    together on the same play (the TD that boosts the QB's line is the
    same TD that boosts the receiver's), so correlated players raise a
    lineup's *ceiling* even though they don't raise its *average* —
    valuable for GPP variance, actively unhelpful for cash, so this
    bonus scales with risk_level same as leverage
  - uses more random noise per candidate lineup in build_many(), so a
    batch of GPP lineups is genuinely differentiated instead of nine
    near-copies of the single "best" lineup

Why not an ILP solver: getting an exactly-optimal lineup needs a
proper solver (PuLP/mip), which is a real dependency this project
deliberately avoids (see requirements.txt). Instead this uses a
standard two-phase heuristic:
  1. greedy fill, slot by slot, by objective-per-salary-dollar
  2. local search: repeatedly try swapping one rostered player for a
     better same-eligibility alternative, keep improvements, stop when
     no swap helps or the iteration budget runs out
This reliably finds a strong (if not always perfectly optimal)
lineup, and is transparent/debuggable in a way a black-box solver
wouldn't be. Stacking bonuses only affect the local-search phase (not
the initial greedy fill, which scores players independently) — that's
enough in practice, since local search runs thousands of swap trials
and will readily find and lock in a stack once it's worth more than
the alternative.

build_many() generates multiple lineups for GPP mass multi-entry (up
to MAX_LINEUPS), using per-lineup random noise on the objective plus
an overlap cap to force genuine diversity — without noise/rejection,
independent runs of the same deterministic greedy+local-search would
converge on the same (or nearly the same) lineup every time.
"""

from __future__ import annotations

import random

from nfl_dfs.models import Lineup, LineupSlot, PlayerValue, Position
from nfl_dfs.roster_rules import ROSTER_SLOTS, SALARY_CAP

LOCAL_SEARCH_ITERATIONS = 3000
MAX_LINEUPS = 50

# how much lower ownership boosts the objective at max risk_level —
# scales linearly with risk_level, so it's a no-op in cash (risk=0)
# and fully active at max GPP (risk=1)
LEVERAGE_WEIGHT = 0.15

# how much a genuinely good matchup (vulnerability_multiplier > 1)
# boosts the objective, scaled by risk_level — the "risky lineups draw
# from deeper picks based on matchups" mechanic. Distinct from
# LEVERAGE_WEIGHT (ownership) and the ceiling-chase from risk_level
# itself: this specifically rewards matchup quality.
MATCHUP_DEPTH_WEIGHT = 0.20

# flat, not risk-scaled bonuses for players already flagged by
# sleepers.py / regression.py — see their own modules for the
# threshold logic that decides who qualifies. Kept modest so these
# nudge selection toward already-good options rather than overriding
# real projection differences.
SLEEPER_BONUS_WEIGHT = 0.08
REGRESSION_BONUS_WEIGHT = 0.08

# a gentle, always-on nudge (not risk-scaled, unlike leverage/stack)
# toward spending more of the salary cap — small enough that real
# projection differences still dominate player selection, but enough
# to break ties toward the pricier option and lean batches toward
# efficient spend without a separate deterministic pass.
SALARY_UTILIZATION_WEIGHT = 0.08

# how close two upgrade options need to be (in _enforce_salary_floor)
# to be treated as a tie and chosen between randomly rather than
# always picking the single best — this is what lets salary-floor
# enforcement run per-candidate in a batch without collapsing
# diversity to one lineup.
TIE_BREAK_TOLERANCE_FRACTION = 0.25
TIE_BREAK_TOLERANCE_FLOOR = 0.5

# stacking: a flat fantasy-point-equivalent bonus per pass-catcher
# rostered alongside the QB (same team), and a smaller "bring-back"
# bonus for also rostering a player from the QB's opponent in the same
# game (a full game stack) — both scale with risk_level, same reasoning
# as the ownership leverage bonus above.
STACK_BONUS_PER_PLAYER = 8.0
BRING_BACK_BONUS = 4.0
STACK_ELIGIBLE_POSITIONS = {Position.RB, Position.WR, Position.TE}

# diversity controls for build_many(): a new lineup must differ from
# every previously accepted one by more than (9 - DEFAULT_MAX_OVERLAP)
# players, found via random noise + rejection sampling. Each candidate
# needs far less local-search polish than a single standalone lineup —
# quantity of attempts matters more than per-candidate perfection here
# — so this uses a much smaller iteration count, and the total attempt
# budget is capped absolutely (not just per-lineup) so a thin player
# pool that can't hit the diversity target fails fast instead of
# grinding through tens of thousands of iterations.
DEFAULT_MAX_OVERLAP = 6

# the total-overlap check above under-constrains any position that
# fills multiple roster slots — WR fills up to 4 (WR1/WR2/WR3/FLEX),
# so two lineups could share every single WR and still pass a 6-of-9
# total check (4 shared WRs + 2 more shared anywhere else = 6). This
# caps how many players at the SAME position two lineups can share,
# independent of the total-overlap check — both must pass.
DEFAULT_MAX_POSITION_OVERLAP = 2

# default target: don't leave more than this much of the $60,000 cap
# unused. A lineup that leaves a lot of cap on the table is usually
# leaving real points on the table too — see _enforce_salary_floor.
DEFAULT_MAX_SALARY_LEFTOVER = 2000
BUILD_MANY_LOCAL_SEARCH_ITERATIONS = 250
MAX_ATTEMPTS_PER_LINEUP = 100
MAX_TOTAL_ATTEMPTS = 8000

# base noise magnitude for build_many()'s diversity; actual noise used
# is scaled by risk_level (cash batches stay close to "the" best
# lineup; GPP batches differentiate much more) and by the caller's
# randomness multiplier (default 1.0 — see build_many's docstring)
BASE_NOISE_MAGNITUDE = 0.10

# exposure cap: without this, the same clearly-best player at a thin
# position (this showed up worst for TE, where real slates often have
# far fewer viable options than RB/WR) gets rostered in nearly every
# lineup of a batch — technically "diverse" by the overlap check above
# (8 other players can still differ), but not diverse in any way that
# matters for real GPP portfolio construction. Once a player hits their
# share of the batch, they're heavily (not absolutely) discouraged from
# further lineups — heavily rather than hard-banned, so a thin pool can
# still complete a full batch by reusing them past the cap if there's
# truly no alternative.
DEFAULT_MAX_EXPOSURE_PCT = 0.5
EXPOSURE_PENALTY_FACTOR = 0.1


class LineupBuilder:
    def __init__(self, salary_cap: int = SALARY_CAP, seed: int | None = None) -> None:
        self._salary_cap = salary_cap
        self._random = random.Random(seed)

    @property
    def salary_cap(self) -> int:
        return self._salary_cap

    def build(
        self,
        players: list[PlayerValue],
        risk_level: float,
        player_noise: dict[str, float] | None = None,
        local_search_iterations: int = LOCAL_SEARCH_ITERATIONS,
        max_player_salary: int | None = None,
        max_salary_leftover: int | None = DEFAULT_MAX_SALARY_LEFTOVER,
    ) -> Lineup | None:
        """Returns None if no legal lineup can be built from the pool
        (e.g. missing a required position entirely, or the cheapest
        possible combination still exceeds the cap).

        `max_player_salary`, if set, excludes any player priced above
        it from consideration entirely — a "no studs" / punt-style
        constraint. `max_salary_leftover` (default $2000) pushes the
        lineup to actually spend close to the cap rather than leaving
        money unused — see `_enforce_salary_floor`'s docstring for why
        this is a best-effort greedy pass, not a hard guarantee."""
        risk_level = max(0.0, min(1.0, risk_level))
        usable = [
            p
            for p in players
            if p.name_match_quality != "unmatched"
            and not p.is_stale
            and not p.is_out
            and not p.manually_excluded
        ]
        if max_player_salary is not None:
            usable = [p for p in usable if p.salary <= max_player_salary]
        noise = player_noise or {}

        lineup = self._greedy_fill(usable, risk_level, noise)
        if lineup is None:
            return None

        lineup = self._local_search(lineup, usable, risk_level, noise, local_search_iterations)

        if max_salary_leftover is not None:
            lineup = self._enforce_salary_floor(lineup, usable, risk_level, noise, max_salary_leftover)

        return self._to_lineup_model(lineup, risk_level)

    def build_many(
        self,
        players: list[PlayerValue],
        risk_level: float,
        count: int,
        max_overlap: int = DEFAULT_MAX_OVERLAP,
        randomness: float = 1.0,
        max_exposure_pct: float = DEFAULT_MAX_EXPOSURE_PCT,
        max_position_overlap: int = DEFAULT_MAX_POSITION_OVERLAP,
        max_player_salary: int | None = None,
        max_salary_leftover: int | None = DEFAULT_MAX_SALARY_LEFTOVER,
    ) -> list[Lineup]:
        """Builds up to `count` (capped at MAX_LINEUPS) diverse
        lineups at one risk level. Each candidate lineup is built with
        fresh random noise on the objective; a candidate is only kept
        if it shares at most `max_overlap` players TOTAL with every
        lineup already accepted, AND at most `max_position_overlap`
        players at any single position (the total check alone
        under-constrains WR specifically, since it fills up to 4 of 9
        roster slots — two lineups could share every WR and still pass
        a 6-of-9 total check). Returns fewer than `count` if the pool
        is too small/thin to keep finding sufficiently different legal
        lineups within the attempt budget — this is reported, not
        silently padded with near-duplicates.

        `randomness` is a multiplier (default 1.0) on top of the
        risk-scaled base noise — pass >1 for more differentiated/wild
        batches, <1 for tighter ones closer to "the" optimal lineup at
        that risk level, 0 to disable noise-driven diversity entirely
        (in which case only genuinely different local-search optima,
        if any, will pass the overlap checks).

        `max_exposure_pct` caps how much of the batch any single player
        can appear in (default 50%) — see the module-level comment on
        DEFAULT_MAX_EXPOSURE_PCT for why this exists separately from
        the overlap checks.

        `max_salary_leftover` has no effect here on purpose (batches
        use the always-on `SALARY_UTILIZATION_WEIGHT` bias instead) —
        tested both a deterministic and a randomized-tie-break version
        of strict enforcement per candidate, and under a tight
        `--max-player-salary` the pool of genuinely good expensive
        upgrades is itself small enough that both still converged
        every candidate to the same final lineup regardless of
        starting point. That's a real structural tension between
        spending near the cap and staying diverse when the pool this
        constrained — not a bug to code around further."""
        count = max(1, min(count, MAX_LINEUPS))
        accepted: list[Lineup] = []
        usage_count: dict[str, int] = {}
        max_uses_per_player = max(1, round(max_exposure_pct * count))
        attempts = 0
        max_attempts = min(count * MAX_ATTEMPTS_PER_LINEUP, MAX_TOTAL_ATTEMPTS)
        seed_counter = 0

        # noise scales with risk_level: cash batches should stay close
        # to the single best lineup, GPP batches should differentiate
        # much more — 0.3 is a floor so risk_level=0 still gets a
        # little jitter (otherwise every "cash" attempt is identical
        # and build_many would only ever return 1 lineup regardless of
        # count requested)
        noise_magnitude = BASE_NOISE_MAGNITUDE * (0.3 + 0.7 * risk_level) * max(0.0, randomness)

        # adaptive relaxation: the per-position cap is a hard rejection
        # checked against EVERY already-accepted lineup, so it gets
        # combinatorially harder to satisfy as the batch grows — even
        # on a healthy, non-thin player pool, a strict cap can quietly
        # starve out the back half of a large batch and under-deliver
        # the requested count. Rather than let that happen silently,
        # loosen the position cap by 1 after a stretch of consecutive
        # rejected attempts with no new lineup accepted, up to a
        # ceiling — this keeps the WR/position-variety benefit as the
        # DEFAULT behavior while guaranteeing the count is still honored
        # once the pool's genuine diversity is exhausted rather than
        # the constraint itself being the bottleneck.
        current_position_overlap = max_position_overlap
        stall_ceiling = max_position_overlap + 7
        consecutive_rejections = 0
        RELAX_AFTER_REJECTIONS = 75

        while len(accepted) < count and attempts < max_attempts:
            attempts += 1
            seed_counter += 1
            rng = random.Random(seed_counter)
            noise = {p.player_name: 1 + rng.uniform(-noise_magnitude, noise_magnitude) for p in players}

            for p in players:
                if usage_count.get(p.player_name, 0) >= max_uses_per_player:
                    noise[p.player_name] = noise.get(p.player_name, 1.0) * EXPOSURE_PENALTY_FACTOR

            self._random = random.Random(seed_counter)
            candidate = self.build(
                players,
                risk_level,
                player_noise=noise,
                local_search_iterations=BUILD_MANY_LOCAL_SEARCH_ITERATIONS,
                max_player_salary=max_player_salary,
                # NOT max_salary_leftover here, even with randomized
                # tie-breaking: verified directly that under a tight
                # --max-player-salary, the pool of genuinely GOOD
                # expensive upgrade options is itself small enough that
                # the enforcement loop still funnels every candidate
                # toward the same final lineup regardless of starting
                # point or random tie-breaks — this is a real structural
                # tension (spend-near-cap vs. diversity), not a fixable
                # bug, once the constrained pool is this thin. The
                # stronger always-on SALARY_UTILIZATION_WEIGHT below
                # pushes spend up without that collapse.
                max_salary_leftover=None,
            )
            if candidate is None:
                continue

            if self._is_diverse_enough(candidate, accepted, max_overlap, current_position_overlap):
                accepted.append(candidate)
                consecutive_rejections = 0
                current_position_overlap = max_position_overlap  # reset to the strict default for the next lineup
                for slot in candidate.slots:
                    usage_count[slot.player.player_name] = usage_count.get(slot.player.player_name, 0) + 1
            else:
                consecutive_rejections += 1
                if (
                    consecutive_rejections >= RELAX_AFTER_REJECTIONS
                    and current_position_overlap < stall_ceiling
                ):
                    current_position_overlap += 1
                    consecutive_rejections = 0

        return accepted

    # ------------------------------------------------------------------

    def _objective(self, player: PlayerValue, risk_level: float, noise: dict[str, float]) -> float:
        base = (1 - risk_level) * player.floor_projection + risk_level * player.ceiling_projection

        if risk_level > 0 and player.projected_ownership_pct:
            # lower ownership (0-100%) raises the objective, scaled by
            # how far up the GPP end of the slider we are
            ownership_fraction = player.projected_ownership_pct / 100.0
            base *= 1 + LEVERAGE_WEIGHT * risk_level * (1 - ownership_fraction)

        # "risky lineups draw from deeper picks based on matchups":
        # reward a genuinely favorable matchup (vulnerability_multiplier
        # > 1 — the opponent is soft against this position) more heavily
        # as risk_level climbs. This is distinct from the ceiling-chase
        # and ownership-leverage terms above — it specifically pulls in
        # cheaper/less-obvious players whose case is "great matchup",
        # not just "high variance" or "low owned". A no-op at cash.
        if risk_level > 0 and player.vulnerability_multiplier > 1.0:
            base *= 1 + MATCHUP_DEPTH_WEIGHT * risk_level * (player.vulnerability_multiplier - 1.0)

        # sleeper.py and regression.py already do the harder analytical
        # work of deciding WHICH players qualify (see their own
        # threshold logic) — these bonuses make that identification
        # actually influence roster construction instead of being
        # purely an informational side-panel. Not risk-scaled: a
        # sleeper (cheap + real role + matchup boost) is good value in
        # cash too, not just GPP, and a regression candidate's
        # underlying volume is real regardless of risk appetite.
        if player.is_sleeper:
            base *= 1 + SLEEPER_BONUS_WEIGHT
        if player.is_regression_candidate:
            base *= 1 + REGRESSION_BONUS_WEIGHT

        # a small, uniform (not risk-scaled) nudge toward higher-salary
        # players, so lineups lean toward spending closer to the cap
        # organically during greedy fill + local search, rather than
        # needing a separate deterministic pass that would flatten
        # batch diversity — see _enforce_salary_floor's docstring for
        # why that pass is reserved for single-lineup builds only.
        base *= 1 + SALARY_UTILIZATION_WEIGHT * (player.salary / self._salary_cap)

        return base * noise.get(player.player_name, 1.0)

    def _is_diverse_enough(
        self,
        candidate: Lineup,
        existing: list[Lineup],
        max_overlap: int,
        max_position_overlap: int = DEFAULT_MAX_POSITION_OVERLAP,
    ) -> bool:
        candidate_names = {s.player.player_name for s in candidate.slots}
        candidate_by_pos: dict[Position, set[str]] = {}
        for s in candidate.slots:
            candidate_by_pos.setdefault(s.player.position, set()).add(s.player.player_name)

        for lineup in existing:
            existing_names = {s.player.player_name for s in lineup.slots}
            if len(candidate_names & existing_names) > max_overlap:
                return False

            existing_by_pos: dict[Position, set[str]] = {}
            for s in lineup.slots:
                existing_by_pos.setdefault(s.player.position, set()).add(s.player.player_name)

            for position, names in candidate_by_pos.items():
                if len(names & existing_by_pos.get(position, set())) > max_position_overlap:
                    return False

        return True

    def _greedy_fill(
        self, players: list[PlayerValue], risk_level: float, noise: dict[str, float]
    ) -> dict[str, PlayerValue] | None:
        # fill tightest-constrained slots first (fewest eligible
        # players) so scarce positions (QB, TE) aren't starved by FLEX
        # grabbing a good RB/WR before its own slot is filled.
        remaining_budget = self._salary_cap
        assigned: dict[str, PlayerValue] = {}
        used_players: set[str] = set()

        slot_order = sorted(
            ROSTER_SLOTS.items(),
            key=lambda item: len([p for p in players if p.position in item[1]]),
        )

        for slot_name, eligible_positions in slot_order:
            candidates = [
                p
                for p in players
                if p.position in eligible_positions and p.player_name not in used_players
            ]
            if not candidates:
                return None  # position missing entirely from the pool — can't build a lineup

            # reserve at least min-price-per-remaining-slot budget for
            # what's left, so an early greedy pick doesn't strand later
            # slots with no affordable options
            slots_left_after_this = len(slot_order) - (slot_order.index((slot_name, eligible_positions)) + 1)
            min_reserve = self._cheapest_remaining_cost(players, slot_order, slots_left_after_this, used_players)

            affordable = [p for p in candidates if p.salary <= remaining_budget - min_reserve]
            if not affordable:
                affordable = candidates  # fall back rather than fail outright; local search may still recover

            best = max(affordable, key=lambda p: self._objective(p, risk_level, noise) / max(p.salary, 1))
            assigned[slot_name] = best
            used_players.add(best.player_name)
            remaining_budget -= best.salary

        return assigned

    def _cheapest_remaining_cost(self, players, slot_order, slots_left_after_this, used_players) -> int:
        if slots_left_after_this <= 0:
            return 0
        # rough lower bound: cheapest available salary times slots left
        salaries = sorted(p.salary for p in players if p.player_name not in used_players)
        return sum(salaries[:slots_left_after_this]) if salaries else 0

    def _local_search(
        self,
        assigned: dict[str, PlayerValue],
        players: list[PlayerValue],
        risk_level: float,
        noise: dict[str, float],
        iterations: int,
    ) -> dict[str, PlayerValue]:
        current = dict(assigned)
        current_score = self._total_objective(current, risk_level, noise)

        for _ in range(iterations):
            slot_name = self._random.choice(list(current.keys()))
            eligible_positions = ROSTER_SLOTS[slot_name]
            used_players = {p.player_name for p in current.values()}

            candidates = [
                p
                for p in players
                if p.position in eligible_positions and p.player_name not in used_players
            ]
            if not candidates:
                continue

            challenger = self._random.choice(candidates)
            incumbent = current[slot_name]

            new_salary = self._total_salary(current) - incumbent.salary + challenger.salary
            if new_salary > self._salary_cap:
                continue

            trial = dict(current)
            trial[slot_name] = challenger
            trial_score = self._total_objective(trial, risk_level, noise)

            if trial_score > current_score:
                current = trial
                current_score = trial_score

        return current

    def _enforce_salary_floor(
        self,
        assigned: dict[str, PlayerValue],
        players: list[PlayerValue],
        risk_level: float,
        noise: dict[str, float],
        max_leftover: int,
        rng: random.Random | None = None,
    ) -> dict[str, PlayerValue]:
        """Upgrades players to more expensive same-slot alternatives
        until total salary reaches (cap - max_leftover). This is a
        best-effort pass, not a guarantee: if the pool genuinely can't
        support spending that close to the cap (e.g. combined with a
        low --max-player-salary), it stops once no further upgrade is
        possible rather than looping forever or breaking the cap.

        Picks RANDOMLY among near-tied upgrade options (within
        TIE_BREAK_TOLERANCE of the best available score), using the
        same per-candidate `rng` as everything else in this build —
        not always the single deterministic "best" swap. A first
        version always took the single best option regardless of
        noise, which converged nearly every candidate in a batch onto
        the same lineup (verified: collapsed a 20-lineup request to 1
        unique lineup). Random tie-breaking lets this run for every
        candidate in a multi-lineup batch without that collapse, while
        still reliably pushing spend toward the target."""
        rng = rng or self._random
        target_min_salary = self._salary_cap - max_leftover
        current = dict(assigned)

        while self._total_salary(current) < target_min_salary:
            used_players = {p.player_name for p in current.values()}
            scored_options: list[tuple[float, str, PlayerValue]] = []

            for slot_name, incumbent in current.items():
                eligible_positions = ROSTER_SLOTS[slot_name]
                candidates = [
                    p
                    for p in players
                    if p.position in eligible_positions
                    and p.player_name not in used_players
                    and p.salary > incumbent.salary
                ]
                for challenger in candidates:
                    new_salary = self._total_salary(current) - incumbent.salary + challenger.salary
                    if new_salary > self._salary_cap:
                        continue
                    score = self._objective(challenger, risk_level, noise) - self._objective(
                        incumbent, risk_level, noise
                    )
                    scored_options.append((score, slot_name, challenger))

            if not scored_options:
                break  # no upgrade left that fits under the cap — stop rather than loop forever

            best_score = max(option[0] for option in scored_options)
            tolerance = abs(best_score) * TIE_BREAK_TOLERANCE_FRACTION + TIE_BREAK_TOLERANCE_FLOOR
            near_best = [option for option in scored_options if option[0] >= best_score - tolerance]

            _, slot_name, challenger = rng.choice(near_best)
            current[slot_name] = challenger

        return current

    def _total_objective(self, assigned: dict[str, PlayerValue], risk_level: float, noise: dict[str, float]) -> float:
        return sum(self._objective(p, risk_level, noise) for p in assigned.values()) + self._stack_bonus(
            assigned, risk_level
        )

    def _stack_bonus(self, assigned: dict[str, PlayerValue], risk_level: float) -> float:
        if risk_level <= 0:
            return 0.0  # correlation only helps ceiling/variance, irrelevant (or mildly bad) for cash

        qb = next((p for p in assigned.values() if p.position == Position.QB), None)
        if qb is None:
            return 0.0

        roster = list(assigned.values())
        teammates = [
            p for p in roster if p is not qb and p.team == qb.team and p.position in STACK_ELIGIBLE_POSITIONS
        ]
        bring_backs = [p for p in roster if p.team == qb.opponent and p.position in STACK_ELIGIBLE_POSITIONS]

        bonus = STACK_BONUS_PER_PLAYER * len(teammates) * risk_level
        if teammates and bring_backs:
            bonus += BRING_BACK_BONUS * risk_level
        return bonus

    def _total_salary(self, assigned: dict[str, PlayerValue]) -> int:
        return sum(p.salary for p in assigned.values())

    def _to_lineup_model(self, assigned: dict[str, PlayerValue], risk_level: float) -> Lineup:
        slots = [LineupSlot(slot=slot_name, player=player) for slot_name, player in assigned.items()]

        # only report stack info when risk_level > 0 — at cash the
        # stack bonus never applied (see _stack_bonus), so any
        # coincidental correlation in the roster wasn't a deliberate
        # construction choice and shouldn't be labeled as a "stack"
        qb = next((p for p in assigned.values() if p.position == Position.QB), None)
        stack_players: list[str] = []
        bring_back_players: list[str] = []
        if qb is not None and risk_level > 0:
            roster = list(assigned.values())
            stack_players = [
                p.player_name
                for p in roster
                if p is not qb and p.team == qb.team and p.position in STACK_ELIGIBLE_POSITIONS
            ]
            bring_back_players = [
                p.player_name for p in roster if p.team == qb.opponent and p.position in STACK_ELIGIBLE_POSITIONS
            ]

        # report un-noised floor/ceiling totals — noise only influenced
        # selection, it shouldn't appear in the reported point values
        return Lineup(
            slots=slots,
            risk_level=risk_level,
            total_salary=self._total_salary(assigned),
            projected_points=round(
                sum(
                    (1 - risk_level) * p.floor_projection + risk_level * p.ceiling_projection
                    for p in assigned.values()
                ),
                2,
            ),
            floor_points=round(sum(p.floor_projection for p in assigned.values()), 2),
            ceiling_points=round(sum(p.ceiling_projection for p in assigned.values()), 2),
            stack_players=stack_players,
            bring_back_players=bring_back_players,
        )
