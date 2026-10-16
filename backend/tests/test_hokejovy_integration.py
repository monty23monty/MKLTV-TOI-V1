"""Imported match lifecycle tests; all provider responses are local fixtures."""

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command as migrate_command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.exc import IntegrityError

from toi.adapters import Principal
from toi import domain
from toi.database import make_engine
from toi.import_service import ImportService, fingerprint, preview_response
from toi.integrations.hokejovy_zapis import HokejovyZapisProvider
from toi.integrations.hokejovy_zapis.normalizer import normalize_game
from toi.main import create_app
from toi.models import GameRow, PersonIdentityRow, RosterRow, ShiftRow
from toi.recommendations import recommend
from toi.store import Store, StoreError, public_snapshot
from toi.seed import create_demo_state


@pytest.fixture
def raw_source():
    return json.loads((Path(__file__).parent / "fixtures" / "hokejovy_966.json").read_text(encoding="utf-8"))


@pytest.fixture
def tracking(tmp_path, monkeypatch):
    import toi.import_service as imports
    import toi.store as storage

    for variable in ("TOI_AUTH_TOKEN", "TOI_OPERATOR_TOKENS", "TOI_CLOCK_SOURCE"):
        monkeypatch.delenv(variable, raising=False)
    clock = {"now": 1_000_000}
    monkeypatch.setattr(imports, "now_ms", lambda: clock["now"])
    monkeypatch.setattr(storage, "now_ms", lambda: clock["now"])
    store = Store(f"sqlite:///{tmp_path / 'imported.db'}")
    store.migrate()
    yield store, ImportService(store), Principal("import-supervisor"), clock
    store.engine.dispose()


def normalized(raw, clock):
    return normalize_game(deepcopy(raw), "966", clock["now"])


def import_match(tracking, raw, **overrides):
    _, service, principal, clock = tracking
    dto = normalized(raw, clock)
    body = {"provider": "hokejovy-zapis", "external_game_id": "966", "preview_fingerprint": fingerprint(dto), "starting_mode": "official", **overrides}
    return service.import_game(dto, body, principal)


def source_id(raw, side, code):
    return f"hz-966-roster-{raw['roster'][side][code]['matchPlayerId']}"


def player(state, player_id):
    return next(p for p in state["players"] if p["id"] == player_id)


def command(tracking, kind, payload, *, advance_ms=0):
    store, _, principal, clock = tracking
    clock["now"] += advance_ms
    state = store.get("hz-966")
    result, _ = store.apply("hz-966", {
        "request_id": str(uuid4()), "expected_version": state["version"], "type": kind, "payload": payload,
    }, principal, clock["now"])
    return result


def refresh(tracking, raw, *, confirm=True, restore_lines=False, **overrides):
    store, service, principal, clock = tracking
    dto = normalized(raw, clock)
    body = {
        "request_id": str(uuid4()), "expected_version": store.get("hz-966")["version"],
        "confirm": confirm, "restore_lines": restore_lines,
        "preview_fingerprint": fingerprint(dto), **overrides,
    }
    return service.refresh("hz-966", dto, body, principal)


def test_import_completed_fixture_initializes_full_rosters_and_distinct_identities(tracking, raw_source):
    state = import_match(tracking, raw_source)
    assert state["game"]["id"] == "hz-966"
    assert state["game"]["status"] == "SETUP"
    assert state["game"]["external_status"] == raw_source["gameInfo"]["status"]
    assert state["clock"]["running"] is False
    assert len(state["players"]) == 36
    assert {t["side"]: t["external_id"] for t in state["teams"]} == {"home": "5", "away": "37"}
    for side in ("home", "visitor"):
        for code, source in raw_source["roster"][side].items():
            imported = player(state, source_id(raw_source, side, code))
            assert imported["person_id"] == f"hz-player-{source['playerId']}"
            assert imported["match_player_id"] == str(source["matchPlayerId"])
            assert imported["number"] == source["jersey"]
            assert imported["location"] == ("ON_ICE" if source["startingSix"] else "BENCH")
            assert imported["total_toi_ms"] == 0
    assert len(state["shifts"]) == 12
    assert state["integration"]["original_snapshot"]["players"]
    store = tracking[0]
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(GameRow)) == 1
        assert session.scalar(select(func.count()).select_from(RosterRow)) == 36
        assert session.scalar(select(func.count()).select_from(PersonIdentityRow)) == 36


