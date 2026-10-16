"""Transactional outbox and bounded live projections for the service interface.

Every writer calls append_change using its existing database transaction. A
notification is only a wake-up hint: committed rows are the delivery authority.
"""
import base64
from copy import deepcopy
import json
import os
from uuid import UUID, uuid4

from sqlalchemy import delete, select

from .models import ChangeRow, JournalRow

MAX_INTEGER = 9_007_199_254_740_991
PAGE_MAX_BYTES = 8 * 1024 * 1024


def stream_enabled() -> bool:
    return os.getenv("TOI_STREAM_ENABLED", "false").lower() in {"true", "1", "yes"}


def manual_authority(generation=0) -> dict:
    return {"mode": "manual", "generation": generation, "source": None,
            "service_id": None, "last_source_sequence": 0, "feed_status": "manual",
            "observed_at_ms": None, "stale_at_ms": None}


def encode_cursor(game_id: str, generation: str, sequence: int) -> str:
    raw = json.dumps([1, game_id, generation, sequence], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str, int]:
    from .store import StoreError
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValueError
        values = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError
        version, game_id, generation, sequence = values
        if type(version) is not int or version != 1 or not isinstance(game_id, str) or not 1 <= len(game_id) <= 100:
            raise ValueError
        if not isinstance(generation, str) or str(UUID(generation)) != generation:
            raise ValueError
        if type(sequence) is not int or not 0 <= sequence <= MAX_INTEGER:
            raise ValueError
        if encode_cursor(game_id, generation, sequence) != cursor:
            raise ValueError
        return game_id, generation, sequence
    except (ValueError, TypeError, UnicodeError):
        raise StoreError(422, "Malformed journal cursor", "CURSOR_INVALID") from None


def bounds(row: JournalRow) -> dict:
    return {"journal_generation": row.generation,
            "oldest_cursor": encode_cursor(row.game_id, row.generation, row.oldest_seq - 1),
            "high_water_cursor": encode_cursor(row.game_id, row.generation, row.high_water_seq)}


def validate_cursor(cursor: str, row: JournalRow) -> int:
    from .store import StoreError
    game_id, generation, sequence = decode_cursor(cursor)
    detail = bounds(row)
    if game_id != row.game_id:
        raise StoreError(409, "Cursor belongs to another game", "CURSOR_WRONG_GAME", detail)
    if generation != row.generation:
        raise StoreError(409, "Journal generation changed; explicitly choose a new baseline", "CURSOR_WRONG_GENERATION", detail)
    if sequence > row.high_water_seq:
        raise StoreError(409, "Cursor is ahead of this journal", "CURSOR_AHEAD", detail)
    if sequence < row.oldest_seq - 1:
        raise StoreError(409, "Cursor has expired from retained history", "CURSOR_EXPIRED", detail)
    return sequence


def live_projection(state: dict, at: int, sample_seq: int = 0) -> dict:
    from .store import public_snapshot
    projected = public_snapshot(state, at)
    result = {key: projected[key] for key in ("version", "game", "teams", "players", "lines", "clock", "warnings", "strengths")}
    # Timing period spans are a historical implementation detail, not part of the
    # roster-sized live projection. Public aggregate totals remain available.
    result["clock"].pop("period_spans", None)
    result["clock_authority"] = projected.get("clock_authority", manual_authority())
    result["sampled_at_ms"], result["sample_seq"] = at, sample_seq
    result["on_ice"] = {
        side: [p["id"] for p in projected["players"] if p["team_id"] == team["id"] and p["location"] == "ON_ICE"]
        for side in ("home", "away") for team in projected["teams"] if team["side"] == side
    }
    return result


