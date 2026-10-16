"""Pure, replayable hockey tracking domain.

``playing_ms`` is an accumulated playing-time axis. Changing the displayed clock
never rewinds that axis; time only advances while the authoritative clock runs.
Movement events are projected from an initial lineup, so corrections also rebuild
shifts and all statistics. Callers persist the returned state atomically.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

State = dict[str, Any]
LOCATIONS = {"ON_ICE", "BENCH", "PENALTY_BOX"}
POSITIONS = {"C", "LW", "RW", "D", "G"}
MOVEMENTS = {"substitute", "move"}


class DomainError(ValueError):
    def __init__(self, message: str, code: str = "invalid_action", status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DomainError(f"{name} must be an integer of at least {minimum}.")
    return value


def _ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise DomainError(f"{name} must be a list of player IDs.")
    if len(set(value)) != len(value):
        raise DomainError(f"{name} contains a duplicate player.")
    return value


def _ensure_base(state: State) -> None:
    clock = state["clock"]
    clock.setdefault("playing_ms", 0)
    clock.setdefault("period_playing_ms", {})
    clock.setdefault("period_spans", [])
    state.setdefault("events", [])
    state.setdefault("version", 0)
    state.setdefault("lines", [])
    if "_initial_locations" not in state:
        state["_initial_locations"] = {p["id"]: p["location"] for p in state["players"]}
        state["_initial_playing_ms"] = clock["playing_ms"]
        state["_initial_period"] = clock["period"]
        _rebuild(state)


def materialize_clock(state: State, now_ms: int) -> State:
    """Return a copy with elapsed running time accrued, capped at period zero."""
    result = deepcopy(state)
    _ensure_base(result)
    clock = result["clock"]
    anchor = clock.get("anchor_unix_ms", now_ms)
    elapsed = max(0, now_ms - anchor) if clock["running"] else 0
    elapsed = min(elapsed, max(0, clock["remaining_ms"]))
    if elapsed:
        start = clock["playing_ms"]
        end = start + elapsed
        spans = clock["period_spans"]
        if spans and spans[-1]["period"] == clock["period"] and spans[-1]["end_playing_ms"] == start:
            spans[-1]["end_playing_ms"] = end
        else:
            spans.append({"period": clock["period"], "start_playing_ms": start, "end_playing_ms": end})
        clock["playing_ms"] = end
        key = str(clock["period"])
        clock["period_playing_ms"][key] = clock["period_playing_ms"].get(key, 0) + elapsed
        clock["remaining_ms"] -= elapsed
    if clock["remaining_ms"] == 0:
        clock["running"] = False
    # A backward wall-clock observation must not make the same interval accrue twice.
    clock["anchor_unix_ms"] = max(anchor, now_ms)
    return result


def _period_at(state: State, playing_ms: int) -> int:
    if playing_ms == state["clock"]["playing_ms"]:
        return state["clock"]["period"]
    for span in reversed(state["clock"].get("period_spans", [])):
        if span["start_playing_ms"] <= playing_ms <= span["end_playing_ms"]:
            return span["period"]
    return state.get("_initial_period", state["clock"]["period"])


def _player_map(state: State) -> dict[str, State]:
    return {p["id"]: p for p in state["players"]}


def _movement_changes(state: State, event: State, locations: dict[str, str], *, live: bool) -> dict[str, str]:
    players = _player_map(state)
    payload = event["payload"]
    if event["type"] == "move":
        player_id = payload.get("player_id")
        target = payload.get("to")
        if not isinstance(player_id, str) or player_id not in players:
            raise DomainError("Player does not exist.", "player_not_found")
        if not isinstance(target, str) or target not in LOCATIONS:
            raise DomainError("Unknown player location.")
        if locations[player_id] == target:
            raise DomainError("Player is already at that location.", "location_conflict", 409)
        if target == "ON_ICE" and live and not players[player_id].get("eligible", True):
            raise DomainError("An unavailable player cannot enter the ice.")
        return {player_id: target}
    outgoing = _ids(payload.get("outgoing_ids", []), "outgoing_ids")
    incoming = _ids(payload.get("incoming_ids", []), "incoming_ids")
    if not outgoing and not incoming:
        raise DomainError("Select at least one player to move.")
    if set(outgoing) & set(incoming):
        raise DomainError("A player cannot enter and leave in the same substitution.")
    team_id = payload.get("team_id")
    if not isinstance(team_id, str) or team_id not in {t["id"] for t in state["teams"]}:
        raise DomainError("Team does not exist.")
    for player_id in outgoing + incoming:
        if player_id not in players or players[player_id]["team_id"] != team_id:
            raise DomainError("Every substitution player must belong to the selected team.")
    for player_id in outgoing:
        if locations[player_id] != "ON_ICE":
            raise DomainError("Outgoing player is no longer on the ice.", "location_conflict", 409)
    for player_id in incoming:
        if locations[player_id] != "BENCH":
            raise DomainError("Incoming player must be on the bench.", "location_conflict", 409)
        if live and not players[player_id].get("eligible", True):
            raise DomainError("An unavailable player cannot enter the ice.")
    return {**{pid: "BENCH" for pid in outgoing}, **{pid: "ON_ICE" for pid in incoming}}


def _effective_movements(state: State) -> list[State]:
    return [e for e in state["events"] if e["type"] in MOVEMENTS and e.get("active", True)]


def _rebuild(state: State) -> None:
    players = _player_map(state)
    locations = dict(state["_initial_locations"])
    shifts: list[State] = []
    opened: dict[str, State] = {}
    last_events: dict[str, str] = {}

    def enter(player_id: str, at: int, period: int, event_id: str) -> None:
        shift = {
            "id": f"{event_id}:{player_id}", "player_id": player_id,
            "team_id": players[player_id]["team_id"], "start_playing_ms": at,
            "end_playing_ms": None, "start_period": period, "end_period": None,
            "period": period, "source_event_id": event_id, "source": "operator",
        }
        shifts.append(shift)
        opened[player_id] = shift

    for player_id, location in locations.items():
        if player_id in players and location == "ON_ICE":
            enter(player_id, state["_initial_playing_ms"], state["_initial_period"], "initial")
    last_at = state["_initial_playing_ms"]
    for event in _effective_movements(state):
        at = event["at_playing_ms"]
        if at < last_at:
            raise DomainError("Movement history is not chronological.", "history_conflict", 409)
        last_at = at
        try:
            changes = _movement_changes(state, event, locations, live=False)
        except DomainError as exc:
            raise DomainError(
                "This change conflicts with a later movement. Correct or undo the dependent movement first.",
                "history_conflict", 409,
            ) from exc
        if event["type"] == "move":
            event["payload"]["from"] = locations[event["payload"]["player_id"]]
        for player_id, target in changes.items():
            if locations[player_id] == "ON_ICE":
                shift = opened.pop(player_id)
                shift["end_playing_ms"] = at
                shift["end_period"] = event["period"]
            if target == "ON_ICE":
                enter(player_id, at, event["period"], event["id"])
            locations[player_id] = target
            last_events[player_id] = event["id"]
    for player in state["players"]:
        player["location"] = locations[player["id"]]
        player["last_event_id"] = last_events.get(player["id"])
        if player["location"] == "ON_ICE" and not player.get("eligible", True):
            raise DomainError("This correction would put an unavailable player on the ice.", "history_conflict", 409)
    state["shifts"] = shifts


def _event_time(state: State, payload: State) -> int:
    now = state["clock"]["playing_ms"]
    at = _integer(payload.get("at_playing_ms", now), "at_playing_ms")
    if at > now or at < state["_initial_playing_ms"]:
        raise DomainError("The interaction timestamp is outside the current game timeline.")
    # Do not insert behind an already confirmed movement, including an undone one.
    # This prevents a delayed client request from silently changing event order.
    latest = max((e["at_playing_ms"] for e in state["events"] if e["type"] in MOVEMENTS), default=state["_initial_playing_ms"])
    if at < latest:
        raise DomainError("Another movement occurred after this selection. Select the players again.", "stale_selection", 409)
    return at


def _clock_action(state: State, payload: State) -> None:
    clock = state["clock"]
    operation = payload.get("operation")
    if not isinstance(operation, str) or operation not in {"start", "pause", "set", "advance"}:
        raise DomainError("Unknown clock operation.")
    if operation == "start":
        if clock["remaining_ms"] <= 0:
            raise DomainError("Set a positive remaining time or advance the period first.")
        clock["running"] = True
        state["game"]["status"] = "LIVE"
        return
    if operation == "pause":
        clock["running"] = False
        return
    lengths = payload.get("period_lengths_ms", state["game"]["period_lengths_ms"])
    if not isinstance(lengths, list) or not lengths:
        raise DomainError("Configure at least one period length.")
    for duration in lengths:
        _integer(duration, "Period length", 1)
    period = _integer(payload.get("period", clock["period"] + (operation == "advance")), "period", 1)
    # Extra periods are deliberate configuration, never silently invented.
    if period > len(lengths):
        raise DomainError("Configure an additional period before advancing.")
    remaining = _integer(payload.get("remaining_ms", lengths[period - 1] if period != clock["period"] or operation == "advance" else clock["remaining_ms"]), "remaining_ms")
    if remaining > lengths[period - 1]:
        raise DomainError("Remaining time exceeds the configured period length.")
    state["game"]["period_lengths_ms"] = list(lengths)
    clock["period"] = period
    clock["remaining_ms"] = remaining
    clock["running"] = False
    clock["period_playing_ms"].setdefault(str(period), 0)


def _setup_action(state: State, payload: State) -> None:
    players = _player_map(state)
    changes = payload.get("players", [])
    if not isinstance(changes, list) or any(not isinstance(p, dict) for p in changes):
        raise DomainError("players must be a list of player objects.")
    if any(not isinstance(p.get("id"), str) or not p["id"].strip() for p in changes):
        raise DomainError("Every roster player needs a nonempty unique ID.")
    if len({p.get("id") for p in changes}) != len(changes):
        raise DomainError("A player can only appear once in setup.")
    has_history = bool([e for e in state["events"] if e["type"] in MOVEMENTS])
    tracking_begun = has_history or state["clock"]["playing_ms"] > 0
    teams = {t["id"] for t in state["teams"]}
    for change in changes:
        player_id = change.get("id")
        if player_id not in players:
            if tracking_begun:
                raise DomainError("Import new roster players before tracking begins.")
            required = {"id", "team_id", "name", "number", "position", "eligible", "location"}
            if not required <= change.keys():
                raise DomainError("A new roster player requires id, team_id, name, number, position, eligible and location.")
            if not isinstance(player_id, str) or not player_id.strip():
                raise DomainError("A new roster player needs a nonempty unique ID.")
            if not isinstance(change["team_id"], str) or change["team_id"] not in teams:
                raise DomainError("A new roster player must belong to a team in this game.")
            player = deepcopy(change)
            state["players"].append(player)
            players[player_id] = player
            state["_initial_locations"][player_id] = change["location"]
        player = players[player_id]
        if "team_id" in change and change["team_id"] != player["team_id"]:
            raise DomainError("An existing player cannot move between team rosters.")
        for key in ("name", "number", "position", "assigned_role", "eligible", "external_id", "location"):
            if key not in change:
                continue
            value = change[key]
            if key == "position" and (not isinstance(value, str) or value not in POSITIONS):
                raise DomainError("Unknown player position.")
            if key == "position" and player.get("assigned_role"):
                role_positions = {"GK": "G", "G": "G", "LD": "D", "RD": "D", "D": "D", "LW": "LW", "C": "C", "RW": "RW"}
                if role_positions.get(player["assigned_role"]) != value:
                    player["assigned_role"] = "GK" if value == "G" else value
            if key == "assigned_role":
                role_positions = {"GK": "G", "G": "G", "LD": "D", "RD": "D", "D": "D", "LW": "LW", "C": "C", "RW": "RW"}
                if not isinstance(value, str) or role_positions.get(value) != player["position"]:
                    raise DomainError("Assigned role must match the player's general position.")
            if key == "number":
                _integer(value, "Jersey number", 0)
                if value > 99:
                    raise DomainError("Jersey numbers must be between 0 and 99.")
            if key == "name" and (not isinstance(value, str) or not value.strip()):
                raise DomainError("Player name cannot be empty.")
            if key == "eligible" and not isinstance(value, bool):
                raise DomainError("eligible must be a boolean.")
            if key == "location":
                if not isinstance(value, str) or value not in LOCATIONS:
                    raise DomainError("Unknown player location.")
                if value != player["location"] and tracking_begun:
                    raise DomainError("Use a player movement to correct a lineup after tracking begins.")
                state["_initial_locations"][player_id] = value
            player[key] = value
        if not player.get("eligible", True) and player["location"] != "BENCH":
            raise DomainError("Move a player to the bench before marking them unavailable.")
    for team in state["teams"]:
        numbers = [p["number"] for p in players.values() if p["team_id"] == team["id"] and p.get("eligible", True)]
        if len(numbers) != len(set(numbers)):
            raise DomainError("Dressed players on a team must have distinct jersey numbers.")
    if "lines" in payload:
        lines = payload["lines"]
        if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
            raise DomainError("lines must be a list of line objects.")
        line_ids: set[str] = set()
        for line in lines:
            if not isinstance(line.get("id"), str) or not line["id"] or line["id"] in line_ids:
                raise DomainError("Line IDs must be unique.")
            line_ids.add(line["id"])
            if not isinstance(line.get("kind"), str) or line["kind"] not in {"FORWARD", "DEFENCE", "SPECIAL"}:
                raise DomainError("Unknown line kind.")
            members = _ids(line.get("player_ids", []), "Line members")
            if line.get("team_id") not in {t["id"] for t in state["teams"]} or any(pid not in players or players[pid]["team_id"] != line.get("team_id") for pid in members):
                raise DomainError("A line must contain players from its own team.")
        state["lines"] = deepcopy(lines)
    if "game" in payload:
        game_change = payload["game"]
        if not isinstance(game_change, dict):
            raise DomainError("game must be an object.")
        for key in ("name", "competition", "scheduled_start", "external_id", "status"):
            if key in game_change:
                state["game"][key] = game_change[key]
        if "period_lengths_ms" in game_change:
            _clock_action(state, {"operation": "set", "period_lengths_ms": game_change["period_lengths_ms"]})
    _rebuild(state)


def apply_action(state: State, action: State, now_ms: int) -> State:
    """Validate and apply one command without mutating the input, including errors.

    Persistence layers must lock or compare-and-swap the stored version in the
    same transaction as saving this result. request_id is additionally recorded
    here to make direct retries harmless.
    """
    result = materialize_clock(state, now_ms)
    request_id = action.get("request_id")
    if request_id and any(e.get("request_id") == request_id for e in result["events"]):
        return result
    if action.get("expected_version") is not None and action["expected_version"] != result["version"]:
        raise DomainError("State changed in another session. The latest lineup has been loaded; retry the action.", "version_conflict", 409)
    kind = action.get("type")
    if not isinstance(kind, str):
        raise DomainError("Action type must be a string.")
    payload = deepcopy(action.get("payload", {}))
    if not isinstance(payload, dict):
        raise DomainError("Action payload must be an object.")
    event = {
        "id": str(uuid4()), "request_id": request_id, "type": kind,
        "payload": payload, "at_playing_ms": result["clock"]["playing_ms"],
        "period": result["clock"]["period"], "active": True,
        "remaining_ms": result["clock"]["remaining_ms"],
        "actor": action.get("actor", "operator"), "created_at_ms": now_ms,
    }
    if kind in MOVEMENTS:
        event["at_playing_ms"] = _event_time(result, payload)
        event["period"] = _period_at(result, event["at_playing_ms"])
        if event["period"] == result["clock"]["period"]:
            event["remaining_ms"] = min(
                result["game"]["period_lengths_ms"][event["period"] - 1],
                result["clock"]["remaining_ms"] + result["clock"]["playing_ms"] - event["at_playing_ms"],
            )
        _movement_changes(result, event, {p["id"]: p["location"] for p in result["players"]}, live=True)
        result["events"].append(event)
        _rebuild(result)
    elif kind == "clock":
        _clock_action(result, payload)
        result["events"].append(event)
    elif kind in {"undo", "correct"}:
        candidates = _effective_movements(result)
        event_id = payload.get("event_id")
        target = next((e for e in reversed(candidates) if event_id is None or e["id"] == event_id), None)
        if target is None:
            raise DomainError("No active movement is available to undo or correct.", "event_not_found", 404)
        if kind == "correct":
            if not event_id:
                raise DomainError("Choose a movement to correct.")
            replacement = payload.get("replacement", {})
            if not isinstance(replacement, dict) or not isinstance(replacement.get("type"), str) or replacement["type"] not in MOVEMENTS or not isinstance(replacement.get("payload"), dict):
                raise DomainError("A correction must replace a movement with a movement.")
            corrected = {
                **deepcopy(target), "id": event["id"] + ":replacement",
                "type": replacement["type"], "payload": deepcopy(replacement["payload"]),
                "request_id": None, "replaces_event_id": target["id"], "active": True,
                "created_at_ms": now_ms, "actor": event["actor"],
            }
            # Replacement occupies the original position and timestamp in history.
            result["events"].insert(result["events"].index(target) + 1, corrected)
            target["corrected_by"] = event["id"]
        else:
            target["undone_by"] = event["id"]
        target["active"] = False
        event["target_event_id"] = target["id"]
        result["events"].append(event)
        _rebuild(result)
    elif kind == "setup":
        _setup_action(result, payload)
        result["events"].append(event)
    else:
        raise DomainError("Unknown action type.")
    result["version"] += 1
    return result


def snapshot(state: State, now_ms: int) -> State:
    """Public authoritative snapshot with live, integer-millisecond statistics."""
    result = materialize_clock(state, now_ms)
    now = result["clock"]["playing_ms"]
    shifts_by_player: dict[str, list[State]] = {}
    for shift in result["shifts"]:
        shifts_by_player.setdefault(shift["player_id"], []).append(shift)
    for player in result["players"]:
        shifts = shifts_by_player.get(player["id"], [])
        completed = [s for s in shifts if s["end_playing_ms"] is not None]
        durations = [max(0, (s["end_playing_ms"] if s["end_playing_ms"] is not None else now) - s["start_playing_ms"]) for s in shifts]
        completed_total = sum(s["end_playing_ms"] - s["start_playing_ms"] for s in completed)
        by_period: dict[str, int] = {str(i + 1): 0 for i in range(len(result["game"]["period_lengths_ms"]))}
        for shift in shifts:
            start = shift["start_playing_ms"]
            end = shift["end_playing_ms"] if shift["end_playing_ms"] is not None else now
            for span in result["clock"]["period_spans"]:
                overlap = max(0, min(end, span["end_playing_ms"]) - max(start, span["start_playing_ms"]))
                key = str(span["period"])
                by_period[key] = by_period.get(key, 0) + overlap
        active = next((s for s in reversed(shifts) if s["end_playing_ms"] is None), None)
        player.update({
            "total_toi_ms": sum(durations),
            "current_shift_ms": max(0, now - active["start_playing_ms"]) if active else 0,
            "completed_shifts": len(completed),
            "average_shift_ms": completed_total // len(completed) if completed else 0,
            "longest_shift_ms": max(durations, default=0),
            "toi_by_period": by_period, "strength_toi": None,
        })
    strengths: dict[str, int] = {}
    warnings: list[str] = []
    for team in result["teams"]:
        on_ice = [p for p in result["players"] if p["team_id"] == team["id"] and p["location"] == "ON_ICE"]
        skaters = sum(p["position"] != "G" for p in on_ice)
        goalies = sum(p["position"] == "G" for p in on_ice)
        strengths[team["side"]] = skaters
        if skaters > 6 or skaters < 3:
            warnings.append(f"{team['short_name']}: {skaters} skaters on ice; verify the lineup.")
        if goalies > 1:
            warnings.append(f"{team['short_name']}: multiple goalkeepers on ice.")
        if goalies == 0:
            warnings.append(f"{team['short_name']}: empty net.")
        if skaters > 5 and goalies:
            warnings.append(f"{team['short_name']}: {skaters} skaters plus goalkeeper; verify the lineup.")
    result["strengths"] = strengths
    result["warnings"] = warnings
    for event in result["events"]:
        event["playing_ms"] = event["at_playing_ms"]
        event["voided"] = not event.get("active", True)
    for shift in result["shifts"]:
        shift["start_ms"] = shift["start_playing_ms"]
        shift["end_ms"] = shift["end_playing_ms"]
    return {key: value for key, value in result.items() if not key.startswith("_")}
