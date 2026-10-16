"""Additive v2 application service; all calculation remains in the domain."""
from copy import deepcopy
import hashlib
import json
import os
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from . import domain
from .journal import (PAGE_MAX_BYTES, append_change, bounds, encode_cursor,
                      live_projection, manual_authority, validate_cursor)
from .models import AuditRow, ChangeRow, GameRow, JournalRow, ReceiptRow
from .schemas import Action

ACTION_VALIDATOR = TypeAdapter(Action)


def _hash(action):
    return hashlib.sha256(json.dumps(action, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ServiceStoreMixin:
    def _ensure_journal(self, session, row, at, kind="integration.activated", actor="system", command=None):
        from .store import constrain_external_clock
        journal = session.get(JournalRow, row.id)
        if journal is not None:
            return journal
        state = deepcopy(row.state)
        legacy_observation = state.get("external_clock_observation") if "clock_authority" not in state else None
        state = constrain_external_clock(state, at)[0]
        if legacy_observation is not None:
            # Activation must not remove a legacy feed's stale cap and turn an
            # unreported wall-clock gap into playing time. Even if global source
            # settings changed during deployment, the saved observation still
            # bounds the formerly external owner's credited interval.
            cutoff = legacy_observation["observed_at_ms"] + int(os.getenv("TOI_CLOCK_STALE_MS", "10000"))
            state = domain.materialize_clock(state, min(at, max(state["clock"]["anchor_unix_ms"], cutoff)))
            state["clock"]["running"] = False
        state = domain.materialize_clock(state, at)
        state.setdefault("clock_authority", manual_authority())
        state.pop("external_clock_observation", None)
        state["clock"]["source"] = "manual"
        row.state = state
        journal = JournalRow(game_id=row.id, generation=str(uuid4()), high_water_seq=0, oldest_seq=1)
        session.add(journal)
        session.flush()
        action = command or {"type": kind, "coverage": {"journal_from_version": row.version, "prior_history": "v1"}}
        append_change(session, state, None if kind in {"game.created", "roster.imported"} else state,
                      action, actor, None, at, kind)
        return journal

    def activate_stream(self, game_id: str) -> dict:
        from .store import StoreError, now_ms
        with self.lock(game_id), self.session.begin() as session:
            row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
            if row is None:
                raise StoreError(404, "Game not found", "GAME_NOT_FOUND")
            return bounds(self._ensure_journal(session, row, now_ms()))

    def stream_games(self) -> list[str]:
        with self.session() as session:
            return list(session.scalars(select(JournalRow.game_id)))

    def journal_bounds(self, game_id: str) -> dict:
        return self.activate_stream(game_id)

    def live(self, game_id: str, at: int | None = None) -> dict:
        from .store import StoreError, now_ms
        timestamp = now_ms() if at is None else at
        with self.lock(game_id), self.session.begin() as session:
            row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
            if row is None:
                raise StoreError(404, "Game not found", "GAME_NOT_FOUND")
            journal = self._ensure_journal(session, row, timestamp)
            return {"baseline_cursor": bounds(journal)["high_water_cursor"], "sampled_at_ms": timestamp,
                    "state": live_projection(row.state, timestamp)}

    def journal_page(self, game_id: str, after=None, through=None, limit=100) -> dict:
        from .store import StoreError, now_ms
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StoreError(422, "Journal page limit must be between 1 and 1000", "CURSOR_INVALID")
        with self.lock(game_id), self.session.begin() as session:
            row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
            if row is None:
                raise StoreError(404, "Game not found", "GAME_NOT_FOUND")
            journal = self._ensure_journal(session, row, now_ms())
            start = journal.oldest_seq - 1 if after is None else validate_cursor(after, journal)
            target = journal.high_water_seq if through is None else validate_cursor(through, journal)
            if target < start:
                raise StoreError(422, "Journal through cursor precedes after cursor", "CURSOR_INVALID", bounds(journal))
            records, byte_count = [], 1024
            query = select(ChangeRow).where(ChangeRow.game_id == game_id, ChangeRow.journal_seq > start,
                                            ChangeRow.journal_seq <= target).order_by(ChangeRow.journal_seq).limit(limit)
            for change in session.scalars(query.execution_options(yield_per=1, stream_results=True)):
                size = len(json.dumps(change.record, separators=(",", ":"), ensure_ascii=False).encode()) + 1
                if byte_count + size > PAGE_MAX_BYTES:
                    if not records:
                        raise StoreError(413, "Journal record exceeds page response limit", "JOURNAL_UNAVAILABLE")
                    break
                records.append(deepcopy(change.record))
                byte_count += size
            position = records[-1]["journal_seq"] if records else start
            return {"records": records, "next_cursor": encode_cursor(game_id, journal.generation, position),
                    "through_cursor": encode_cursor(game_id, journal.generation, target), "has_more": position < target}

    def rotate_journal(self, game_id: str, at: int | None = None) -> dict:
        """Offline restore tooling must call this before reopening service access."""
        from .store import StoreError, constrain_external_clock, now_ms
        timestamp = now_ms() if at is None else at
        with self.lock(game_id), self.session.begin() as session:
            row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
            if row is None:
                raise StoreError(404, "Game not found", "GAME_NOT_FOUND")
            session.execute(delete(ChangeRow).where(ChangeRow.game_id == game_id))
            session.execute(delete(JournalRow).where(JournalRow.game_id == game_id))
            session.flush()
            # Fence a restored old bridge checkpoint as well as old journal cursors.
            state = domain.materialize_clock(constrain_external_clock(row.state, timestamp)[0], timestamp)
            state["clock"]["running"] = False
            state["clock"]["source"] = "manual"
            state["clock_authority"] = manual_authority(max(state.get("clock_authority", {}).get("generation", 0) + 1, timestamp))
            row.state = state
            return bounds(self._ensure_journal(session, row, timestamp))

    def _service_authorize(self, before, action, principal):
        from .store import StoreError
        permission = {"clock.observe": "clock:write", "clock.authority.set": "clock:authority",
                      "clock": "clock:control", "setup": "setup:write", "move": "players:write",
                      "substitute": "players:write", "undo": "history:write", "correct": "history:write"}[action["type"]]
        if permission not in principal.permissions:
            raise StoreError(403, "Service lacks permission for this command", "FORBIDDEN")
        if action["type"] in {"clock", "setup", "clock.authority.set"}:
            if principal.team_ids is not None:
                raise StoreError(403, "This command requires a match supervisor", "FORBIDDEN")
        elif action["type"] != "clock.observe":
            # Completed undo/correction retries still need present-day team
            # authorization, but their target is deliberately inactive now.
            # Resolve the original committed target rather than validating the
            # mutation again before its idempotency receipt is considered.
            committed = next((event for event in before["events"] if event.get("request_id") == action.get("request_id")), None)
            if action["type"] in {"undo", "correct"} and committed is not None and principal.team_ids is not None:
                target = next((event for event in before["events"] if event["id"] == committed.get("target_event_id")), None)
                if target is None:
                    raise StoreError(403, "Cannot establish the historical command's team scope", "FORBIDDEN")
                self._authorize(before, {"type": target["type"], "payload": target["payload"]}, principal)
                if action["type"] == "correct":
                    self._authorize(before, action["payload"]["replacement"], principal)
            else:
                self._authorize(before, action, principal)
        if action["type"] == "clock.observe":
            sources = getattr(principal, "clock_sources", None)
            if sources is not None and action["payload"]["source"] not in sources and "*" not in sources:
                raise StoreError(403, "Clock source is outside the service allowlist", "FORBIDDEN")

    def _save_service(self, session, row, before, state, action, principal, timestamp):
        from .store import StoreError
        previous_version = row.version
        state["version"] = previous_version + 1
        changed = session.execute(update(GameRow).where(GameRow.id == row.id, GameRow.version == previous_version)
                                  .values(state=state, version=state["version"], name=state["game"]["name"],
                                          status=state["game"]["status"], external_id=state["game"].get("external_id"))
                                  .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise StoreError(409, "Concurrent update; resynchronise and retry", "VERSION_CONFLICT")
        # Clock-only observations never rewrite all roster/shift/event rows.
        if action["type"] not in {"clock.observe", "clock.authority.set", "clock.feed.stale"}:
            self._project(session, state)
        cursor = append_change(session, state, before, action, principal.actor, action.get("request_id"), timestamp)
        session.add(AuditRow(game_id=row.id, version=state["version"], actor=principal.actor,
                             request_id=action.get("request_id") or f"watchdog-{uuid4()}", created_at_ms=timestamp,
                             command=action, previous_values={"clock": before["clock"], "clock_authority": before.get("clock_authority"),
                                                              "locations": {p["id"]: p["location"] for p in before["players"]} if action["type"] not in {"clock.observe", "clock.authority.set", "clock.feed.stale"} else {}},
                             new_values={"clock": state["clock"], "clock_authority": state.get("clock_authority"),
                                         "locations": {p["id"]: p["location"] for p in state["players"]} if action["type"] not in {"clock.observe", "clock.authority.set", "clock.feed.stale"} else {}}))
        return cursor

    def _receipt_available(self, session, cursor, game_id):
        if not cursor:
            return False
        from .store import StoreError
        journal = session.get(JournalRow, game_id)
        if journal is None:
            return False
        try:
            sequence = validate_cursor(cursor, journal)
        except StoreError:
            return False
        return session.get(ChangeRow, (game_id, sequence)) is not None

    def apply_service(self, game_id: str, data: dict, principal, at: int | None = None) -> tuple[dict, bool]:
        from .store import StoreError, constrain_external_clock, now_ms
        timestamp = now_ms() if at is None else at
        data = deepcopy(data)
        if "command" in data:
            action = {**data["command"], "request_id": data["request_id"], "expected_version": data["expected_version"], "actor": principal.actor}
            if action["type"] != "clock.authority.set":
                try:
                    action = ACTION_VALIDATOR.validate_python(action).model_dump(exclude_none=True)
                except ValidationError as error:
                    raise StoreError(422, "Command does not match the v1 command contract", "INVALID_COMMAND") from error
        else:
            action = {"type": "clock.observe", "payload": {key: value for key, value in data.items() if key not in {"request_id", "expected_version"}},
                      "request_id": data["request_id"], "expected_version": data["expected_version"], "actor": principal.actor}
        fingerprint = _hash(action)
        try:
            with self.lock(game_id), self.session.begin() as session:
                row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
                if row is None:
                    raise StoreError(404, "Game not found", "GAME_NOT_FOUND")
                self._ensure_journal(session, row, timestamp)
                self._service_authorize(row.state, action, principal)
                receipt = session.get(ReceiptRow, (game_id, action["request_id"]))
                if receipt:
                    if receipt.fingerprint != fingerprint or receipt.actor != principal.actor or (receipt.service_id is not None and receipt.service_id != principal.service_id):
                        raise StoreError(409, "Request ID already belongs to a different command or identity", "IDEMPOTENCY_CONFLICT")
                    version = receipt.response.get("committed_version", receipt.response.get("version"))
                    return {"request_id": action["request_id"], "committed_version": version, "current_version": row.version,
                            "journal_cursor": receipt.journal_cursor, "duplicate": True,
                            "journal_available": self._receipt_available(session, receipt.journal_cursor, game_id)}, False
                if row.version != action["expected_version"]:
                    raise StoreError(409, "Game changed; resynchronise before a new command", "VERSION_CONFLICT", {"current_version": row.version})
                stored_before = deepcopy(row.state)
                before = constrain_external_clock(stored_before, timestamp)[0]
                if action["type"] == "clock.observe":
                    state = self._observe_clock(before, action["payload"], principal, timestamp)
                elif action["type"] == "clock.authority.set":
                    state = self._set_authority(before, action["payload"], timestamp)
                else:
                    state = self._mutate_domain(before, action, timestamp)
                cursor = self._save_service(session, row, stored_before, state, action, principal, timestamp)
                response = {"request_id": action["request_id"], "committed_version": state["version"], "current_version": state["version"],
                            "journal_cursor": cursor, "duplicate": False, "journal_available": cursor is not None}
                session.add(ReceiptRow(game_id=game_id, request_id=action["request_id"], actor=principal.actor,
                                       service_id=principal.service_id, fingerprint=fingerprint, response=response, journal_cursor=cursor))
                return response, True
        except domain.DomainError as error:
            raise StoreError(error.status_code, str(error), "INVALID_COMMAND") from error
        except (IntegrityError, OperationalError) as error:
            raise StoreError(409, "Concurrent database write; retry the same request ID", "VERSION_CONFLICT") from error

    def _set_authority(self, before, payload, timestamp):
        from .store import StoreError
        mode = payload.get("mode")
        if mode not in {"manual", "external"} or (mode == "manual" and (payload.get("source") is not None or payload.get("service_id") is not None)):
            raise StoreError(422, "Invalid clock authority payload", "INVALID_COMMAND")
        if mode == "external" and any(not isinstance(payload.get(key), str) or not 1 <= len(payload[key]) <= 100 for key in ("source", "service_id")):
            raise StoreError(422, "External ownership requires a source and service ID", "INVALID_COMMAND")
        state = domain.materialize_clock(before, timestamp)
        state["clock"]["running"] = False
        state["clock"]["source"] = payload["source"] if mode == "external" else "manual"
        state["clock_authority"] = {**manual_authority(before["clock_authority"]["generation"] + 1),
                                    "mode": mode, "source": payload.get("source"), "service_id": payload.get("service_id"),
                                    "feed_status": "stale" if mode == "external" else "manual"}
        state.pop("external_clock_observation", None)
        return state

    def _observe_clock(self, before, payload, principal, timestamp):
        from .store import StoreError
        authority = before["clock_authority"]
        if authority["mode"] != "external" or payload["authority_generation"] != authority["generation"]:
            raise StoreError(409, "Clock ownership generation changed", "AUTHORITY_CONFLICT")
        if authority["service_id"] != principal.service_id or payload["source"] != authority["source"]:
            raise StoreError(403, "Service is not the active clock owner", "CLOCK_NOT_OWNER")
        if payload["source_sequence"] <= authority["last_source_sequence"] or payload["observed_at_ms"] <= (authority["observed_at_ms"] if authority["observed_at_ms"] is not None else -1):
            raise StoreError(409, "Clock sequence or observation time is out of order", "CLOCK_OUT_OF_ORDER")
        stale_ms = int(os.getenv("TOI_STREAM_CLOCK_STALE_MS", "3000"))
        age = timestamp - payload["observed_at_ms"]
        if age < -2000:
            raise StoreError(422, "Clock observation is from the future", "CLOCK_FUTURE")
        if age >= stale_ms:
            raise StoreError(422, "Clock observation is stale", "CLOCK_STALE")
        state = domain.materialize_clock(before, timestamp)
        # Reuse the established clock validation/correction primitive once. No
        # fabricated pause/set/start event triple or growing full-state receipt.
        domain._clock_action(state, {"operation": "set", "period": payload["period"], "remaining_ms": payload["remaining_ms"]})
        state["clock"]["running"] = False
        if payload["running"]:
            domain._clock_action(state, {"operation": "start"})
        state["clock"]["source"] = payload["source"]
        state["clock_authority"] = {**authority, "last_source_sequence": payload["source_sequence"], "feed_status": "fresh",
                                    "observed_at_ms": payload["observed_at_ms"], "stale_at_ms": payload["observed_at_ms"] + stale_ms}
        return state

    def expire_clock(self, game_id: str, at: int | None = None) -> tuple[dict | None, bool]:
        from types import SimpleNamespace
        from .store import StoreError, constrain_external_clock, now_ms, public_snapshot
        timestamp = now_ms() if at is None else at
        with self.lock(game_id), self.session.begin() as session:
            row = session.scalar(select(GameRow).where(GameRow.id == game_id).with_for_update())
            if row is None:
                raise StoreError(404, "Game not found", "GAME_NOT_FOUND")
            authority = row.state.get("clock_authority", {})
            checkpoint = [authority.get("generation"), authority.get("last_source_sequence")]
            if authority.get("mode") != "external" or authority.get("stale_at_ms") is None or timestamp < authority["stale_at_ms"] or row.state.get("_clock_stale_checkpoint") == checkpoint:
                return None, False
            before = deepcopy(row.state)
            state = constrain_external_clock(before, timestamp)[0]
            state["clock_authority"]["feed_status"] = "stale"
            state["_clock_stale_checkpoint"] = checkpoint
            state["clock"]["running"] = False
            action = {"type": "clock.feed.stale", "payload": {"authority_generation": authority["generation"], "stale_at_ms": authority["stale_at_ms"]}}
            self._save_service(session, row, before, state, action, SimpleNamespace(actor="clock-watchdog"), timestamp)
            return public_snapshot(state, timestamp), True
