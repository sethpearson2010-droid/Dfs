"""
FanDuel salary CSV importer.

FanDuel's export (from the 'Download CSV' button on a slate's lineup
builder) typically has columns like:
  Id, Position, First Name, Last Name, Nickname, FPPG, Played,
  Salary, Game, Team, Opponent, Injury Indicator, ...

Column names have shifted slightly across FanDuel's own redesigns
before, so this class is intentionally tolerant: it looks for a
handful of known aliases per field rather than hard-coding one exact
header row. If FanDuel changes their export again, update
_COLUMN_ALIASES here — nothing else in the pipeline needs to change.
"""

from __future__ import annotations

import csv
from pathlib import Path

from nfl_dfs.models import Position, SalaryEntry

_COLUMN_ALIASES: dict[str, list[str]] = {
    "id": ["Id", "ID"],
    "position": ["Position"],
    "first_name": ["First Name"],
    "last_name": ["Last Name"],
    "nickname": ["Nickname"],
    "salary": ["Salary"],
    "team": ["Team"],
    "opponent": ["Opponent"],
    "injury_status": ["Injury Indicator"],
    "injury_details": ["Injury Details"],
}

# FanDuel's own designations that mean "not realistically going to
# play" — excluded from lineup consideration entirely, same as a
# staleness/unmatched player, rather than just flagged. "NA" means
# no designation (healthy), not "not available".
OUT_INJURY_STATUSES = {"O", "IR", "NFI", "SUSP", "PUP"}


class FanDuelSalaryImporter:
    def load(self, csv_path: str | Path) -> list[SalaryEntry]:
        path = Path(csv_path)
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            column_map = self._resolve_columns(reader.fieldnames or [])
            return [self._to_entry(row, column_map) for row in reader if row.get(column_map.get("salary", ""))]

    def _resolve_columns(self, header_row: list[str]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for field_key, aliases in _COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in header_row:
                    resolved[field_key] = alias
                    break
        missing = [k for k in ("position", "salary", "team") if k not in resolved]
        if missing:
            raise ValueError(
                f"FanDuel CSV is missing expected columns: {missing}. "
                f"Found headers: {header_row}. Update _COLUMN_ALIASES in salary.py."
            )
        return resolved

    def _to_entry(self, row: dict, column_map: dict[str, str]) -> SalaryEntry:
        position = Position.from_raw(row[column_map["position"]])
        if position is None:
            position = Position.DST  # FanDuel often lists defense as "D"

        name = (
            row.get(column_map.get("nickname", ""), "")
            or f"{row.get(column_map.get('first_name', ''), '')} {row.get(column_map.get('last_name', ''), '')}".strip()
        )

        return SalaryEntry(
            player_name=name,
            position=position,
            team=row[column_map["team"]],
            opponent=row.get(column_map.get("opponent", ""), ""),
            salary=int(float(row[column_map["salary"]])),
            fanduel_id=row.get(column_map.get("id", ""), ""),
            injury_status=row.get(column_map.get("injury_status", ""), "").strip(),
            injury_details=row.get(column_map.get("injury_details", ""), "").strip(),
        )