def test_reimport_reopens_tracked_session_without_duplicate_or_reset(tracking, raw_source):
    import_match(tracking, raw_source)
    changed = command(tracking, "move", {"player_id": source_id(raw_source, "home", "311"), "to": "PENALTY_BOX"})
    reopened = import_match(tracking, raw_source, starting_mode="manual")
    assert reopened["version"] == changed["version"]
    assert reopened["players"] == changed["players"]
    assert reopened["events"] == changed["events"]
    assert reopened["shifts"] == changed["shifts"]
    with tracking[0].session() as session:
        assert session.scalar(select(func.count()).select_from(GameRow)) == 1
        assert session.scalar(select(func.count()).select_from(PersonIdentityRow)) == 36


def test_preview_fingerprint_is_stable_across_provider_cache_hit(raw_source):
    provider = HokejovyZapisProvider(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw_source)),
        now_ms=lambda: 1000, sleep=lambda _: None,
    )
    first = preview_response(provider.preview("966"))
    second = preview_response(provider.preview("966"))
    assert not first["cache"]["hit"]
    assert second["cache"]["hit"]
    assert first["preview_fingerprint"] == second["preview_fingerprint"]


def test_stale_preview_fingerprint_rejected_without_partial_game(tracking, raw_source):
    with pytest.raises(StoreError) as raised:
        import_match(tracking, raw_source, preview_fingerprint="stale-review")
    assert raised.value.status == 409
    assert tracking[0].list_games() == []


def test_invalid_lineup_and_database_failure_roll_back_entire_import(tracking, raw_source, monkeypatch):
    invalid = [{"id": source_id(raw_source, "home", "101"), "location": "PENALTY_BOX"}]
    with pytest.raises(StoreError):
        import_match(tracking, raw_source, players=invalid)
    assert tracking[0].list_games() == []

    def fail_projection(*_):
        raise IntegrityError("insert", {}, RuntimeError("simulated storage conflict"))

    monkeypatch.setattr(tracking[0], "_project", fail_projection)
    with pytest.raises(StoreError):
        import_match(tracking, raw_source)
    with tracking[0].session() as session:
        assert session.scalar(select(func.count()).select_from(GameRow)) == 0
        assert session.scalar(select(func.count()).select_from(RosterRow)) == 0


def test_manual_midgame_import_has_zero_prior_toi_and_explicit_coverage(tracking, raw_source):
    current = [{"id": source_id(raw_source, side, code), "location": "ON_ICE"}
               for side in ("home", "visitor") for code, record in raw_source["roster"][side].items()
               if record["startingSix"]]
    state = import_match(tracking, raw_source, starting_mode="manual", players=current, coverage={"period": 2, "remaining_ms": 900000})
    assert state["game"]["coverage"] == {"period": 2, "remaining_ms": 900000, "partial_match": True}
    assert state["clock"]["period"] == 2
    assert state["clock"]["remaining_ms"] == 900000
    assert state["clock"]["playing_ms"] == 0
    assert len(state["shifts"]) == 12
    assert all(p["total_toi_ms"] == 0 for p in state["players"])
    assert all(shift["start_period"] == 2 and shift["start_ms"] == 0 for shift in state["shifts"])


