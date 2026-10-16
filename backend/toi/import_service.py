"""Transactional import and conservative three-way source reconciliation.

The provider supplies a normalized DTO. The existing tracking engine owns all
locations, shifts and playing time; source refresh never replays startingSix.
"""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from . import domain
from .models import AuditRow, GameRow, ReceiptRow
from .store import StoreError, constrain_external_clock, now_ms, public_snapshot
from .journal import append_change, stream_enabled

PROVIDER = "hokejovy-zapis"


def fingerprint(dto: dict) -> str:
    # A fetch timestamp/cache flag is not a roster change.
    content = {k: v for k, v in dto.items() if k not in {"fetched_at_ms", "preview_fingerprint", "cached", "stale", "cache_age_ms", "cache", "cache_error", "warnings"}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def preview_response(dto: dict) -> dict:
    dto = deepcopy(dto)
    dto["preview_fingerprint"] = fingerprint(dto)
    return dto


def require_supervisor(principal):
    if principal.team_ids is not None or not principal.can_control_clock:
        raise StoreError(403, "Import and source refresh require a match supervisor")


def game_id_for(external_game_id: str) -> str:
    if not external_game_id.isascii() or not external_game_id.isdecimal() or not 0 < int(external_game_id) < 10**15:
        raise StoreError(422, "Game ID must be a positive integer of at most 15 digits")
    return f"hz-{int(external_game_id)}"


def make_state(dto: dict, body: dict, timestamp: int) -> dict:
    period_lengths = dto["game"].get("period_lengths_ms", [1200000, 1200000, 1200000])
    coverage = body.get("coverage") or {"period": 1, "remaining_ms": period_lengths[0]}
    period, remaining = coverage["period"], coverage["remaining_ms"]
    if period < 1 or period > len(period_lengths) or remaining < 0 or remaining > period_lengths[period - 1]:
        raise StoreError(422, "Coverage clock is outside the configured periods")
    game_id = game_id_for(dto["external_game_id"])
    players = deepcopy(dto["players"])
    for player in players:
        if body.get("starting_mode", "official") == "manual":
            player["location"] = "BENCH"
    state = {
        "game": {"id": game_id, "name": dto["game"]["name"], "status": "SETUP", "competition": "Hokejový Zápis imported match", "scheduled_start": dto["game"].get("scheduled_start"), "external_id": f"{PROVIDER}:match:{dto['external_game_id']}", "period_lengths_ms": period_lengths, "external_status": dto["game"].get("external_status"), "stadium": dto["game"].get("stadium"), "coverage": {**coverage, "partial_match": period != 1 or remaining != period_lengths[0]}},
        "teams": deepcopy(dto["teams"]), "players": players, "lines": deepcopy(dto["lines"]),
        "clock": {"period": period, "remaining_ms": remaining, "running": False, "playing_ms": 0, "anchor_unix_ms": timestamp, "period_playing_ms": {str(i + 1): 0 for i in range(len(period_lengths))}},
        "events": [], "shifts": [], "version": 0,
        "integration": {"provider": PROVIDER, "external_game_id": dto["external_game_id"], "schema_version": dto["schema_version"], "imported_at_ms": timestamp, "last_successful_refresh_ms": timestamp, "external_status": dto["game"].get("external_status"), "original_snapshot": deepcopy(dto), "latest_snapshot": deepcopy(dto), "warnings": list(dto.get("warnings", [])), "changes": []},
    }
    state = domain.materialize_clock(state, timestamp)
    patches = body.get("players") or []
    known = {p["id"] for p in players}
    if any(p["id"] not in known for p in patches):
        raise StoreError(422, "Import adjustments must identify a player from this preview")
    payload = {"players": patches, "lines": body.get("lines") if body.get("lines") is not None else state["lines"]}
    # The established setup engine validates rosters/line memberships atomically.
    state = domain.apply_action(state, {"type": "setup", "payload": payload, "actor": body["actor"]}, timestamp)
    for player in state["players"]:
        if player["location"] == "PENALTY_BOX":
            raise StoreError(422, "A new imported session starts with empty penalty boxes")
        patch = next((p for p in patches if p["id"] == player["id"]), {})
        if "assigned_role" in patch:
            player["assigned_role"] = patch["assigned_role"]
    originals = {p["id"]: p for p in dto["players"]}
    state["integration"]["operator_overrides"] = {"players": {}, "teams": {}, "game": []}
    for player in state["players"]:
        fields = [key for key in ("name", "number", "position", "assigned_role", "eligible") if player.get(key) != originals[player["id"]].get(key)]
        if fields:
            state["integration"]["operator_overrides"]["players"][player["id"]] = fields
    for team in state["teams"]:
        active = [p for p in state["players"] if p["team_id"] == team["id"] and p["location"] == "ON_ICE"]
        if len(active) != 6 or sum(p["position"] == "G" for p in active) != 1:
            state["integration"]["warnings"].append(f"{team['short_name']}: initial lineup is not five skaters and one goalkeeper; review before starting.")
    return state


def source_changes(state: dict, dto: dict) -> list[dict]:
    previous = state["integration"]["latest_snapshot"]
    before = {p["id"]: p for p in previous["players"]}
    after = {p["id"]: p for p in dto["players"]}
    changes = []
    for player_id in sorted(before.keys() | after.keys()):
        if player_id not in before:
            changes.append({"type": "player.added", "player_id": player_id, "message": f"New roster entry: #{after[player_id]['number']} {after[player_id]['name']}; will be added to the bench."})
        elif player_id not in after:
            changes.append({"type": "player.removed", "player_id": player_id, "message": f"{before[player_id]['name']} disappeared from source; local record and all tracked time will be retained."})
        else:
            for key in ("name", "number", "position", "assigned_role", "eligible", "source"):
                if before[player_id].get(key) != after[player_id].get(key):
                    changes.append({"type": "player.changed", "player_id": player_id, "field": key, "before": before[player_id].get(key), "after": after[player_id].get(key), "message": f"{after[player_id]['name']}: source {key} changed; operator overrides and location are preserved."})
    if previous["lines"] != dto["lines"]:
        changes.append({"type": "lines.changed", "message": "Official lines/pairs changed. Current presets are kept unless Restore official lines is selected."})
    if previous["game"] != dto["game"] or previous["teams"] != dto["teams"]:
        changes.append({"type": "match.changed", "message": "Source match information changed; local session status remains independent."})
    return changes


def reconcile(state: dict, dto: dict, restore_lines: bool, timestamp: int) -> dict:
    state = domain.materialize_clock(constrain_external_clock(state, timestamp)[0], timestamp)
    previous = state["integration"]["latest_snapshot"]
    previous_players = {p["id"]: p for p in previous["players"]}
    incoming = {p["id"]: p for p in dto["players"]}
    current = {p["id"]: p for p in state["players"]}
    if {t["side"]: t["id"] for t in previous["teams"]} != {t["side"]: t["id"] for t in dto["teams"]}:
        raise StoreError(409, "Source team identity changed; reconciliation is required before refreshing")
    overrides = state["integration"].setdefault("operator_overrides", {"players": {}, "teams": {}, "game": []})
    old_people = {p.get("person_id"): p["id"] for p in state["players"] if p.get("person_id")}
    warnings = list(dto.get("warnings", []))
    for player_id, source in incoming.items():
        if source.get("person_id") in old_people and old_people[source["person_id"]] != player_id:
            raise StoreError(409, "Source changed a player's match-specific identity; manual reconciliation is required")
        if player_id in current:
            player = current[player_id]
            if player.get("person_id") != source.get("person_id") or player["team_id"] != source["team_id"]:
                raise StoreError(409, "Source reused a roster identity for a different player or team")
            old = previous_players.get(player_id, {})
            preserved = set(overrides["players"].get(player_id, []))
            preserved.update(key for key in ("name", "number", "position", "assigned_role", "eligible") if player.get(key) != old.get(key))
            if preserved:
                overrides["players"][player_id] = sorted(preserved)
            review_required = source.get("source", {}).get("review_required", False)
            if review_required:
                warnings.append(f"{player['name']}: source entry needs review; confirmed local name, jersey, role and eligibility are retained.")
            for key in ("name", "number", "position", "assigned_role"):
                if key in source and key not in preserved and not review_required and player.get(key) == old.get(key):
                    if key in {"position", "assigned_role"} and source.get("source", {}).get("position_unresolved"):
                        warnings.append(f"{player['name']}: source role is unresolved; the confirmed local role is retained.")
                        continue
                    candidate = source[key]
                    if key == "number" and any(p["id"] != player_id and p["team_id"] == player["team_id"] and p["eligible"] and p["number"] == candidate for p in state["players"]):
                        warnings.append(f"{player['name']}: source jersey #{candidate} conflicts with the local roster; existing number retained.")
                    else:
                        player[key] = candidate
            player["source"] = deepcopy(source.get("source", {}))
            player["source_missing"] = False
        else:
            added = deepcopy(source)
            added["location"] = "BENCH"
            if any(p["team_id"] == added["team_id"] and p["eligible"] and p["number"] == added["number"] for p in state["players"]):
                added["eligible"] = False
                warnings.append(f"{added['name']}: added as unavailable because the jersey number conflicts; review in match setup.")
            state["players"].append(added)
            state["_initial_locations"][player_id] = "BENCH"
    for player_id in previous_players.keys() - incoming.keys():
        if player_id in current:
            current[player_id]["source_missing"] = True
            warnings.append(f"{current[player_id]['name']} is no longer in the source roster; the local record, location and statistics are retained.")
    if restore_lines:
        state["lines"] = deepcopy(dto["lines"])
    if state["game"]["name"] != previous["game"]["name"]:
        overrides["game"] = sorted(set(overrides["game"]) | {"name"})
    if "name" not in overrides["game"] and state["game"]["name"] == previous["game"]["name"]:
        state["game"]["name"] = dto["game"]["name"]
    old_teams = {t["id"]: t for t in previous["teams"]}
    new_teams = {t["id"]: t for t in dto["teams"]}
    for team in state["teams"]:
        preserved = set(overrides["teams"].get(team["id"], []))
        preserved.update(field for field in ("name", "short_name", "color") if team.get(field) != old_teams[team["id"]].get(field))
        if preserved:
            overrides["teams"][team["id"]] = sorted(preserved)
        for field in ("name", "short_name", "color"):
            if field not in preserved and team.get(field) == old_teams[team["id"]].get(field):
                team[field] = new_teams[team["id"]].get(field, team.get(field))
    changes = source_changes(state, dto)
    state["game"]["external_status"] = dto["game"].get("external_status")
    state["game"]["stadium"] = dto["game"].get("stadium")
    state["integration"].update({"latest_snapshot": deepcopy(dto), "external_status": dto["game"].get("external_status"), "last_successful_refresh_ms": timestamp, "warnings": warnings, "changes": changes})
    state["events"].append({"id": str(uuid4()), "type": "source.refresh", "payload": {"restore_lines": restore_lines, "changes": changes}, "at_playing_ms": state["clock"]["playing_ms"], "period": state["clock"]["period"], "remaining_ms": state["clock"]["remaining_ms"], "active": True, "actor": "source-refresh", "created_at_ms": timestamp})
    # Rebuild without importing live locations or creating fictitious past shifts.
    state = domain.apply_action(state, {"type": "setup", "payload": {}, "actor": "source-refresh"}, timestamp)
    return state


class ImportService:
    def __init__(self, store):
        self.store = store

    def receipt(self, game_id: str, body: dict, principal) -> dict | None:
        require_supervisor(principal)
        with self.store.session() as session:
            receipt = session.get(ReceiptRow, (game_id, body["request_id"]))
            if receipt is None:
                return None
            action_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if receipt.actor != principal.actor or receipt.fingerprint != action_hash:
                raise StoreError(409, "Request ID was already used for another command")
            return deepcopy(receipt.response)

    def import_game(self, dto: dict, body: dict, principal) -> dict:
        require_supervisor(principal)
        game_id = game_id_for(dto["external_game_id"])
        if body.get("preview_fingerprint") != fingerprint(dto):
            raise StoreError(409, "Source preview changed. Fetch and review the latest roster before importing.")
        timestamp = now_ms()
        try:
            with self.store.lock(game_id), self.store.session.begin() as session:
                existing = session.get(GameRow, game_id)
                if existing:
                    return public_snapshot(existing.state)
                state = make_state(dto, {**body, "actor": principal.actor}, timestamp)
                row = GameRow(id=game_id, name=state["game"]["name"], status=state["game"]["status"], external_id=state["game"]["external_id"], version=state["version"], state=state)
                session.add(row)
                session.flush()
                self.store._project(session, state)
                if stream_enabled():
                    self.store._ensure_journal(session, row, timestamp, "roster.imported", actor=principal.actor,
                                               command={"type": "source.import", "external_game_id": dto["external_game_id"],
                                                        "preview_fingerprint": fingerprint(dto), "adjustments": body})
                session.add(AuditRow(game_id=game_id, version=state["version"], actor=principal.actor, request_id=f"import-{game_id}", created_at_ms=timestamp, command={"type": "source.import", "external_game_id": dto["external_game_id"], "preview_fingerprint": fingerprint(dto), "adjustments": body}, previous_values={}, new_values={"source": PROVIDER, "roster_count": len(state["players"]), "coverage": state["game"]["coverage"]}))
                return public_snapshot(row.state, timestamp)
        except domain.DomainError as error:
            raise StoreError(error.status_code, str(error)) from error
        except (IntegrityError, OperationalError) as error:
            raise StoreError(409, "Import conflicted with an existing identity or concurrent import; reload the saved match") from error

    def refresh(self, game_id: str, dto: dict, body: dict, principal) -> dict:
        require_supervisor(principal)
        timestamp = now_ms()
        try:
            with self.store.lock(game_id), self.store.session.begin() as session:
                row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
                if not row:
                    raise StoreError(404, "Game not found")
                if not row.state.get("integration") or row.state["integration"]["external_game_id"] != dto["external_game_id"]:
                    raise StoreError(409, "This game is not linked to the requested source")
                action_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                receipt = session.get(ReceiptRow, (game_id, body["request_id"])) if body["confirm"] else None
                if receipt:
                    if receipt.actor != principal.actor or receipt.fingerprint != action_hash:
                        raise StoreError(409, "Request ID was already used for another refresh")
                    return receipt.response
                if row.version != body["expected_version"]:
                    raise StoreError(409, "Match changed while reviewing the source. Review the latest differences again.")
                if not body["confirm"]:
                    return {"version": row.version, "preview_fingerprint": fingerprint(dto), "changes": source_changes(row.state, dto), "warnings": dto.get("warnings", [])}
                if body.get("preview_fingerprint") != fingerprint(dto):
                    raise StoreError(409, "Source changed after preview; fetch and review the differences again")
                if stream_enabled():
                    self.store._ensure_journal(session, row, timestamp)
                stored_before = deepcopy(row.state)
                previous_version = row.version
                previous_fingerprint = fingerprint(row.state["integration"]["latest_snapshot"])
                state = reconcile(row.state, dto, body.get("restore_lines", False), timestamp)
                state["version"] = previous_version + 1
                result = public_snapshot(state, timestamp)
                changed = session.execute(update(GameRow).where(GameRow.id == game_id, GameRow.version == previous_version).values(state=state, version=state["version"], name=state["game"]["name"]))
                if changed.rowcount != 1:
                    raise StoreError(409, "Concurrent match update; review the source again")
                self.store._project(session, state)
                session.add(AuditRow(game_id=game_id, version=state["version"], actor=principal.actor, request_id=body["request_id"], created_at_ms=timestamp, command={"type": "source.refresh", **body}, previous_values={"source_fingerprint": previous_fingerprint}, new_values={"source_fingerprint": fingerprint(dto), "changes": state["integration"]["changes"]}))
                cursor = append_change(session, state, stored_before, {"type": "source.refresh", **body}, principal.actor, body["request_id"], timestamp)
                session.add(ReceiptRow(game_id=game_id, request_id=body["request_id"], actor=principal.actor, fingerprint=action_hash, response=result, journal_cursor=cursor))
                return result
        except domain.DomainError as error:
            raise StoreError(error.status_code, str(error)) from error
        except (IntegrityError, OperationalError) as error:
            raise StoreError(409, "Roster refresh conflicted with another write; no partial changes were saved") from error
