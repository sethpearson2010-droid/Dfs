"""
FanDuel NFL classic-contest roster rules. Kept separate from the
optimizer so if you add DraftKings or Underdog later, that's a new
constants module, not a rewrite of lineup_builder.py.
"""

from __future__ import annotations

from nfl_dfs.models import Position

SALARY_CAP = 60_000

# slot name -> eligible positions. FLEX can be filled by any of the
# three skill positions; every other slot is position-locked.
ROSTER_SLOTS: dict[str, tuple[Position, ...]] = {
    "QB": (Position.QB,),
    "RB1": (Position.RB,),
    "RB2": (Position.RB,),
    "WR1": (Position.WR,),
    "WR2": (Position.WR,),
    "WR3": (Position.WR,),
    "TE": (Position.TE,),
    "FLEX": (Position.RB, Position.WR, Position.TE),
    "D": (Position.DST,),
}