def test_refresh_preview_is_read_only_and_confirmed_retry_is_idempotent(tracking, raw_source):
    original = import_match(tracking, raw_source)
    updated = deepcopy(raw_source)
    updated["roster"]["home"]["321"]["jersey"] = 88
    proposal = refresh(tracking, updated, confirm=False)
    assert any(change.get("field") == "number" for change in proposal["changes"])
    assert tracking[0].get("hz-966")["version"] == original["version"]
    store, service, principal, clock = tracking
    dto = normalized(updated, clock)
    body = {"request_id": str(uuid4()), "expected_version": original["version"], "confirm": True,
            "restore_lines": False, "preview_fingerprint": proposal["preview_fingerprint"]}
    first = service.refresh("hz-966", dto, body, principal)
    retry = service.refresh("hz-966", dto, body, principal)
    assert retry == first
    assert player(first, source_id(raw_source, "home", "321"))["number"] == 88
    assert store.get("hz-966")["version"] == original["version"] + 1


def test_refresh_preserves_local_metadata_overrides(tracking, raw_source):
    import_match(tracking, raw_source)
    pid = source_id(raw_source, "home", "321")
    command(tracking, "setup", {"players": [{"id": pid, "name": "Local corrected name", "number": 87, "position": "RW"}]})
    updated = deepcopy(raw_source)
    updated["roster"]["home"]["321"].update({"name": "Source changed", "jersey": 88})
    state = refresh(tracking, updated)
    p = player(state, pid)
    assert (p["name"], p["number"], p["position"]) == ("Local corrected name", 87, "RW")
    assert player(state["integration"]["latest_snapshot"], pid)["number"] == 88


def test_refresh_new_player_starts_bench_and_removed_active_player_keeps_history(tracking, raw_source):
    import_match(tracking, raw_source)
    command(tracking, "clock", {"operation": "start"})
    before = command(tracking, "clock", {"operation": "pause"}, advance_ms=30000)
    removed = source_id(raw_source, "home", "311")
    updated = deepcopy(raw_source)
    del updated["roster"]["home"]["311"]
    updated["roster"]["home"]["343"] = {"playerId": 999001, "matchPlayerId": 999002, "name": "New", "surname": "Entrant", "jersey": 77, "position": "FW", "startingSix": True, "roster": "343"}
    state = refresh(tracking, updated)
    assert player(state, removed)["source_missing"]
    assert player(state, removed)["location"] == "ON_ICE"
    assert player(state, removed)["total_toi_ms"] == 30000
    added = player(state, "hz-966-roster-999002")
    assert added["location"] == "BENCH"
    assert added["total_toi_ms"] == 0
    assert state["shifts"] == before["shifts"]


def test_changed_starting_six_never_rewrites_current_locations(tracking, raw_source):
    import_match(tracking, raw_source)
    outgoing, incoming = source_id(raw_source, "home", "311"), source_id(raw_source, "home", "321")
    before = command(tracking, "substitute", {"team_id": "hz-team-5", "outgoing_ids": [outgoing], "incoming_ids": [incoming]})
    updated = deepcopy(raw_source)
    updated["roster"]["home"]["311"]["startingSix"] = False
    updated["roster"]["home"]["321"]["startingSix"] = True
    state = refresh(tracking, updated)
    assert {p["id"]: p["location"] for p in state["players"]} == {p["id"]: p["location"] for p in before["players"]}
    assert state["shifts"] == before["shifts"]


def test_source_line_changes_require_explicit_restore(tracking, raw_source):
    state = import_match(tracking, raw_source)
    custom_lines = deepcopy(state["lines"])
    custom_lines[0]["name"] = "Operator preferred combination"
    command(tracking, "setup", {"lines": custom_lines})
    updated = deepcopy(raw_source)
    moved = updated["roster"]["home"].pop("321")
    moved["roster"] = "343"
    updated["roster"]["home"]["343"] = moved
    retained = refresh(tracking, updated)
    assert retained["lines"] == custom_lines
    restored = refresh(tracking, updated, restore_lines=True)
    assert restored["lines"] == normalized(updated, tracking[3])["lines"]


