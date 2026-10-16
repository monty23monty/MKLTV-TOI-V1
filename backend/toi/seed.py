"""Fictional demonstration data, kept outside the tracking domain.

These names and combinations are illustrative, not an MK Lightning roster.
Internal identifiers are independent of jersey numbers and can map to real IDs.
"""

from __future__ import annotations

import time
from typing import Any


_HOME = [
    (9, "Alex Mercer", "C"), (17, "Theo Bennett", "LW"), (21, "Finn Clarke", "RW"),
    (12, "Jamie Rowan", "C"), (18, "Noah Ellis", "LW"), (27, "Leo Hart", "RW"),
    (15, "Oliver Brooks", "C"), (23, "Max Sutton", "LW"), (29, "Sam Fletcher", "RW"),
    (14, "Ben Archer", "C"), (24, "Jack Palmer", "LW"), (34, "Harry Reid", "RW"),
    (4, "Luke Morgan", "D"), (6, "Will Dawson", "D"), (8, "Ethan Shaw", "D"),
    (22, "Charlie Wells", "D"), (44, "Adam Ross", "D"), (55, "George Mason", "D"),
    (31, "Daniel Frost", "G"), (35, "Tom Sterling", "G"),
]

_AWAY = [
    (10, "Ryan Cole", "C"), (19, "Oscar Hayes", "LW"), (28, "Isaac Ward", "RW"),
    (16, "Dylan Price", "C"), (26, "Archie Blake", "LW"), (38, "Louis Grant", "RW"),
    (11, "Jude Spencer", "C"), (20, "Freddie Lane", "LW"), (37, "Alfie Nash", "RW"),
    (13, "Toby Drake", "C"), (25, "Hugo West", "LW"), (40, "Milo Chase", "RW"),
    (3, "Zach Turner", "D"), (5, "Callum North", "D"), (7, "Lewis Stone", "D"),
    (32, "Nathan Bell", "D"), (41, "Aaron Fox", "D"), (52, "Reuben Scott", "D"),
    (30, "Chris Vale", "G"), (33, "Josh Rivers", "G"),
]


def create_demo_state(
    game_id: str = "demo",
    name: str | None = None,
    home_name: str | None = None,
    away_name: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Create an isolated two-team game with four lines, three pairs and two Gs.

    Both teams begin with line 1, pair 1 and their first goalkeeper on the ice.
    The engine reconstructs open shifts from these initial locations.
    """
    anchor = int(time.time() * 1000) if now_ms is None else now_ms
    home = home_name or "Milton Keynes Storm"
    away = away_name or "Northbank Wolves"
    teams = [
        {"id": f"{game_id}-home", "name": home, "short_name": "MKS", "color": "#D8B95A", "side": "home", "external_id": None},
        {"id": f"{game_id}-away", "name": away, "short_name": "NBW", "color": "#76A9D5", "side": "away", "external_id": None},
    ]
    players: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for team, roster in zip(teams, (_HOME, _AWAY), strict=True):
        team_players = []
        for index, (number, player_name, position) in enumerate(roster):
            player_id = f"{team['id']}-p{index + 1:02d}"
            player = {
                "id": player_id, "team_id": team["id"], "name": player_name,
                "number": number, "position": position, "eligible": True,
                "location": "ON_ICE" if index in (0, 1, 2, 12, 13, 18) else "BENCH",
                "external_id": None,
            }
            players.append(player)
            team_players.append(player)
        for index in range(4):
            lines.append({
                "id": f"{team['id']}-f{index + 1}", "team_id": team["id"],
                "name": f"Line {index + 1}", "kind": "FORWARD",
                "player_ids": [p["id"] for p in team_players[index * 3:index * 3 + 3]],
            })
        for index in range(3):
            lines.append({
                "id": f"{team['id']}-d{index + 1}", "team_id": team["id"],
                "name": f"Pair {index + 1}", "kind": "DEFENCE",
                "player_ids": [p["id"] for p in team_players[12 + index * 2:14 + index * 2]],
            })
    return {
        "game": {
            "id": game_id, "name": name or f"{home} vs {away}", "status": "LIVE",
            "competition": "Demonstration · fictional rosters", "scheduled_start": None,
            "external_id": None, "period_lengths_ms": [1_200_000, 1_200_000, 1_200_000],
        },
        "teams": teams, "players": players, "lines": lines,
        "clock": {
            "period": 1, "remaining_ms": 1_200_000, "running": False,
            "playing_ms": 0, "anchor_unix_ms": anchor, "period_playing_ms": {"1": 0},
        },
        "events": [], "shifts": [], "version": 0,
    }
