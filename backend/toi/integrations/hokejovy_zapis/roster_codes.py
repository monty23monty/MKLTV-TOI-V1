"""Confirmed three-digit lineup specification, independent of player identity."""

from typing import Any


def parse_roster_code(value: Any) -> dict[str, Any]:
    raw = str(value) if value is not None else ""
    result = {
        "raw_code": raw, "category": None, "line_number": None,
        "line_position": None, "role": None, "display_position": "Unassigned",
        "is_goalkeeper": False, "is_starting_goalkeeper_slot": False,
        "valid": False,
    }
    if raw in {"101", "102"}:
        return {
            **result, "category": "goalkeeper", "role": "GK",
            "display_position": "Starting Goalkeeper" if raw == "101" else "Backup Goalkeeper",
            "is_goalkeeper": True, "is_starting_goalkeeper_slot": raw == "101", "valid": True,
        }
    if len(raw) != 3 or any(char not in "0123456789" for char in raw):
        return result
    category, line, slot = (int(char) for char in raw)
    if line == 0:
        return result
    if category == 2 and slot in {1, 2}:
        role, label = ("LD", "Left Defence") if slot == 1 else ("RD", "Right Defence")
        return {**result, "category": "defence", "line_number": line, "line_position": slot, "role": role, "display_position": label, "valid": True}
    if category == 3 and slot in {1, 2, 3}:
        role, label = {1: ("LW", "Left Wing"), 2: ("C", "Centre"), 3: ("RW", "Right Wing")}[slot]
        return {**result, "category": "forward", "line_number": line, "line_position": slot, "role": role, "display_position": label, "valid": True}
    return result