def test_changed_source_identity_and_stale_version_are_rolled_back(tracking, raw_source):
    state = import_match(tracking, raw_source)
    stored_before = tracking[0].get("hz-966")
    changed_identity = deepcopy(raw_source)
    changed_identity["roster"]["home"]["311"]["playerId"] = 888888
    with pytest.raises(StoreError) as raised:
        refresh(tracking, changed_identity)
    assert raised.value.status == 409
    assert tracking[0].get("hz-966")["version"] == state["version"]
    assert tracking[0].get("hz-966") == stored_before
    with pytest.raises(StoreError) as raised:
        refresh(tracking, raw_source, expected_version=state["version"] - 1)
    assert raised.value.status == 409


def test_team_restricted_operator_cannot_import_or_refresh(tracking, raw_source):
    store, service, _, clock = tracking
    dto = normalized(raw_source, clock)
    limited = Principal("home-only", {"hz-team-5"}, False)
    with pytest.raises(StoreError) as raised:
        service.import_game(dto, {"preview_fingerprint": fingerprint(dto)}, limited)
    assert raised.value.status == 403
    import_match(tracking, raw_source)
    with pytest.raises(StoreError) as raised:
        service.refresh("hz-966", dto, {"request_id": str(uuid4()), "expected_version": store.get("hz-966")["version"], "confirm": False}, limited)
    assert raised.value.status == 403


def test_imported_roles_lines_and_corrected_learning_survive_refresh(tracking, raw_source):
    state = import_match(tracking, raw_source)
    outgoing, incoming = source_id(raw_source, "home", "311"), source_id(raw_source, "home", "321")
    initial = recommend(state, "hz-team-5", [outgoing])
    assert "Same position" in next(p for p in initial["players"] if p["player_id"] == incoming)["reasons"]
    first_line = next(line for line in state["lines"] if line["team_id"] == "hz-team-5" and line["kind"] == "FORWARD" and line["line_number"] == 1)
    assert any(group["name"] == "Forward line 2" for group in recommend(state, "hz-team-5", first_line["player_ids"])["groups"])
    for _ in range(2):
        command(tracking, "substitute", {"team_id": "hz-team-5", "outgoing_ids": [outgoing], "incoming_ids": [incoming]})
        state = command(tracking, "substitute", {"team_id": "hz-team-5", "outgoing_ids": [incoming], "incoming_ids": [outgoing]})
    before = next(p for p in recommend(state, "hz-team-5", [outgoing])["players"] if p["player_id"] == incoming)
    refreshed = refresh(tracking, raw_source)
    after = next(p for p in recommend(refreshed, "hz-team-5", [outgoing])["players"] if p["player_id"] == incoming)
    assert before["observations"] == after["observations"] == 2
    command(tracking, "undo", {})
    undone = command(tracking, "undo", {})
    learned = next(p for p in recommend(undone, "hz-team-5", [outgoing])["players"] if p["player_id"] == incoming)
    assert learned["observations"] == 1


