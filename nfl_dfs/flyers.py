"""
Detects genuine "flyer" candidates at minimum salary — a cheap player
whose underlying OPPORTUNITY (target share, WOPR, red-zone share, or
snap share) clears a real absolute bar, distinct from a player who's
simply the cheapest available option with no real signal behind them
at all.

Real case that motivated this: a $4,000 WR with zero red-zone
involvement, a zeroed-out real game, and every matchup multiplier
sitting at or below neutral still got selected — not because of any
genuine analytical signal, but because SOMETHING has to fill a cheap
slot to make the salary math work, and an undifferentiated bottom of
the salary barrel gives the optimizer nothing to prefer one totally
unremarkable option over another.

Recent-scoring-based projections structurally can't detect an
about-to-break-out player: a rookie stepping into a bigger role, a
practice-squad promotion, someone getting real targets the ball
hasn't bounced their way on yet — their recent POINTS won't show it,
since points are downstream of opportunity that hasn't converted into
production. Opportunity metrics (already computed in advanced_stats.py
for other purposes) are a leading indicator that doesn't have that lag.

Deliberately conservative, the same philosophy as regression.py: a
real, absolute minimum bar on the underlying metrics is required, not
just "relatively better than other cheap players" — if the whole
bottom of the salary barrel has equally thin usage, nobody there
should be labeled a flyer just for being the least-bad of a bad bunch.
"""

from __future__ import annotations

from nfl_dfs.models import FlyerCandidate, PlayerValue, Position

ELIGIBLE_POSITIONS = {Position.WR, Position.RB, Position.TE}

# only genuinely cheap players qualify — this is specifically about
# min-salary "dart throw" plays, not a general value metric (that's
# what smash_score/value_score already cover for the whole pool)
MAX_SALARY_FOR_FLYER = 5000

# a real, absolute minimum bar on underlying opportunity — clearing
# ANY ONE of these means real, meaningful involvement, not just
# "somewhat more than an equally-thin peer." Below all three genuinely
# means "barely on the field," not a hidden gem worth a dart throw.
#
# Raised significantly (0.12/0.18/0.15 → 0.20/0.35/0.30) after a real
# reported case (Kevin Austin Jr., Mason Taylor, Greg Dulcich flagged
# as flyers when they shouldn't have been) exposed how permissive the
# original bars actually were: checked directly against the full real
# player pool and found 71 of 167 (42%) of the ENTIRE min-salary
# WR/RB/TE population qualified — the bar was doing almost no
# selecting at all. The new thresholds bring that down to ~19 of 167
# (11%) on the same real data, a genuinely selective rate rather than
# "most cheap players somehow clear it."
MIN_TARGET_SHARE = 0.20
MIN_WOPR = 0.35
MIN_REDZONE_SHARE = 0.30

# snap share is a genuinely different kind of signal from the three
# above — target share/WOPR/red-zone share all measure INVOLVEMENT
# when the ball comes their way, but a player can be getting real,
# meaningful field time (run-blocking, pass-pro, routes that don't
# get the target) well before that shows up in receiving/red-zone
# numbers at all. Uses the single most recent real game (see
# value.py's _build_recent_snap_pcts) — the same "are they playing
# right now" reasoning as the backup-QB check.
#
# Raised from 0.40 to 0.65 in the same tightening pass above — 40%
# turned out to describe ordinary rotational usage for a large share
# of the player pool, not a meaningful standout signal; 65% means real,
# sustained snaps, not just "gets some run."
MIN_SNAP_PCT = 0.65

TOP_N_PER_POSITION = 3


class FlyerCalculator:
    def identify(self, player_values: list[PlayerValue]) -> list[FlyerCandidate]:
        candidates: list[FlyerCandidate] = []

        for pv in player_values:
            if pv.position not in ELIGIBLE_POSITIONS:
                continue
            if pv.salary > MAX_SALARY_FOR_FLYER:
                continue
            if pv.name_match_quality == "unmatched" or pv.is_stale or pv.is_out or not pv.advanced_metrics:
                continue

            advanced = pv.advanced_metrics
            # a real gap found and fixed: redzone_share can be 100%
            # carryover (see real_games_in_touches_sample, added in
            # advanced_stats.py) even for a player who isn't flagged
            # is_stale overall — the is_stale check above covers their
            # SCORING average, not this specific metric. A player
            # whose entire red-zone signal is last season's role
            # shouldn't count toward "genuine current opportunity"
            # here, same reasoning as the identical fix in
            # regression.py. target_share/wopr don't need this same
            # guard — they come from weekly_stats, which the is_stale
            # check above already protects.
            redzone_share_is_real = advanced.real_games_in_touches_sample > 0
            has_real_snap_share = pv.recent_snap_pct is not None and pv.recent_snap_pct >= MIN_SNAP_PCT
            clears_bar = (
                advanced.recent_target_share >= MIN_TARGET_SHARE
                or advanced.recent_wopr >= MIN_WOPR
                or (redzone_share_is_real and advanced.recent_redzone_share >= MIN_REDZONE_SHARE)
                or has_real_snap_share
            )
            if not clears_bar:
                continue

            candidates.append(
                FlyerCandidate(
                    player_name=pv.player_name,
                    position=pv.position,
                    team=pv.team,
                    opponent=pv.opponent,
                    salary=pv.salary,
                    target_share=advanced.recent_target_share,
                    wopr=advanced.recent_wopr,
                    redzone_share=advanced.recent_redzone_share,
                    snap_pct=pv.recent_snap_pct,
                )
            )

        found: list[FlyerCandidate] = []
        for position in ELIGIBLE_POSITIONS:
            position_candidates = sorted(
                (c for c in candidates if c.position == position),
                key=lambda c: c.wopr,
                reverse=True,
            )
            found.extend(position_candidates[:TOP_N_PER_POSITION])
        return found