def _entities(state: dict | None) -> dict:
    if state is None:
        return {}
    result = {"game": {state["game"]["id"]: state["game"]},
              "clock": {"clock": state["clock"]},
              "clock_authority": {"clock_authority": state.get("clock_authority", manual_authority())}}
    for singular, plural in (("team", "teams"), ("player", "players"), ("line", "lines"), ("shift", "shifts"), ("event", "events")):
        result[singular] = {entity["id"]: entity for entity in state[plural]}
    result["location"] = {p["id"]: {"player_id": p["id"], "team_id": p["team_id"], "location": p["location"]} for p in state["players"]}
    if state.get("integration"):
        result["integration"] = {"source": state["integration"]}
    return result


def changes_between(before: dict | None, after: dict) -> list[dict]:
    previous, current = _entities(before), _entities(after)
    changes = []
    for entity in sorted(previous.keys() | current.keys()):
        old, new = previous.get(entity, {}), current.get(entity, {})
        for identity in sorted(old.keys() | new.keys()):
            if old.get(identity) != new.get(identity):
                changes.append({"entity": entity, "id": identity,
                                "operation": "upsert" if identity in new else "delete",
                                "before": deepcopy(old.get(identity)), "after": deepcopy(new.get(identity))})
    return changes


def record_kind(action: dict, before: dict) -> str:
    kind, payload = action["type"], action.get("payload", {})
    if kind == "move":
        player = next((p for p in before["players"] if p["id"] == payload.get("player_id")), {})
        return "penalty.location.updated" if "PENALTY_BOX" in {player.get("location"), payload.get("to")} else "player.location.updated"
    if kind == "substitute":
        return "substitution.group.updated" if max(len(payload.get("outgoing_ids", [])), len(payload.get("incoming_ids", []))) > 1 else "substitution.updated"
    return {"clock": "clock.updated", "external_clock": "clock.observed", "clock.observe": "clock.observed",
            "clock.authority.set": "clock.authority.updated", "clock.feed.stale": "clock.feed.stale",
            "setup": "setup.updated", "undo": "correction.updated", "correct": "correction.updated",
            "source.refresh": "roster.refreshed"}[kind]


def append_change(session, state: dict, before: dict | None, action: dict | None,
                  actor: str, request_id: str | None, at: int, kind: str | None = None) -> str | None:
    """Append only for activated games; called before the writer commits."""
    from .store import StoreError
    game_id = state["game"]["id"]
    journal = session.get(JournalRow, game_id)
    if journal is None:
        return None
    if journal.high_water_seq >= MAX_INTEGER:
        raise StoreError(503, "Journal counter exhausted; rotate generation before accepting writes", "JOURNAL_UNAVAILABLE")
    sequence = journal.high_water_seq + 1
    cursor = encode_cursor(game_id, journal.generation, sequence)
    record = {"event_id": str(uuid4()), "journal_generation": journal.generation,
              "journal_seq": sequence, "cursor": cursor, "version": state["version"],
              "kind": kind or record_kind(action, before), "request_id": request_id,
              "actor": actor, "recorded_at_ms": at, "command": deepcopy(action),
              "changes": changes_between(before, state), "live_state": live_projection(state, at)}
    # A record is indivisible on the wire. Roll back the mutation rather than
    # commit a state that cannot ever be replayed by a conforming subscriber.
    if len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()) > 1_000_000:
        raise StoreError(413, "Committed record would exceed the service message limit", "MESSAGE_TOO_LARGE")
    session.add(ChangeRow(game_id=game_id, journal_seq=sequence, recorded_at_ms=at, record=record))
    journal.high_water_seq = sequence
    session.flush()
    retention_days = int(os.getenv("TOI_STREAM_JOURNAL_RETENTION_DAYS", "7"))
    cutoff = at - retention_days * 86_400_000
    # Preserve contiguous suffixes and the current tail even after an idle week.
    oldest = session.scalar(select(ChangeRow.journal_seq).where(ChangeRow.game_id == game_id, ChangeRow.recorded_at_ms >= cutoff).order_by(ChangeRow.journal_seq).limit(1))
    if oldest is None:
        oldest = sequence
    session.execute(delete(ChangeRow).where(ChangeRow.game_id == game_id, ChangeRow.journal_seq < oldest))
    journal.oldest_seq = oldest
    return cursor