def test_realistic_import_track_refresh_reload_preserves_independent_toi(tracking, raw_source):
    import_match(tracking, raw_source)

    def pid(code):
        return source_id(raw_source, "home", code)
    command(tracking, "clock", {"operation": "start"})
    command(tracking, "substitute", {"team_id": "hz-team-5", "outgoing_ids": [pid("311")], "incoming_ids": [pid("321")]}, advance_ms=30000)
    command(tracking, "substitute", {"team_id": "hz-team-5", "outgoing_ids": [pid("321"), pid("312"), pid("313")], "incoming_ids": [pid("331"), pid("332"), pid("333")]}, advance_ms=30000)
    command(tracking, "substitute", {"team_id": "hz-team-5", "outgoing_ids": [pid("211"), pid("212")], "incoming_ids": [pid("221"), pid("222")]}, advance_ms=30000)
    command(tracking, "move", {"player_id": pid("331"), "to": "PENALTY_BOX"}, advance_ms=20000)
    command(tracking, "move", {"player_id": pid("331"), "to": "ON_ICE"}, advance_ms=20000)
    before = command(tracking, "clock", {"operation": "pause"}, advance_ms=20000)
    # Independent on-ice intervals on a 150-second playing-time axis.
    expected = {"101": 150000, "311": 30000, "321": 30000, "312": 60000, "313": 60000,
                "331": 70000, "332": 90000, "333": 90000, "211": 90000, "212": 90000,
                "221": 60000, "222": 60000}
    for code, toi in expected.items():
        assert player(before, pid(code))["total_toi_ms"] == toi
    updated = deepcopy(raw_source)
    updated["roster"]["home"]["341"]["jersey"] = 88
    after = refresh(tracking, updated)
    assert after["shifts"] == before["shifts"]
    assert after["clock"] == before["clock"]
    assert after["events"][:len(before["events"])] == before["events"]
    restarted = Store(str(tracking[0].engine.url))
    try:
        restarted.migrate()
        reloaded = public_snapshot(restarted.get("hz-966"), tracking[3]["now"] + 999000)
        assert reloaded["shifts"] == after["shifts"]
        assert reloaded["clock"]["playing_ms"] == 150000
        for code, toi in expected.items():
            assert player(reloaded, pid(code))["total_toi_ms"] == toi
    finally:
        restarted.engine.dispose()


@pytest.fixture
def source_api(tracking, raw_source):
    control = {"payload": deepcopy(raw_source), "status": 200, "requests": []}

    def respond(request):
        control["requests"].append(str(request.url))
        return httpx.Response(control["status"], json=control["payload"])

    app = create_app(str(tracking[0].engine.url))
    app.state.hokejovy_provider = HokejovyZapisProvider(
        transport=httpx.MockTransport(respond), now_ms=lambda: tracking[3]["now"], sleep=lambda _: None,
    )
    with TestClient(app) as client:
        yield client, control


