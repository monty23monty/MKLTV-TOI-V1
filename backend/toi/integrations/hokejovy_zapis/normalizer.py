"""Map provider JSON into an explicit reviewable, versioned roster DTO."""

from copy import deepcopy
from typing import Any

from .errors import ProviderError
from .roster_codes import parse_roster_code

PROVIDER = "hokejovy-zapis"
ROLE_POSITION = {"GK": "G", "LD": "D", "RD": "D", "LW": "LW", "C": "C", "RW": "RW"}
RAW_CATEGORY = {"GK": "goalkeeper", "G": "goalkeeper", "DE": "defence", "DF": "defence", "D": "defence", "LD": "defence", "RD": "defence", "FW": "forward", "CE": "forward", "C": "forward", "LW": "forward", "RW": "forward"}
RAW_FALLBACK = {"GK": ("G", "GK"), "G": ("G", "GK"), "DE": ("D", "D"), "DF": ("D", "D"), "D": ("D", "D"), "LD": ("D", "LD"), "RD": ("D", "RD"), "CE": ("C", "C"), "C": ("C", "C"), "LW": ("LW", "LW"), "RW": ("RW", "RW")}


def positive_id(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProviderError(f"{field} must be a positive numeric identifier.", "invalid_identity", 422)
    raw = str(value)
    if not raw or len(raw) > 16 or any(char not in "0123456789" for char in raw) or int(raw) <= 0:
        raise ProviderError(f"{field} must be a positive numeric identifier.", "invalid_identity", 422)
    return str(int(raw))


def _required_object(value: Any, label: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise ProviderError(f"Source response is missing a valid {label} object.", "invalid_schema", 502)
    return value


def normalize_game(payload: Any, game_id: str, fetched_at_ms: int) -> dict[str, Any]:
    """Validate identity before emitting any partial import; preserve review issues."""
    game_id = positive_id(game_id, "Game ID")
    payload = _required_object(payload, "response")
    info = _required_object(payload.get("gameInfo"), "gameInfo")
    team_info = _required_object(info.get("teamInfo"), "teamInfo")
    top_roster = payload.get("roster")
    nested_roster = info.get("roster")
    if top_roster is not None and nested_roster is not None and top_roster != nested_roster:
        raise ProviderError("Top-level and gameInfo rosters disagree; review the source schema before importing.", "roster_schema_conflict", 409)
    rosters = _required_object(top_roster if top_roster is not None else nested_roster, "roster")
    warnings: list[str] = []
    teams: list[dict] = []
    players: list[dict] = []
    lines: list[dict] = []
    team_ids: set[str] = set()
    person_ids: set[str] = set()
    match_ids: set[str] = set()
    for source_side, side, color in [("home", "home", "#D8B95A"), ("visitor", "away", "#68B8D3")]:
        raw_team = _required_object(team_info.get(source_side), f"teamInfo.{source_side}")
        external_team_id = positive_id(raw_team.get("id"), f"{source_side} team ID")
        if external_team_id in team_ids:
            raise ProviderError("Home and away teams share the same external identifier.", "duplicate_identity", 409)
        team_ids.add(external_team_id)
        if not isinstance(raw_team.get("name"), str) or not raw_team["name"].strip():
            raise ProviderError(f"The {source_side} team has no valid name.", "invalid_schema", 502)
        team_name = raw_team["name"].strip()[:200]
        shortcut = raw_team.get("shortcut")
        if not isinstance(shortcut, str) or not shortcut.strip():
            shortcut = team_name[:4].upper()
            warnings.append(f"{team_name}: missing team shortcut; review the generated display abbreviation.")
        team_id = f"hz-team-{external_team_id}"
        teams.append({"id": team_id, "external_id": external_team_id, "name": team_name, "short_name": shortcut.strip()[:20], "side": side, "color": color, "provider": PROVIDER})
        roster = _required_object(rosters.get(source_side), f"roster.{source_side}")
        if len(roster) > 200:
            raise ProviderError("Source roster exceeds the supported 200 entries per team.", "invalid_schema", 502)
        slots_seen: set[str] = set()
        team_players: list[dict] = []
        groups: dict[tuple[str, int], dict] = {}
        for roster_key, raw in roster.items():
            if not isinstance(raw, dict):
                raise ProviderError(f"{team_name} roster slot {roster_key} has no identifiable player record.", "invalid_identity", 422)
            external_player_id = positive_id(raw.get("playerId"), "playerId")
            match_player_id = positive_id(raw.get("matchPlayerId"), "matchPlayerId")
            if external_player_id in person_ids or match_player_id in match_ids:
                raise ProviderError(f"Duplicate player or match-roster identifier at {team_name} slot {roster_key}; reconcile identities before import.", "duplicate_identity", 409)
            person_ids.add(external_player_id)
            match_ids.add(match_player_id)
            review_required = False
            first = raw.get("name")
            surname = raw.get("surname")
            if not isinstance(first, str) or not isinstance(surname, str) or not (first.strip() or surname.strip()):
                first, surname = "Unknown player", external_player_id
                review_required = True
                warnings.append(f"{team_name} player {external_player_id}: invalid name; excluded until reviewed.")
            first, surname = first.strip()[:100], surname.strip()[:100]
            name = f"{first} {surname}".strip()[:200]
            raw_code = raw.get("roster", roster_key)
            code = str(raw_code) if raw_code is not None else ""
            if "roster" not in raw:
                warnings.append(f"{name}: missing roster field; using the source object key {roster_key}.")
            if code != str(roster_key):
                warnings.append(f"{name}: roster field {code!r} differs from object slot {roster_key!r}; using the explicit roster field.")
            if code in slots_seen:
                raise ProviderError(f"{team_name}: duplicate assigned roster slot {code!r}.", "duplicate_roster_slot", 409)
            slots_seen.add(code)
            parsed = parse_roster_code(raw_code)
            raw_position = raw.get("position")
            raw_position = raw_position.strip().upper() if isinstance(raw_position, str) else ""
            position_unresolved = False
            assigned_role = parsed["role"]
            if parsed["valid"]:
                position = ROLE_POSITION[parsed["role"]]
                category = RAW_CATEGORY.get(raw_position)
                if category is None:
                    warnings.append(f"{name}: unfamiliar raw position {raw_position!r}; valid roster code {code} defines the assigned role.")
                elif category != parsed["category"]:
                    warnings.append(f"{name}: position {raw_position} conflicts with roster code {code}; using code role {assigned_role}.")
            else:
                warnings.append(f"{name}: unfamiliar roster code {code!r}; review the assigned position and line.")
                fallback = RAW_FALLBACK.get(raw_position)
                if fallback:
                    position, assigned_role = fallback
                else:
                    position, assigned_role = "C", None
                    position_unresolved = review_required = True
                    warnings.append(f"{name}: position_unresolved; excluded on the bench until an operator chooses a position.")
            jersey = raw.get("jersey")
            if isinstance(jersey, str) and jersey.isascii() and jersey.isdecimal():
                jersey = int(jersey)
            if isinstance(jersey, bool) or not isinstance(jersey, int) or not 0 <= jersey <= 99:
                jersey = 0
                review_required = True
                warnings.append(f"{name}: invalid jersey number; placeholder 0 is excluded until reviewed.")
            starting = raw.get("startingSix")
            if not isinstance(starting, bool):
                starting = False
                warnings.append(f"{name}: missing or invalid startingSix flag; placed on the bench for review.")
            if parsed["is_goalkeeper"] and starting != parsed["is_starting_goalkeeper_slot"]:
                warnings.append(f"{name}: goalkeeper slot {code} disagrees with startingSix; actual initial location follows startingSix.")
            player_id = f"hz-{game_id}-roster-{match_player_id}"
            player = {
                "id": player_id, "person_id": f"hz-player-{external_player_id}",
                "external_id": external_player_id, "external_player_id": external_player_id,
                "match_player_id": match_player_id, "team_id": team_id, "provider": PROVIDER,
                "first_name": first, "surname": surname, "name": name,
                "number": jersey, "position": position, "assigned_role": assigned_role,
                "display_position": parsed["display_position"] if parsed["valid"] else (assigned_role or "Unassigned"),
                "eligible": not review_required, "location": "ON_ICE" if starting and not review_required else "BENCH",
                "source": {
                    "roster_key": str(roster_key), "roster_code": code, "raw_position": raw_position,
                    "parsed_role": parsed["role"], "category": parsed["category"],
                    "line_number": parsed["line_number"], "line_position": parsed["line_position"],
                    "starting_six": starting, "is_starting_goalkeeper_slot": parsed["is_starting_goalkeeper_slot"],
                    "position_unresolved": position_unresolved, "review_required": review_required,
                    "original_jersey": raw.get("jersey"),
                },
            }
            team_players.append(player)
            if parsed["category"] in {"forward", "defence"}:
                kind = parsed["category"]
                line_number = parsed["line_number"]
                key = (kind, line_number)
                if key not in groups:
                    expected = ("LW", "C", "RW") if kind == "forward" else ("LD", "RD")
                    groups[key] = {
                        "id": f"hz-{game_id}-{side}-{kind}-{line_number}", "team_id": team_id,
                        "name": f"Forward line {line_number}" if kind == "forward" else f"Defensive pair {line_number}",
                        "kind": "FORWARD" if kind == "forward" else "DEFENCE",
                        "player_ids": [], "slots": dict.fromkeys(expected), "line_number": line_number,
                    }
                groups[key]["slots"][parsed["role"]] = player_id
        jersey_groups: dict[int, list[dict]] = {}
        for player in team_players:
            jersey_groups.setdefault(player["number"], []).append(player)
        for number, same_number in jersey_groups.items():
            if len(same_number) > 1:
                warnings.append(f"{team_name}: duplicate jersey #{number}; affected players are excluded until reviewed.")
                for player in same_number:
                    player["eligible"] = False
                    player["location"] = "BENCH"
                    player["source"]["review_required"] = True
        for key in sorted(groups):
            group = groups[key]
            group["player_ids"] = [pid for pid in group["slots"].values() if pid is not None]
            if any(pid is None for pid in group["slots"].values()):
                warnings.append(f"{team_name}: {group['name']} has empty lineup slots; the partial group is retained.")
            lines.append(group)
        on_ice = [p for p in team_players if p["location"] == "ON_ICE"]
        goalkeepers = sum(p["position"] == "G" for p in on_ice)
        skaters = len(on_ice) - goalkeepers
        if goalkeepers != 1 or skaters != 5:
            warnings.append(f"{team_name}: proposed starting lineup has {skaters} skaters and {goalkeepers} goalkeepers; review before tracking.")
        players.extend(team_players)
    players.sort(key=lambda p: (p["team_id"], p["source"]["roster_code"], p["id"]))
    def metadata_object(field: str) -> dict:
        value = info.get(field, {})
        if not isinstance(value, dict):
            warnings.append(f"Invalid {field} metadata was omitted.")
            return {}
        return deepcopy(value)
    game = {
        "name": f"{teams[0]['name']} vs {teams[1]['name']}"[:200],
        "external_id": f"{PROVIDER}:match:{game_id}",
        "external_status": str(info.get("status", "unknown"))[:100],
        "stadium": str(info.get("stadium", ""))[:500],
        "coaches": metadata_object("trainers"), "referees": metadata_object("referees"),
        "results": metadata_object("results"), "team_bilance": metadata_object("teamBilance"),
        "actual_time_name": info.get("actualTimeName"), "actual_time_alias": info.get("actualTimeAlias"),
        "number_of_viewers": info.get("numberOfViewers"),
    }
    return {"provider": PROVIDER, "external_game_id": game_id, "schema_version": 1,
            "fetched_at_ms": fetched_at_ms, "warnings": warnings, "game": game,
            "teams": teams, "players": players, "lines": lines}
