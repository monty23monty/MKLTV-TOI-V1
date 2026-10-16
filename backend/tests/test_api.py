"""Contract, persistence, concurrency and real-time integration tests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from toi.adapters import Principal
from toi.main import create_app
from toi.models import AuditRow, LocationRow, ReceiptRow, ShiftRow
from toi.store import Store, StoreError


@pytest.fixture
def client(tmp_path, monkeypatch):
    for variable in ("TOI_AUTH_TOKEN", "TOI_OPERATOR_TOKENS", "TOI_CLOCK_SOURCE"):
        monkeypatch.delenv(variable, raising=False)
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}")
    with TestClient(app) as client:
        yield client


def command(client, kind, payload, version=None, request_id=None):
    if version is None:
        version = client.get("/api/v1/games/demo/state").json()["version"]
    body = {"request_id": request_id or str(uuid4()), "expected_version": version, "type": kind, "payload": payload}
    return client.post("/api/v1/games/demo/actions", json=body)


def test_snapshot_projections_and_openapi(client):
    response = client.get("/api/v1/games/demo/state")
    assert response.status_code == 200
    state = response.json()
    assert len(state["players"]) == 40
    assert len(state["shifts"]) == 12
    assert state["strengths"] == {"home": 5, "away": 5}
    assert not any(key.startswith("_") for key in state)
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/openapi.json").status_code == 200
    store = client.app.state.store
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(LocationRow)) == 40
        assert session.scalar(select(func.count()).select_from(ShiftRow)) == 12


def test_transactional_group_and_idempotency(client):
    body = {"team_id": "demo-home", "outgoing_ids": ["demo-home-p01", "demo-home-p02", "demo-home-p03"], "incoming_ids": ["demo-home-p04", "demo-home-p05", "demo-home-p06"]}
    first = command(client, "substitute", body, 0, "group-retry-key")
    assert first.status_code == 200
    duplicate = command(client, "substitute", body, 0, "group-retry-key")
    assert duplicate.json() == first.json()
    assert duplicate.status_code == 200
    changed = command(client, "substitute", {**body, "incoming_ids": ["demo-home-p07"]}, 0, "group-retry-key")
    assert changed.status_code == 409
    state = first.json()
    assert state["version"] == 1
    assert all(p["location"] == "BENCH" for p in state["players"] if p["id"] in body["outgoing_ids"])
    assert all(p["location"] == "ON_ICE" for p in state["players"] if p["id"] in body["incoming_ids"])
    with client.app.state.store.session() as session:
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 1
        assert session.scalar(select(func.count()).select_from(ReceiptRow)) == 1


def test_bad_group_is_fully_rolled_back(client):
    response = command(client, "substitute", {"team_id": "demo-home", "outgoing_ids": ["demo-home-p01", "demo-home-p02"], "incoming_ids": ["demo-home-p04", "demo-away-p04"]})
    assert response.status_code == 422
    state = client.get("/api/v1/games/demo/state").json()
    assert state["version"] == 0
    assert state["events"] == []
    assert state["players"][0]["location"] == "ON_ICE"


def test_stale_version_cannot_overwrite(client):
    assert command(client, "move", {"player_id": "demo-home-p01", "to": "BENCH"}, 0).status_code == 200
    response = command(client, "move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0)
    assert response.status_code == 409
    assert response.json()["resync_required"] is True
    assert client.get("/api/v1/games/demo/state").json()["version"] == 1


def test_undo_and_correct_are_auditable(client):
    first = command(client, "substitute", {"team_id": "demo-home", "outgoing_ids": ["demo-home-p02"], "incoming_ids": ["demo-home-p05"]}).json()
    event_id = first["events"][-1]["id"]
    corrected = command(client, "correct", {"event_id": event_id, "replacement": {"type": "substitute", "payload": {"team_id": "demo-home", "outgoing_ids": ["demo-home-p02"], "incoming_ids": ["demo-home-p08"]}}})
    assert corrected.status_code == 200
    state = corrected.json()
    assert next(p for p in state["players"] if p["id"] == "demo-home-p05")["location"] == "BENCH"
    assert next(p for p in state["players"] if p["id"] == "demo-home-p08")["location"] == "ON_ICE"
    assert state["events"][0]["voided"] is True
    assert command(client, "undo", {}).status_code == 200
    assert len(client.get("/api/v1/games/demo/audit").json()) == 3


def test_ws_order_and_reconnection(client):
    with client.websocket_connect("/api/v1/games/demo/ws") as ws:
        assert ws.receive_json()["type"] == "state.resync"
        for index, player_id in enumerate(["demo-home-p01", "demo-home-p02"], 1):
            assert command(client, "move", {"player_id": player_id, "to": "PENALTY_BOX"}).status_code == 200
            event = ws.receive_json()
            while event["type"] == "clock.tick":
                event = ws.receive_json()
            assert event["version"] == index
            assert event["state"]["version"] == index
            assert event["type"] == "penalty.location.updated"
    with client.websocket_connect("/api/v1/games/demo/ws") as ws:
        event = ws.receive_json()
        assert event["type"] == "state.resync"
        assert event["version"] == 2
        refreshed = client.get("/api/v1/games/demo/state").json()
        assert event["state"]["players"] == refreshed["players"]
        assert event["state"]["shifts"] == refreshed["shifts"]
        assert event["state"]["events"] == refreshed["events"]
        assert event["state"]["clock"]["playing_ms"] == refreshed["clock"]["playing_ms"]


def test_read_endpoints_and_eligibility(client):
    for endpoint in ["teams", "roster", "locations", "statistics", "shifts", "events", "clock", "audit"]:
        assert client.get(f"/api/v1/games/demo/{endpoint}").status_code == 200
    response = client.get("/api/v1/games/demo/suggestions", params={"team_id": "demo-home", "outgoing_ids": "demo-home-p02"})
    assert response.status_code == 200
    assert response.json()["players"][0]["eligible"] is True
    assert client.get("/api/v1/games/demo/suggestions", params={"team_id": "bad"}).status_code == 404
    assert client.get("/api/v1/games/demo/suggestions", params={"team_id": "demo-home", "outgoing_ids": "demo-away-p02"}).status_code == 422


def test_setup_and_independent_games(client):
    setup = command(client, "setup", {"game": {"home_name": "Test Home"}, "players": [{"id": "demo-home-p01", "number": 90, "external_id": "legacy-123"}]})
    assert setup.status_code == 200
    assert setup.json()["teams"][0]["name"] == "Test Home"
    new_game = client.post("/api/v1/games", json={"home_name": "South Coast", "away_name": "West Ridge"})
    assert new_game.status_code == 201
    assert new_game.json()["game"]["id"] != "demo"
    assert new_game.json()["version"] == 0
    assert len(client.get("/api/v1/games").json()) == 2


def test_authentication_and_team_permissions(client, monkeypatch):
    monkeypatch.setenv("TOI_OPERATOR_TOKENS", '{"home-token":{"actor":"home-op","team_ids":["demo-home"],"can_control_clock":false}}')
    assert client.get("/api/v1/games").status_code == 401
    headers = {"Authorization": "Bearer home-token"}
    assert client.get("/api/v1/games", headers=headers).status_code == 200
    for kind, payload in [("clock", {"operation": "start"}), ("move", {"player_id": "demo-away-p01", "to": "BENCH"})]:
        response = client.post("/api/v1/games/demo/actions", headers=headers, json={"request_id": str(uuid4()), "expected_version": 0, "type": kind, "payload": payload})
        assert response.status_code == 403
    response = client.post("/api/v1/games/demo/actions", headers=headers, json={"request_id": str(uuid4()), "expected_version": 0, "actor": "forged", "type": "move", "payload": {"player_id": "demo-home-p01", "to": "BENCH"}})
    assert response.status_code == 200
    assert response.json()["events"][-1]["actor"] == "home-op"
    event_id = response.json()["events"][-1]["id"]
    malformed = client.post("/api/v1/games/demo/actions", headers=headers, json={"request_id": str(uuid4()), "expected_version": 1, "type": "correct", "payload": {"event_id": event_id, "replacement": {"type": "move", "payload": {}}}})
    assert malformed.status_code == 422
    assert client.get("/api/v1/games/demo/state", headers=headers).json()["version"] == 1


def test_persistence_after_service_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    with TestClient(create_app(url)) as first:
        response = command(first, "move", {"player_id": "demo-home-p01", "to": "PENALTY_BOX"})
        assert response.status_code == 200
    with TestClient(create_app(url)) as second:
        state = second.get("/api/v1/games/demo/state").json()
        assert state["version"] == 1
        assert state["players"][0]["location"] == "PENALTY_BOX"
        assert len(state["events"]) == 1


def test_concurrent_writers_have_one_winner(tmp_path):
    url = f"sqlite:///{tmp_path / 'concurrent.db'}"
    first, second = Store(url), Store(url)
    first.migrate()
    first.create("demo")
    principal = Principal("test")

    def write(store, player):
        try:
            store.apply("demo", {"request_id": str(uuid4()), "expected_version": 0, "type": "move", "payload": {"player_id": player, "to": "BENCH"}}, principal)
            return 200
        except StoreError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(write, first, "demo-home-p01")
        b = pool.submit(write, second, "demo-home-p02")
        assert sorted([a.result(), b.result()]) == [200, 409]
    assert first.get("demo")["version"] == 1
    first.engine.dispose()
    second.engine.dispose()


def test_clock_provider_validation_and_stale_feed(client, monkeypatch):
    monkeypatch.setenv("TOI_CLOCK_SOURCE", "test-scoreboard")
    monkeypatch.setenv("TOI_CLOCK_STALE_MS", "2000")
    from toi.store import now_ms, public_snapshot
    timestamp = now_ms()
    body = {"request_id": str(uuid4()), "expected_version": 0, "period": 1, "remaining_ms": 1_100_000, "running": True, "observed_at_ms": timestamp, "source": "test-scoreboard"}
    response = client.post("/api/v1/games/demo/clock/observations", json=body)
    assert response.status_code == 200
    state = client.app.state.store.get("demo")
    stale = public_snapshot(state, timestamp + 100_000)
    assert stale["clock"]["running"] is False
    assert stale["clock"]["playing_ms"] <= 2000
    assert any("stale" in warning for warning in stale["warnings"])
    assert client.post("/api/v1/games/demo/clock/observations", json={**body, "request_id": str(uuid4()), "expected_version": 1}).status_code == 409
    assert client.post("/api/v1/games/demo/clock/observations", json={**body, "request_id": str(uuid4()), "expected_version": 1, "observed_at_ms": timestamp - 50000}).status_code == 422


def test_unknown_and_malformed_requests(client):
    assert client.get("/api/v1/games/missing/state").status_code == 404
    assert command(client, "delete-everything", {}).status_code == 422
    assert command(client, "move", {"player_id": "demo-home-p01", "to": "INVALID"}).status_code == 422
    assert command(client, "clock", {"operation": "set", "remaining_ms": -1}).status_code == 422
    assert command(client, "setup", {"players": [{"id": "demo-home-p01", "number": "17"}]}).status_code == 422
    assert command(client, "setup", {"players": [{"id": "demo-home-p01", "eligible": "false"}]}).status_code == 422
    assert command(client, "setup", {"players": [{"id": "a" * 101, "name": "New"}]}).status_code == 422


def test_manual_fallback_clears_stale_provider(client, monkeypatch):
    monkeypatch.setenv("TOI_CLOCK_SOURCE", "test-scoreboard")
    monkeypatch.setenv("TOI_CLOCK_STALE_MS", "2000")
    from toi.store import now_ms, public_snapshot
    timestamp = now_ms()
    store = client.app.state.store
    principal = Principal("supervisor")
    payload = {"period": 1, "remaining_ms": 1_100_000, "running": True, "observed_at_ms": timestamp, "source": "test-scoreboard"}
    store.apply("demo", {"type": "external_clock", "payload": payload, "request_id": str(uuid4()), "expected_version": 0}, principal, timestamp)
    store.apply("demo", {"type": "clock", "payload": {"operation": "start"}, "request_id": str(uuid4()), "expected_version": 1}, principal, timestamp + 10000)
    state = store.get("demo")
    assert "external_clock_observation" not in state
    manual = public_snapshot(state, timestamp + 11000)
    assert manual["clock"]["running"] is True
    assert manual["clock"]["playing_ms"] == 3000
    assert manual["clock"]["source"] == "manual"


def test_ws_ticks_are_compact_and_clock_based(client):
    with client.websocket_connect("/api/v1/games/demo/ws") as ws:
        assert ws.receive_json()["type"] == "state.resync"
        tick = ws.receive_json()
        assert tick["type"] == "clock.tick"
        assert "state" not in tick and "events" not in tick
        assert tick["clock"]["running"] is False
        assert len(tick["toi"]) == 40
        assert all(p["total_toi_ms"] == 0 for p in tick["toi"])