def api_import(client, **adjustments):
    preview = client.get("/api/v1/integrations/hokejovy-zapis/games/966/preview")
    assert preview.status_code == 200
    body = {"provider": "hokejovy-zapis", "external_game_id": "966",
            "preview_fingerprint": preview.json()["preview_fingerprint"], **adjustments}
    response = client.post("/api/v1/games/import", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def test_http_preview_confirm_cached_fetch_and_offline_reimport(source_api):
    client, control = source_api
    first, body = api_import(client)
    assert len(control["requests"]) == 1  # Confirmation used the reviewed preview cache.
    assert len(first["players"]) == 36
    assert client.get("/api/v1/games/hz-966/lines").json() == first["lines"]
    control["status"] = 503
    again = client.post("/api/v1/games/import", json=body)
    assert again.status_code == 200
    assert again.json()["players"] == first["players"]
    assert len(control["requests"]) == 1  # Existing sessions do not depend on the provider.
    assert len(client.get("/api/v1/games").json()) == 2  # Imported game plus local demo.


def test_http_failed_initial_fetch_leaves_only_existing_demo(source_api):
    client, control = source_api
    control["status"] = 503
    response = client.post("/api/v1/games/import", json={"provider": "hokejovy-zapis", "external_game_id": "966", "preview_fingerprint": "0" * 64})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_error"
    assert [g["id"] for g in client.get("/api/v1/games").json()] == ["demo"]


@pytest.mark.parametrize("game_id", ["0", "-1", "abc", "1.2", "999999999999999999999999"])
def test_http_invalid_game_id_never_requests_provider(source_api, game_id):
    client, control = source_api
    assert client.get(f"/api/v1/integrations/hokejovy-zapis/games/{game_id}/preview").status_code == 422
    assert control["requests"] == []


def test_http_refresh_failure_exposes_cache_without_modifying_imported_state(source_api):
    client, control = source_api
    state, _ = api_import(client)
    control["status"] = 503
    response = client.post("/api/v1/games/hz-966/roster/refresh", json={"request_id": str(uuid4()), "expected_version": state["version"], "confirm": False})
    assert response.status_code == 502
    error = response.json()["detail"]
    assert error["code"] == "upstream_error"
    assert error["cached_preview"]["cache"]["stale"]
    current = client.get("/api/v1/games/hz-966/state").json()
    assert current["version"] == state["version"]
    assert current["players"] == state["players"]
    assert current["shifts"] == state["shifts"]


def test_http_confirmed_refresh_retry_works_after_provider_goes_down(source_api):
    client, control = source_api
    state, _ = api_import(client)
    control["payload"]["roster"]["home"]["321"]["jersey"] = 88
    preview_body = {"request_id": str(uuid4()), "expected_version": state["version"], "confirm": False, "restore_lines": False}
    preview = client.post("/api/v1/games/hz-966/roster/refresh", json=preview_body)
    assert preview.status_code == 200
    body = {**preview_body, "confirm": True, "preview_fingerprint": preview.json()["preview_fingerprint"]}
    first = client.post("/api/v1/games/hz-966/roster/refresh", json=body)
    assert first.status_code == 200
    calls_before = len(control["requests"])
    control["status"] = 503
    retry = client.post("/api/v1/games/hz-966/roster/refresh", json=body)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert len(control["requests"]) == calls_before
    current = client.get("/api/v1/games/hz-966/state").json()
    assert current["version"] == first.json()["version"]


def test_http_changed_preview_and_team_permission_rejected(source_api, monkeypatch):
    client, control = source_api
    preview = client.get("/api/v1/integrations/hokejovy-zapis/games/966/preview").json()
    response = client.post("/api/v1/games/import", json={"provider": "hokejovy-zapis", "external_game_id": "966", "preview_fingerprint": "0" * 64})
    assert response.status_code == 409
    monkeypatch.setenv("TOI_OPERATOR_TOKENS", '{"home":{"actor":"home-op","team_ids":["hz-team-5"],"can_control_clock":false}}')
    calls_before = len(control["requests"])
    headers = {"Authorization": "Bearer home"}
    assert client.get("/api/v1/integrations/hokejovy-zapis/games/966/preview", headers=headers).status_code == 403
    response = client.post("/api/v1/games/import", headers=headers, json={"provider": "hokejovy-zapis", "external_game_id": "966", "preview_fingerprint": preview["preview_fingerprint"]})
    assert response.status_code == 403
    assert len(control["requests"]) == calls_before


def test_existing_ineligible_player_is_not_reenabled_by_source_refresh(tracking, raw_source):
    import_match(tracking, raw_source)
    bench = source_id(raw_source, "home", "321")
    command(tracking, "setup", {"players": [{"id": bench, "eligible": False}]})
    state = refresh(tracking, raw_source)
    assert not player(state, bench)["eligible"]
    assert bench not in {p["player_id"] for p in recommend(state, "hz-team-5", [source_id(raw_source, "home", "311")])["players"]}


def test_running_refresh_accrues_only_current_tracked_playing_time(tracking, raw_source):
    state = import_match(tracking, raw_source)
    original_snapshot = deepcopy(state["integration"]["original_snapshot"])
    command(tracking, "clock", {"operation": "start"})
    tracking[3]["now"] += 30000
    state = refresh(tracking, raw_source)
    assert state["clock"]["running"]
    assert state["clock"]["playing_ms"] == 30000
    assert state["integration"]["original_snapshot"] == original_snapshot
    assert state["game"]["external_status"] == "end"  # Source completion cannot stop local play.
    assert all(p["total_toi_ms"] == (30000 if p["location"] == "ON_ICE" else 0) for p in state["players"])


def test_source_changes_during_refresh_review_require_a_new_confirmation(source_api):
    client, control = source_api
    state, _ = api_import(client)
    control["payload"]["roster"]["home"]["321"]["jersey"] = 88
    body = {"request_id": str(uuid4()), "expected_version": state["version"], "confirm": False}
    proposal = client.post("/api/v1/games/hz-966/roster/refresh", json=body).json()
    control["payload"]["roster"]["home"]["321"]["jersey"] = 87
    # Another deliberate force fetch updates the cached provider revision.
    assert client.get("/api/v1/integrations/hokejovy-zapis/games/966/preview?force=true").status_code == 200
    response = client.post("/api/v1/games/hz-966/roster/refresh", json={**body, "confirm": True, "preview_fingerprint": proposal["preview_fingerprint"]})
    assert response.status_code == 409
    current = client.get("/api/v1/games/hz-966/state").json()
    assert current["version"] == state["version"]
    assert current["players"] == state["players"]


def test_persistent_people_are_reused_but_match_tracking_entries_remain_separate(tracking, raw_source):
    first = import_match(tracking, raw_source)
    other_source = deepcopy(raw_source)
    for roster in other_source["roster"].values():
        for entry in roster.values():
            entry["matchPlayerId"] += 100000
    store, service, principal, clock = tracking
    dto = normalize_game(other_source, "967", clock["now"])
    second = service.import_game(dto, {"preview_fingerprint": fingerprint(dto), "starting_mode": "official"}, principal)
    assert {p["person_id"] for p in first["players"]} == {p["person_id"] for p in second["players"]}
    assert not {p["id"] for p in first["players"]} & {p["id"] for p in second["players"]}
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(GameRow)) == 2
        assert session.scalar(select(func.count()).select_from(PersonIdentityRow)) == 36
        assert session.scalar(select(func.count()).select_from(RosterRow)) == 72


