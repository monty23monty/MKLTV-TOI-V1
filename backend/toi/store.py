"""Transactional application service; no UI state or timing rules live here."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from . import domain
from .database import make_engine, sessions
from .models import AuditRow, EventRow, GameRow, LineRow, LocationRow, PersonIdentityRow, PlayerRow, ReceiptRow, RosterRow, ShiftRow, TeamRow
from .seed import create_demo_state


class StoreError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def public_snapshot(state: dict, at: int | None = None) -> dict:
    at = now_ms() if at is None else at
    state, feed_stale = constrain_external_clock(state, at)
    result = domain.snapshot(state, at)
    for event in result["events"]:
        event["playing_ms"] = event.get("at_playing_ms", 0)
        event["voided"] = not event.get("active", True)
    for shift in result["shifts"]:
        shift["start_ms"] = shift.get("start_playing_ms", 0)
        shift["end_ms"] = shift.get("end_playing_ms")
    result["server_time_ms"] = at
    if feed_stale:
        result["warnings"].append("Authoritative clock feed is stale; playing time is paused until a fresh observation arrives.")
    result["warnings"].extend(state.get("integration", {}).get("warnings", []))
    return result


def constrain_external_clock(state: dict, at: int) -> tuple[dict, bool]:
    """Cap interpolation if an external provider stops reporting."""
    import os
    observation = state.get("external_clock_observation")
    if not observation or os.getenv("TOI_CLOCK_SOURCE", "manual") == "manual":
        return state, False
    cutoff = observation["observed_at_ms"] + int(os.getenv("TOI_CLOCK_STALE_MS", "10000"))
    if at <= cutoff:
        return state, False
    state = domain.materialize_clock(state, max(state["clock"]["anchor_unix_ms"], cutoff))
    state["clock"]["running"] = False
    return state, True


class Store:
    def __init__(self, url: str):
        self.engine = make_engine(url)
        self.session = sessions(self.engine)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def lock(self, game_id: str):
        with self._locks_guard:
            return self._locks.setdefault(game_id, threading.RLock())

    def migrate(self):
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def list_games(self) -> list[dict]:
        with self.session() as session:
            rows = session.scalars(select(GameRow).order_by(GameRow.id)).all()
            return [dict(row.state["game"], version=row.version) for row in rows]

    def get(self, game_id: str) -> dict:
        with self.session() as session:
            row = session.get(GameRow, game_id)
            if row is None:
                raise StoreError(404, "Game not found")
            return deepcopy(row.state)

    def create(self, game_id: str | None = None, **details) -> dict:
        game_id = game_id or str(uuid4())
        with self.lock(game_id), self.session.begin() as session:
            existing = session.get(GameRow, game_id)
            if existing:
                return public_snapshot(existing.state)
            state = create_demo_state(game_id=game_id, now_ms=now_ms(), **details)
            # A snapshot initializes the domain's rebuildable initial-shift projection.
            state = domain.materialize_clock(state, now_ms())
            session.add(GameRow(id=game_id, name=state["game"]["name"], status=state["game"]["status"], external_id=state["game"].get("external_id"), version=state["version"], state=state))
            session.flush()
            self._project(session, state)
            return public_snapshot(state)

    def _project(self, session, state: dict):
        game_id = state["game"]["id"]
        # These are bounded match-sized projections; history and receipts remain immutable.
        for model in (TeamRow, RosterRow, LocationRow, ShiftRow, LineRow, EventRow):
            session.execute(delete(model).where(model.game_id == game_id))
        for team in state["teams"]:
            session.add(TeamRow(game_id=game_id, id=team["id"], name=team["name"], external_id=team.get("external_id"), data=team))
        for player in state["players"]:
            person_id = player.get("person_id")
            if person_id:
                person = session.get(PersonIdentityRow, person_id)
                if person is None:
                    session.add(PersonIdentityRow(id=person_id, provider="hokejovy-zapis", external_player_id=str(player["external_player_id"]), name=player["name"]))
                    session.flush()
                elif person.external_player_id != str(player["external_player_id"]):
                    raise StoreError(409, "Persistent external player identity changed; reconciliation required")
            existing = session.get(PlayerRow, player["id"])
            if existing is None:
                session.add(PlayerRow(id=player["id"], name=player["name"], external_id=player.get("external_id"), default_position=player["position"], person_id=person_id))
            else:
                if existing.person_id and existing.person_id != person_id:
                    raise StoreError(409, "Match roster identity changed; reconciliation required")
                existing.name = player["name"]
                existing.external_id = player.get("external_id")
                existing.default_position = player["position"]
                existing.person_id = person_id
        session.flush()
        for player in state["players"]:
            session.add(RosterRow(game_id=game_id, player_id=player["id"], team_id=player["team_id"], number=player["number"], position=player["position"], eligible=player["eligible"], external_match_player_id=player.get("match_player_id"), source_data=player.get("source")))
            session.add(LocationRow(game_id=game_id, player_id=player["id"], location=player["location"], last_event_id=player.get("last_event_id")))
        projected = public_snapshot(state)
        for shift in projected["shifts"]:
            session.add(ShiftRow(game_id=game_id, id=shift["id"], player_id=shift["player_id"], team_id=shift["team_id"], start_playing_ms=shift["start_playing_ms"], end_playing_ms=shift.get("end_playing_ms"), data=shift))
        for line in state["lines"]:
            session.add(LineRow(game_id=game_id, id=line["id"], team_id=line["team_id"], data=line))
        for event in state["events"]:
            session.add(EventRow(game_id=game_id, id=event["id"], type=event["type"], active=event.get("active", True), playing_ms=event.get("at_playing_ms", 0), data=event))

    def apply(self, game_id: str, action: dict, principal, at: int | None = None) -> tuple[dict, bool]:
        timestamp = now_ms() if at is None else at
        action = deepcopy(action)
        action["actor"] = principal.actor
        fingerprint = hashlib.sha256(json.dumps(action, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            with self.lock(game_id), self.session.begin() as session:
                row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
                if row is None:
                    raise StoreError(404, "Game not found")
                receipt = session.get(ReceiptRow, (game_id, action["request_id"]))
                if receipt:
                    if receipt.fingerprint != fingerprint or receipt.actor != principal.actor:
                        raise StoreError(409, "Request ID was already used for a different command")
                    return deepcopy(receipt.response), False
                if row.version != action["expected_version"]:
                    raise StoreError(409, "Game changed on another client. State resynchronisation required.")
                before, _stale = constrain_external_clock(deepcopy(row.state), timestamp)
                self._authorize(before, action, principal)
                if action["type"] == "external_clock":
                    state = self._external_clock(before, action, timestamp)
                else:
                    state = domain.apply_action(before, action, timestamp)
                    if action["type"] == "clock":
                        # An explicit supervisor command takes control immediately.
                        # A fresh observation from the configured source may resume provider control.
                        state.pop("external_clock_observation", None)
                        state["clock"]["source"] = "manual"
                if action["type"] == "setup":
                    for team in state["teams"]:
                        name = action["payload"].get("game", {}).get(f"{team['side']}_name")
                        if name:
                            team["name"] = str(name)[:100]
                    if state.get("integration"):
                        overrides = state["integration"].setdefault("operator_overrides", {"players": {}, "teams": {}, "game": []})
                        old_players = {p["id"]: p for p in before["players"]}
                        for player in state["players"]:
                            fields = set(overrides["players"].get(player["id"], []))
                            fields.update(key for key in ("name", "number", "position", "assigned_role", "eligible") if player.get(key) != old_players.get(player["id"], {}).get(key))
                            if fields:
                                overrides["players"][player["id"]] = sorted(fields)
                        old_teams = {t["id"]: t for t in before["teams"]}
                        for team in state["teams"]:
                            fields = set(overrides["teams"].get(team["id"], []))
                            fields.update(key for key in ("name", "short_name", "color") if team.get(key) != old_teams[team["id"]].get(key))
                            if fields:
                                overrides["teams"][team["id"]] = sorted(fields)
                        if state["game"]["name"] != before["game"]["name"]:
                            overrides["game"] = sorted(set(overrides["game"]) | {"name"})
                state["version"] = row.version + 1
                result = public_snapshot(state, timestamp)
                changed = session.execute(update(GameRow).where(GameRow.id == game_id, GameRow.version == row.version).values(state=state, version=state["version"], name=state["game"]["name"], status=state["game"]["status"], external_id=state["game"].get("external_id")))
                if changed.rowcount != 1:
                    raise StoreError(409, "Concurrent update; resynchronise and try again")
                self._project(session, state)
                session.add(AuditRow(game_id=game_id, version=state["version"], actor=principal.actor, request_id=action["request_id"], created_at_ms=timestamp, command=action, previous_values={"locations": {p["id"]: p["location"] for p in before["players"]}, "clock": before["clock"]}, new_values={"locations": {p["id"]: p["location"] for p in state["players"]}, "clock": state["clock"]}))
                session.add(ReceiptRow(game_id=game_id, request_id=action["request_id"], actor=principal.actor, fingerprint=fingerprint, response=result))
                return result, True
        except domain.DomainError as error:
            raise StoreError(error.status_code, str(error)) from error
        except (IntegrityError, OperationalError) as error:
            raise StoreError(409, "Concurrent database write; resynchronise and retry with the same request ID") from error

    def _authorize(self, state: dict, action: dict, principal):
        kind, payload = action["type"], action["payload"]
        if kind in {"clock", "external_clock", "setup"}:
            if not principal.can_control_clock or principal.team_ids is not None:
                raise StoreError(403, "This command requires a match supervisor")
            return
        if principal.team_ids is None:
            return
        if kind in {"undo", "correct"}:
            candidates = [e for e in state["events"] if e.get("active", True) and e["type"] in {"move", "substitute"}]
            target = next((e for e in candidates if e["id"] == payload.get("event_id")), None) if payload.get("event_id") else (candidates[-1] if candidates else None)
            if target is None:
                raise StoreError(422, "No movement available to correct")
            self._authorize(state, {"type": target["type"], "payload": target["payload"]}, principal)
            if kind == "correct":
                self._authorize(state, payload["replacement"], principal)
            return
        team = payload.get("team_id")
        if kind == "move":
            player = next((p for p in state["players"] if p["id"] == payload.get("player_id")), None)
            team = player["team_id"] if player else None
        if not isinstance(team, str) or team not in principal.team_ids:
            raise StoreError(403, "Operator is not assigned to this team")

    def _external_clock(self, before: dict, action: dict, timestamp: int):
        import os
        payload = action["payload"]
        source = os.getenv("TOI_CLOCK_SOURCE", "manual")
        if source == "manual" or payload["source"] != source:
            raise StoreError(409, "Clock source is not the configured authoritative provider")
        age = timestamp - payload["observed_at_ms"]
        if age < -2000 or age > int(os.getenv("TOI_CLOCK_STALE_MS", "10000")):
            raise StoreError(422, "Clock observation is stale or from the future")
        last = before.get("external_clock_observation", {})
        if payload["observed_at_ms"] <= last.get("observed_at_ms", -1):
            raise StoreError(409, "Clock observation is out of order")
        # First stop accrual at arrival, then correct the displayed clock. Never backfill a dropped feed.
        state = domain.apply_action(before, {"type": "clock", "payload": {"operation": "pause"}, "actor": action["actor"]}, timestamp)
        state = domain.apply_action(state, {"type": "clock", "payload": {"operation": "set", "period": payload["period"], "remaining_ms": payload["remaining_ms"]}, "actor": action["actor"]}, timestamp)
        if payload["running"]:
            state = domain.apply_action(state, {"type": "clock", "payload": {"operation": "start"}, "actor": action["actor"]}, timestamp)
        state["external_clock_observation"] = payload
        state["clock"]["source"] = payload["source"]
        return state

    def audit(self, game_id: str) -> list[dict]:
        self.get(game_id)
        with self.session() as session:
            return [{"version": r.version, "actor": r.actor, "request_id": r.request_id, "created_at_ms": r.created_at_ms, "command": r.command, "previous_values": r.previous_values, "new_values": r.new_values} for r in session.scalars(select(AuditRow).where(AuditRow.game_id == game_id).order_by(AuditRow.version))]
