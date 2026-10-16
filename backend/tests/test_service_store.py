"""Durable journal/clock ownership contracts, independent of socket delivery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from toi.adapters import Principal
from toi.import_service import ImportService, fingerprint
from toi.integrations.hokejovy_zapis.normalizer import normalize_game
from toi.journal import decode_cursor, encode_cursor
from toi.models import AuditRow, ChangeRow, JournalRow, ReceiptRow
from toi.store import Store, StoreError, public_snapshot


def service(identity="clock-a", permissions=None, teams=None, sources=None):
    return SimpleNamespace(actor=f"service:{identity}", service_id=identity,
                           permissions=frozenset(permissions or {"players:write", "history:write", "clock:authority", "clock:write", "clock:control", "setup:write"}),
                           team_ids=teams, clock_sources=sources, can_control_clock=True)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TOI_STREAM_ENABLED", "true")
    monkeypatch.setenv("TOI_STREAM_JOURNAL_RETENTION_DAYS", "7")
    monkeypatch.setenv("TOI_STREAM_CLOCK_STALE_MS", "3000")
    monkeypatch.setattr("toi.store.now_ms", lambda: 1_000_000)
    monkeypatch.setattr("toi.import_service.now_ms", lambda: 1_000_000)
    result = Store(f"sqlite:///{tmp_path / 'service.db'}")
    result.migrate()
    result.create("demo")
    yield result
    result.engine.dispose()


def submit(store, kind, payload, *, principal=None, at=1_000_000, version=None, request_id=None):
    data = {"request_id": request_id or str(uuid4()), "expected_version": store.get("demo")["version"] if version is None else version,
            "command": {"type": kind, "payload": payload}}
    return store.apply_service("demo", data, principal or service(), at), data


def claim(store, **kwargs):
    return submit(store, "clock.authority.set", {"mode": "external", "source": "scoreboard", "service_id": "clock-a"}, **kwargs)


def observe(store, *, at=1_000_000, principal=None, **overrides):
    state = store.get("demo")
    authority = state["clock_authority"]
    data = {"request_id": str(uuid4()), "expected_version": state["version"],
            "authority_generation": authority["generation"], "source": "scoreboard", "source_sequence": authority["last_source_sequence"] + 1,
            "observed_at_ms": at, "period": 1, "remaining_ms": 1_200_000, "running": True, **overrides}
    return store.apply_service("demo", data, principal or service(), at), data


def test_creation_activation_and_bounded_live_projection(store, monkeypatch):
    first = store.journal_page("demo")["records"]
    assert [r["kind"] for r in first] == ["game.created"]
    assert first[0]["version"] == 0
    assert decode_cursor(store.journal_bounds("demo")["oldest_cursor"])[2] == 0
    live = store.live("demo", 1_000_000)
    assert len(live["state"]["players"]) == 40
    assert len(live["state"]["on_ice"]["home"]) == 6
    assert live["baseline_cursor"] == first[0]["cursor"]
    assert not {"events", "shifts", "integration"} & live["state"].keys()
    assert "period_spans" not in live["state"]["clock"]
    assert live["state"]["clock_authority"]["mode"] == "manual"
    monkeypatch.setenv("TOI_STREAM_ENABLED", "false")
    store.create("legacy")
    with store.session() as session:
        assert session.get(JournalRow, "legacy") is None
    baseline = store.activate_stream("legacy")
    assert baseline == store.activate_stream("legacy")
    records = store.journal_page("legacy")["records"]
    assert [r["kind"] for r in records] == ["integration.activated"]
    assert records[0]["changes"] == []
    assert records[0]["command"]["coverage"]["prior_history"] == "v1"


def test_v1_movements_and_undo_are_atomic_journal_batches(store):
    result, _ = store.apply("demo", {"request_id": "v1-penalty", "expected_version": 0, "type": "move", "payload": {"player_id": "demo-home-p01", "to": "PENALTY_BOX"}}, Principal("operator"), 1_000_000)
    assert result["version"] == 1
    (ack, changed), _ = submit(store, "undo", {})
    assert changed and ack["committed_version"] == 2
    records = store.journal_page("demo")["records"]
    assert [r["kind"] for r in records] == ["game.created", "penalty.location.updated", "correction.updated"]
    assert {c["entity"] for c in records[1]["changes"]} >= {"player", "location", "shift", "event"}
    voided = [c for c in records[2]["changes"] if c["entity"] == "event" and c["before"]]
    assert any(c["after"]["active"] is False for c in voided)
    assert store.live("demo")["state"]["players"][0]["location"] == "ON_ICE"


def test_replay_barrier_and_cursors_survive_restart(store):
    first = store.journal_page("demo", limit=1)
    submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"})
    barrier_page = store.journal_page("demo", after=first["next_cursor"], through=first["through_cursor"])
    assert barrier_page["records"] == [] and not barrier_page["has_more"]
    latest = store.journal_bounds("demo")
    second = store.journal_page("demo", after=first["next_cursor"], through=latest["high_water_cursor"])
    assert len(second["records"]) == 1 and not second["has_more"]
    restarted = Store(str(store.engine.url))
    try:
        restarted.migrate()
        assert restarted.journal_bounds("demo") == latest
        assert restarted.journal_page("demo", after=first["next_cursor"])["records"] == second["records"]
    finally:
        restarted.engine.dispose()


@pytest.mark.parametrize("mutation,code", [
    (lambda g, n, s: "not-a-cursor", "CURSOR_INVALID"),
    (lambda g, n, s: encode_cursor("other-game", n, s), "CURSOR_WRONG_GAME"),
    (lambda g, n, s: encode_cursor(g, str(uuid4()), s), "CURSOR_WRONG_GENERATION"),
    (lambda g, n, s: encode_cursor(g, n, s + 100), "CURSOR_AHEAD"),
])
def test_distinct_cursor_errors(store, mutation, code):
    cursor = store.journal_bounds("demo")["high_water_cursor"]
    with pytest.raises(StoreError) as error:
        store.journal_page("demo", after=mutation(*decode_cursor(cursor)))
    assert error.value.code == code


def test_retention_keeps_tail_exclusive_position_and_receipt_forever(store):
    (original, _), request = submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"})
    old_cursor = original["journal_cursor"]
    submit(store, "move", {"player_id": "demo-home-p01", "to": "ON_ICE"}, at=1_000_000 + 8 * 86_400_000)
    with pytest.raises(StoreError) as error:
        store.journal_page("demo", after=encode_cursor(*decode_cursor(old_cursor)[:2], 0))
    assert error.value.code == "CURSOR_EXPIRED"
    page = store.journal_page("demo")
    assert len(page["records"]) == 1
    assert store.journal_page("demo", after=store.journal_bounds("demo")["oldest_cursor"])["records"] == page["records"]
    ack, changed = store.apply_service("demo", request, service(), 1_000_000 + 8 * 86_400_000)
    assert not changed and ack["duplicate"] and not ack["journal_available"]
    assert ack["journal_cursor"] == old_cursor and ack["current_version"] == 2


def test_clock_ownership_incremental_observation_and_cutoff(store, monkeypatch):
    claim(store)
    monkeypatch.setenv("TOI_CLOCK_SOURCE", "manual")  # Global v1 config cannot override v2 owner.
    (ack, changed), _ = observe(store)
    assert changed and ack["committed_version"] == 2
    state = store.get("demo")
    assert state["events"] == []  # No expensive growing pause/set/start triple.
    assert state["clock_authority"]["last_source_sequence"] == 1
    live = store.live("demo", at=1_010_000)["state"]
    assert live["clock"]["playing_ms"] == 3000
    assert not live["clock"]["running"]
    assert live["clock_authority"]["feed_status"] == "stale"
    assert live["players"][0]["total_toi_ms"] == 3000
    result, changed = store.expire_clock("demo", 1_010_000)
    assert changed and result["version"] == 3 and result["clock"]["playing_ms"] == 3000
    assert store.expire_clock("demo", 1_020_000) == (None, False)
    assert store.journal_page("demo")["records"][-1]["kind"] == "clock.feed.stale"
    with store.session() as session:
        receipt = session.get(ReceiptRow, ("demo", ack["request_id"]))
        assert len(json.dumps(receipt.response)) < 1024
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 3


def test_clock_stale_resume_does_not_backfill_gap(store):
    claim(store)
    observe(store)
    store.expire_clock("demo", 1_005_000)
    observe(store, at=1_010_000)
    state = store.live("demo", 1_011_000)["state"]
    assert state["clock"]["playing_ms"] == 4000
    assert state["players"][0]["total_toi_ms"] == 4000


def test_movement_after_deadline_does_not_suppress_watchdog_record(store):
    claim(store)
    observe(store)
    submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"}, at=1_005_000)
    assert store.get("demo")["clock_authority"]["feed_status"] == "stale"
    _, changed = store.expire_clock("demo", 1_005_001)
    assert changed
    assert store.journal_page("demo")["records"][-1]["kind"] == "clock.feed.stale"
    assert store.expire_clock("demo", 1_005_002) == (None, False)


def test_pre_journal_receipt_returns_original_outcome_without_reexecution(store, monkeypatch):
    monkeypatch.setenv("TOI_STREAM_ENABLED", "false")
    store.create("legacy")
    request_id = str(uuid4())
    action = {"request_id": request_id, "expected_version": 0, "type": "move", "payload": {"player_id": "legacy-home-p01", "to": "BENCH"}}
    original, _ = store.apply("legacy", action, Principal(service().actor), 1_000_000)
    ack, changed = store.apply_service("legacy", {"request_id": request_id, "expected_version": 0, "command": {"type": "move", "payload": action["payload"]}}, service(), 1_001_000)
    assert not changed and ack["duplicate"] and ack["committed_version"] == original["version"]
    assert ack["journal_cursor"] is None and not ack["journal_available"]
    assert len(store.journal_page("legacy")["records"]) == 1


@pytest.mark.parametrize("source_setting", ["scoreboard", "manual"])
def test_activation_pauses_old_external_feed_without_backfilling_stale_gap(store, monkeypatch, source_setting):
    monkeypatch.setenv("TOI_STREAM_ENABLED", "false")
    monkeypatch.setenv("TOI_CLOCK_SOURCE", "scoreboard")
    monkeypatch.setenv("TOI_CLOCK_STALE_MS", "10000")
    store.create("legacy")
    store.apply("legacy", {"request_id": "legacy-clock-feed", "expected_version": 0, "type": "external_clock",
                           "payload": {"source": "scoreboard", "period": 1, "remaining_ms": 1_200_000,
                                       "running": True, "observed_at_ms": 1_000_000}}, Principal("operator"), 1_000_000)
    monkeypatch.setattr("toi.store.now_ms", lambda: 1_110_000)
    monkeypatch.setenv("TOI_CLOCK_SOURCE", source_setting)
    store.activate_stream("legacy")
    state = store.live("legacy", 1_120_000)["state"]
    assert state["clock"]["playing_ms"] == 10000
    assert state["players"][0]["total_toi_ms"] == 10000
    assert state["clock_authority"]["mode"] == "manual"
    assert not state["clock"]["running"]
    assert "external_clock_observation" not in store.get("legacy")


def test_team_scoped_completed_undo_retry_does_not_revalidate_inactive_target(store):
    actor = service(teams={"demo-home"})
    submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"}, principal=actor)
    (_, _), data = submit(store, "undo", {}, principal=actor)
    ack, changed = store.apply_service("demo", data, actor, 1_001_000)
    assert not changed and ack["duplicate"]


def test_manual_takeover_fences_old_owner_but_identical_retry_returns_receipt(store):
    claim(store)
    (original, _), request = observe(store)
    store.apply("demo", {"request_id": "manual-takeover", "expected_version": 2, "type": "clock", "payload": {"operation": "pause"}}, Principal("operator"), 1_001_000)
    authority = store.get("demo")["clock_authority"]
    assert authority["mode"] == "manual" and authority["generation"] == 2
    duplicate, changed = store.apply_service("demo", request, service(), 1_002_000)
    assert not changed and duplicate["duplicate"] and duplicate["committed_version"] == original["committed_version"]
    with pytest.raises(StoreError) as error:
        observe(store, at=1_002_000, authority_generation=1)
    assert error.value.code == "AUTHORITY_CONFLICT"
    legacy = {"request_id": "unfenced-clock", "expected_version": 3, "type": "external_clock", "payload": {"source": "scoreboard", "period": 1, "remaining_ms": 1_190_000, "running": True, "observed_at_ms": 1_002_000}}
    with pytest.raises(StoreError) as error:
        store.apply("demo", legacy, Principal("operator"), 1_002_000)
    assert error.value.code == "AUTHORITY_CONFLICT"


@pytest.mark.parametrize("override,principal,code", [
    ({"authority_generation": 99}, None, "AUTHORITY_CONFLICT"),
    ({}, service(identity="other-service"), "CLOCK_NOT_OWNER"),
    ({"source_sequence": 0}, None, "CLOCK_OUT_OF_ORDER"),
    ({"observed_at_ms": 996_000}, None, "CLOCK_STALE"),
    ({"observed_at_ms": 1_003_000}, None, "CLOCK_FUTURE"),
    ({}, service(sources={"other-source"}), "FORBIDDEN"),
])
def test_observation_rejections_do_not_change_any_durable_rows(store, override, principal, code):
    claim(store)
    before, journal = store.get("demo"), store.journal_bounds("demo")
    with pytest.raises(StoreError) as error:
        observe(store, principal=principal, **override)
    assert error.value.code == code
    assert store.get("demo") == before and store.journal_bounds("demo") == journal


def test_team_and_setup_permissions_are_enforced_before_duplicate_lookup(store):
    restricted = service(permissions={"players:write"}, teams={"demo-home"})
    with pytest.raises(StoreError) as error:
        submit(store, "move", {"player_id": "demo-away-p01", "to": "BENCH"}, principal=restricted)
    assert error.value.status == 403
    # Dedicated setup permission does not accidentally require clock:control.
    submit(store, "setup", {"game": {"name": "Changed"}}, principal=service(permissions={"setup:write"}))
    assert store.get("demo")["game"]["name"] == "Changed"
    (_, _), data = submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"})
    with pytest.raises(StoreError) as error:
        store.apply_service("demo", data, service(permissions={"stream:read"}))
    assert error.value.code == "FORBIDDEN"


def test_duplicate_id_with_different_payload_or_actor_conflicts(store):
    (_, _), data = submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"})
    changed = deepcopy(data)
    changed["command"]["payload"]["to"] = "PENALTY_BOX"
    for request, principal in ((changed, service()), (data, service(identity="someone-else"))):
        with pytest.raises(StoreError) as error:
            store.apply_service("demo", request, principal)
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert store.get("demo")["version"] == 1


def test_failed_journal_insert_rolls_back_state_projection_audit_receipt(store, monkeypatch):
    previous = store.get("demo")
    before_journal = store.journal_bounds("demo")
    def fail(*args, **kwargs):
        raise RuntimeError("simulated journal storage failure")
    monkeypatch.setattr("toi.service_store.append_change", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        submit(store, "move", {"player_id": "demo-home-p01", "to": "BENCH"})
    assert store.get("demo") == previous
    assert store.journal_bounds("demo") == before_journal
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 0
        assert session.scalar(select(func.count()).select_from(ReceiptRow)) == 0


def test_concurrent_commands_commit_one_version_and_one_journal_record(store):
    def execute(number):
        try:
            return submit(store, "move", {"player_id": f"demo-home-p0{number}", "to": "BENCH"}, version=0)[0][0]
        except StoreError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, [1, 2]))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert len(store.journal_page("demo")["records"]) == 2
    assert store.get("demo")["version"] == 1


def test_import_and_confirmed_refresh_share_journal_transaction(store):
    raw = json.loads((Path(__file__).parent / "fixtures" / "hokejovy_966.json").read_text(encoding="utf-8"))
    dto = normalize_game(raw, "966", 1_000_000)
    importer, supervisor = ImportService(store), Principal("operator")
    initial = importer.import_game(dto, {"preview_fingerprint": fingerprint(dto)}, supervisor)
    assert [r["kind"] for r in store.journal_page("hz-966")["records"]] == ["roster.imported"]
    body = {"request_id": str(uuid4()), "expected_version": initial["version"], "confirm": False, "preview_fingerprint": fingerprint(dto)}
    importer.refresh("hz-966", dto, body, supervisor)
    assert len(store.journal_page("hz-966")["records"]) == 1
    body["confirm"] = True
    importer.refresh("hz-966", dto, body, supervisor)
    records = store.journal_page("hz-966")["records"]
    assert [r["kind"] for r in records] == ["roster.imported", "roster.refreshed"]
    assert records[-1]["request_id"] == body["request_id"]


def test_restore_rotation_invalidates_old_cursor_and_fences_clock(store):
    claim(store)
    observe(store)
    old = store.journal_bounds("demo")
    rotated = store.rotate_journal("demo", at=1_000_000)
    assert rotated["journal_generation"] != old["journal_generation"]
    with pytest.raises(StoreError) as error:
        store.journal_page("demo", after=old["high_water_cursor"])
    assert error.value.code == "CURSOR_WRONG_GENERATION"
    assert store.get("demo")["clock_authority"]["mode"] == "manual"
    assert not public_snapshot(store.get("demo"), 1_001_000)["clock"]["running"]


def test_journal_records_are_contract_valid(store):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[2] / "docs" / "interfaces" / "service-stream-v2.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator({"$ref": "#/$defs/JournalRecord", "$defs": schema["$defs"]}, format_checker=jsonschema.FormatChecker())
    claim(store)
    observe(store)
    store.expire_clock("demo", 1_010_000)
    for record in store.journal_page("demo")["records"]:
        validator.validate(record)
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(ChangeRow)) == 4