@pytest.mark.parametrize("identity_change", ["team", "match_player"])
def test_refresh_identity_reassignments_require_reconciliation(tracking, raw_source, identity_change):
    import_match(tracking, raw_source)
    before = tracking[0].get("hz-966")
    changed = deepcopy(raw_source)
    if identity_change == "team":
        changed["gameInfo"]["teamInfo"]["home"]["id"] = 999
    else:
        changed["roster"]["home"]["311"]["matchPlayerId"] = 999998
    with pytest.raises(StoreError) as raised:
        refresh(tracking, changed)
    assert raised.value.status == 409
    assert tracking[0].get("hz-966") == before


def test_source_identity_migration_upgrades_existing_0001_game_without_losing_shifts(tmp_path):
    url = f"sqlite:///{tmp_path / 'old-match.db'}"
    engine = make_engine(url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    state = create_demo_state(now_ms=1000)
    state = domain.apply_action(state, {"type": "clock", "payload": {"operation": "start"}}, 1000)
    state = domain.apply_action(state, {"type": "move", "payload": {"player_id": "demo-home-p01", "to": "PENALTY_BOX"}}, 31000)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        migrate_command.upgrade(config, "0001")
        tables = MetaData()
        games = Table("games", tables, autoload_with=connection)
        shifts = Table("shifts", tables, autoload_with=connection)
        connection.execute(games.insert().values(id="demo", name=state["game"]["name"], external_id=None, status="LIVE", version=state["version"], state=state))
        for shift in state["shifts"]:
            connection.execute(shifts.insert().values(game_id="demo", id=shift["id"], player_id=shift["player_id"], team_id=shift["team_id"], start_playing_ms=shift["start_playing_ms"], end_playing_ms=shift["end_playing_ms"], data=shift))
    assert "person_id" not in {column["name"] for column in inspect(engine).get_columns("players")}
    engine.dispose()
    store = Store(url)
    try:
        store.migrate()
        assert store.get("demo") == state
        assert "person_identities" in inspect(store.engine).get_table_names()
        assert "person_id" in {column["name"] for column in inspect(store.engine).get_columns("players")}
        with store.session() as session:
            saved = session.scalars(select(ShiftRow)).all()
            assert {s.id: s.data for s in saved} == {s["id"]: s for s in state["shifts"]}
        snapshot = public_snapshot(store.get("demo"), 31000)
        assert player(snapshot, "demo-home-p01")["total_toi_ms"] == 30000
        assert player(snapshot, "demo-home-p01")["location"] == "PENALTY_BOX"
    finally:
        store.engine.dispose()


@pytest.mark.parametrize("field", ["number", "name"])
def test_local_player_override_stays_authoritative_after_source_converges(tracking, raw_source, field):
    import_match(tracking, raw_source)
    pid = source_id(raw_source, "home", "321")
    local_value = 88 if field == "number" else "Operator Chosen"
    command(tracking, "setup", {"players": [{"id": pid, field: local_value}]})
    converged = deepcopy(raw_source)
    entry = converged["roster"]["home"]["321"]
    if field == "number":
        entry["jersey"] = 88
    else:
        entry.update({"name": "Operator", "surname": "Chosen"})
    first = refresh(tracking, converged)
    assert player(first, pid)[field] == local_value
    changed_again = deepcopy(converged)
    entry = changed_again["roster"]["home"]["321"]
    if field == "number":
        entry["jersey"] = 89
    else:
        entry.update({"name": "New", "surname": "Source Name"})
    second = refresh(tracking, changed_again)
    assert player(second, pid)[field] == local_value
    assert player(second["integration"]["latest_snapshot"], pid)[field] != local_value


def test_local_team_and_game_name_overrides_survive_source_convergence(tracking, raw_source):
    import_match(tracking, raw_source)
    local_home = "Operator Home"
    local_game = f"{local_home} vs {raw_source['gameInfo']['teamInfo']['visitor']['name']}"
    command(tracking, "setup", {"game": {"name": local_game, "home_name": local_home}})
    converged = deepcopy(raw_source)
    converged["gameInfo"]["teamInfo"]["home"]["name"] = local_home
    first = refresh(tracking, converged)
    assert first["game"]["name"] == local_game
    assert next(t for t in first["teams"] if t["side"] == "home")["name"] == local_home
    changed_again = deepcopy(converged)
    changed_again["gameInfo"]["teamInfo"]["home"]["name"] = "New Official Home"
    second = refresh(tracking, changed_again)
    assert second["game"]["name"] == local_game
    assert next(t for t in second["teams"] if t["side"] == "home")["name"] == local_home
    assert second["integration"]["latest_snapshot"]["game"]["name"] != local_game


def test_invalid_source_jersey_preserves_valid_active_player_metadata(tracking, raw_source):
    import_match(tracking, raw_source)
    pid = source_id(raw_source, "home", "311")
    command(tracking, "clock", {"operation": "start"})
    before = command(tracking, "clock", {"operation": "pause"}, advance_ms=10000)
    previous = player(before, pid)
    malformed = deepcopy(raw_source)
    malformed["roster"]["home"]["311"].update({"jersey": -1, "name": "Unreviewed", "surname": "Source Edit"})
    state = refresh(tracking, malformed)
    current = player(state, pid)
    assert current["source"]["review_required"]
    for field in ("name", "number", "position", "assigned_role", "eligible", "location"):
        assert current[field] == previous[field], field
    assert current["eligible"]
    assert current["location"] == "ON_ICE"
    assert current["total_toi_ms"] == 10000
    assert state["shifts"] == before["shifts"]


def test_swapping_source_team_sides_requires_reconciliation_without_mutation(tracking, raw_source):
    import_match(tracking, raw_source)
    before = tracking[0].get("hz-966")
    swapped = deepcopy(raw_source)
    info = swapped["gameInfo"]["teamInfo"]
    info["home"], info["visitor"] = info["visitor"], info["home"]
    rosters = swapped["roster"]
    rosters["home"], rosters["visitor"] = rosters["visitor"], rosters["home"]
    with pytest.raises(StoreError) as raised:
        refresh(tracking, swapped)
    assert raised.value.status == 409
    assert tracking[0].get("hz-966") == before
