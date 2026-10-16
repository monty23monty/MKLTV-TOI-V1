"""Real ASGI duplex transport, scope isolation, replay and strict wire admission."""

import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect
from starlette.testclient import WebSocketDenialResponse

from toi.main import create_app
from toi.service_auth import StreamConfig, resolve_service, service_identities
from toi.service_schemas import WireError, parse_message
from toi.service_stream import GameFeed, RateBucket, Session
from toi.store import now_ms


READS = ["stream:read", "journal:read"]
ALL = READS + ["players:write", "clock:write", "clock:control", "clock:authority", "history:write", "setup:write"]
HEADERS = {"Authorization": "Bearer controller-secret"}
URL = "/api/v2/games/demo/stream"
PROTOCOLS = ["lightning.toi.stream.v2"]


def message(kind, data, game="demo"):
    return {"protocol": 2, "type": kind, "message_id": str(uuid4()), "game_id": game, "data": data}


def command(kind, payload, version, request_id=None):
    return message("command.submit", {"request_id": request_id or str(uuid4()), "expected_version": version,
                                      "command": {"type": kind, "payload": payload}})


def open_session(socket, mode="now", after=None, tick_hz=0):
    data = {"client_id": "test-service", "journal_mode": mode, "tick_hz": tick_hz}
    if after is not None:
        data["after_cursor"] = after
    socket.send_json(message("session.open", data))
    frames = []
    while True:
        frame = socket.receive_json()
        frames.append(frame)
        if frame["type"] in {"stream.ready", "session.error", "replay.required"}:
            return frames


def until(socket, kind):
    frames = []
    while len(frames) < 100:
        frame = socket.receive_json()
        frames.append(frame)
        if frame["type"] == kind:
            return frame, frames
        if frame["type"] == "ping":
            socket.send_json(message("pong", frame["data"]))
    raise AssertionError(f"No {kind} frame")


@pytest.fixture
def client(tmp_path, monkeypatch):
    for variable in ("TOI_OPERATOR_TOKENS", "TOI_AUTH_TOKEN", "TOI_CLOCK_SOURCE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("TOI_STREAM_ENABLED", "true")
    identities = {
        "controller-secret": {"service_id": "controller", "game_ids": ["demo"], "permissions": ALL,
                              "clock_sources": ["scoreboard"]},
        "graphics-secret": {"service_id": "graphics", "game_ids": ["demo"], "permissions": READS},
        "clock-secret": {"service_id": "clock-bridge", "game_ids": ["demo"],
                         "permissions": READS + ["clock:write"], "clock_sources": ["scoreboard"]},
        "home-secret": {"service_id": "home-control", "game_ids": ["demo"],
                        "permissions": READS + ["players:write", "history:write"], "team_ids": ["demo-home"]},
    }
    monkeypatch.setenv("TOI_SERVICE_TOKENS", json.dumps(identities))
    app = create_app(f"sqlite:///{tmp_path / 'services.db'}")
    with TestClient(app) as instance:
        instance.identities = identities
        yield instance


def test_rest_live_player_and_lineup_share_projection(client):
    live = client.get("/api/v2/games/demo/live", headers=HEADERS)
    assert live.status_code == 200
    live = live.json()
    assert len(live["state"]["players"]) == 40
    assert not {"events", "shifts", "audit"} & live["state"].keys()
    on_ice = client.get("/api/v2/games/demo/on-ice", headers=HEADERS).json()
    assert [player["id"] for player in on_ice["home"]] == live["state"]["on_ice"]["home"]
    player = client.get("/api/v2/games/demo/players/demo-home-p02/toi", headers=HEADERS).json()
    assert player["player"] == next(player for player in live["state"]["players"] if player["id"] == "demo-home-p02")
    assert client.get("/api/v2/games/demo/players/missing/toi", headers=HEADERS).status_code == 404


def test_v2_fail_closed_and_game_scope_precedes_lookup(client):
    assert client.get("/api/v2/games/demo/live").status_code == 401
    assert client.get("/api/v2/games/not-permitted/live", headers=HEADERS).status_code == 403
    client.identities["operator-looking-secret"] = {"service_id": "reader", "game_ids": ["demo"], "permissions": ["stream:read"]}
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        frames = open_session(socket)
        assert [frame["type"] for frame in frames] == ["session.welcome", "state.snapshot", "stream.ready"]
        assert frames[0]["data"]["max_in_flight_commands"] == 1
        assert [frame["seq"] for frame in frames] == [1, 2, 3]
        assert len({frame["server_epoch"] for frame in frames}) == 1
        assert socket.accepted_subprotocol == PROTOCOLS[0]


@pytest.mark.parametrize("headers,protocols", [({}, PROTOCOLS), (HEADERS, []),
                         ({**HEADERS, "Origin": "http://untrusted.example"}, PROTOCOLS)])
def test_websocket_rejects_invalid_handshake(client, headers, protocols):
    headers = {key: value for key, value in headers.items() if key.lower() != "sec-websocket-protocol"}
    with pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)) as rejected:
        with client.websocket_connect(URL, headers=headers, subprotocols=protocols):
            pass
    # This intentionally accepts Starlette's HTTP-denial exception as well as
    # its portable pre-accept WebSocketDisconnect representation.
    assert rejected.value is not None


def test_duplex_move_receipt_order_and_restart_replay(client):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        opening = open_session(socket)
        baseline = opening[-1]["data"]["through_cursor"]
        submit = command("move", {"player_id": "demo-home-p02", "to": "PENALTY_BOX"}, 0)
        socket.send_json(submit)
        seen = []
        while not {"command.ack", "event.recorded"} <= {frame["type"] for frame in seen}:
            seen.append(socket.receive_json())
        ack = next(frame["data"] for frame in seen if frame["type"] == "command.ack")
        recorded = next(frame["data"]["record"] for frame in seen if frame["type"] == "event.recorded")
        assert ack["committed_version"] == recorded["version"] == 1
        assert ack["journal_cursor"] == recorded["cursor"]
        assert recorded["kind"] == "penalty.location.updated"
        assert recorded["actor"] == "service:controller"
        socket.send_json({**submit, "message_id": str(uuid4())})
        duplicate, _ = until(socket, "command.ack")
        assert duplicate["data"]["duplicate"] is True
    # New connection uses durable cursor rather than the replaced socket queue.
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        frames = open_session(socket, "resume", baseline)
        assert [frame["type"] for frame in frames] == ["session.welcome", "event.recorded", "state.snapshot", "stream.ready"]
        assert frames[1]["data"]["replay"] is True
        assert frames[1]["data"]["record"]["event_id"] == recorded["event_id"]
        assert frames[2]["data"]["state"]["version"] == 1
        assert [frame["seq"] for frame in frames] == [1, 2, 3, 4]


def test_read_only_and_team_permissions_precede_receipts(client):
    with client.websocket_connect(URL, headers={"Authorization": "Bearer graphics-secret"}, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json(command("move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0))
        error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "FORBIDDEN"
    with client.websocket_connect(URL, headers={"Authorization": "Bearer home-secret"}, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json(command("move", {"player_id": "demo-away-p02", "to": "BENCH"}, 0))
        error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "FORBIDDEN"
    assert client.get("/api/v1/games/demo/state").json()["version"] == 0


def test_external_bridge_clock_roundtrip_and_manual_fence(client):
    transfer = client.post("/api/v1/games/demo/clock/authority", json={
        "request_id": str(uuid4()), "expected_version": 0, "mode": "external",
        "source": "scoreboard", "service_id": "clock-bridge"})
    assert transfer.status_code == 200, transfer.text
    state = client.get("/api/v2/games/demo/live", headers=HEADERS).json()["state"]
    generation = state["clock_authority"]["generation"]
    with client.websocket_connect(URL, headers={"Authorization": "Bearer clock-secret"}, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        observation = message("clock.observe", {"request_id": str(uuid4()), "expected_version": 1,
            "authority_generation": generation, "source": "scoreboard", "source_sequence": 1,
            "observed_at_ms": now_ms(), "period": 1, "remaining_ms": 1_100_000, "running": True})
        socket.send_json(observation)
        ack, _ = until(socket, "command.ack")
        assert ack["data"]["committed_version"] == 2
        manual = client.post("/api/v1/games/demo/actions", json={"type": "clock", "payload": {"operation": "pause"},
                            "request_id": str(uuid4()), "expected_version": 2})
        assert manual.status_code == 200
        socket.send_json(message("clock.observe", {**observation["data"], "request_id": str(uuid4()),
                        "expected_version": 3, "source_sequence": 2, "observed_at_ms": now_ms() + 1}))
        error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "AUTHORITY_CONFLICT"
        # A committed identical retry still reports its old result after takeover.
        socket.send_json({**observation, "message_id": str(uuid4())})
        ack, _ = until(socket, "command.ack")
        assert ack["data"]["duplicate"] is True
        assert ack["data"]["current_version"] == 3


def test_journal_rest_fixed_barrier_and_typed_cursor_errors(client):
    initial = client.get("/api/v2/games/demo/journal", headers=HEADERS).json()
    baseline = initial["through_cursor"]
    assert len(initial["records"]) == 1
    client.post("/api/v1/games/demo/actions", json={"type": "move", "payload": {"player_id": "demo-home-p02", "to": "BENCH"},
                "request_id": str(uuid4()), "expected_version": 0})
    fixed = client.get("/api/v2/games/demo/journal", params={"through": baseline}, headers=HEADERS).json()
    assert fixed["records"] == initial["records"]
    assert fixed["has_more"] is False
    error = client.get("/api/v2/games/demo/journal", params={"after": "broken"}, headers=HEADERS)
    assert error.status_code == 409
    assert error.json()["code"] == "CURSOR_INVALID"
    assert client.get("/api/v2/games/demo/journal?limit=1001", headers=HEADERS).status_code == 422


def test_v1_changes_reach_services_and_live_tick_contains_all_players(client):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        opening = open_session(socket, tick_hz=10)
        client.post("/api/v1/games/demo/actions", json={"type": "move", "payload": {"player_id": "demo-home-p02", "to": "BENCH"},
                    "request_id": str(uuid4()), "expected_version": 0})
        tick, frames = until(socket, "live.tick")
        # A tick can race before the HTTP mutation; continue until revision one.
        while tick["data"]["version"] < 1:
            tick, subsequent = until(socket, "live.tick")
            frames += subsequent
        assert len(tick["data"]["toi"]) == 40
        assert "demo-home-p02" not in tick["data"]["on_ice"]["home"]
        record_index = next(index for index, frame in enumerate(frames) if frame["type"] == "event.recorded")
        tick_index = next(index for index, frame in enumerate(frames) if frame["type"] == "live.tick" and frame["data"]["version"] == 1)
        assert record_index < tick_index
        seq = [frame["seq"] for frame in opening + frames]
        assert seq == list(range(1, len(seq) + 1))


def test_state_request_ping_and_invalid_input_do_not_mutate(client):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json(message("ping", {"nonce": "roundtrip", "client_sent_at_ms": 123}))
        pong, _ = until(socket, "pong")
        assert pong["data"] == {"nonce": "roundtrip", "client_sent_at_ms": 123}
        socket.send_text('{"protocol":2,"protocol":2}')
        error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "INVALID_MESSAGE"
        socket.send_json(message("ping", {"nonce": "\ud800", "client_sent_at_ms": 123}))
        error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "INVALID_MESSAGE"
        socket.send_json(message("state.request", {}))
        snapshot, _ = until(socket, "state.snapshot")
        assert snapshot["data"]["reason"] == "requested"
        assert snapshot["data"]["version"] == 0


def test_scope_revocation_closes_existing_socket(client, monkeypatch):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        monkeypatch.setenv("TOI_SERVICE_TOKENS", "{}")
        socket.send_json(command("move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0))
        error, _ = until(socket, "session.error")
        assert error["data"]["code"] == "AUTHENTICATION_FAILED"


def test_disabled_api_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("TOI_STREAM_ENABLED", "false")
    monkeypatch.delenv("TOI_SERVICE_TOKENS", raising=False)
    app = create_app(f"sqlite:///{tmp_path / 'disabled.db'}")
    with TestClient(app) as client:
        assert client.get("/api/v2/games/demo/live").status_code == 404
        assert client.get("/api/v1/games/demo/state").status_code == 200


@pytest.mark.parametrize("value", ["[]", '{"secret":{"service_id":"x","game_ids":["demo"],"permissions":["root"]}}',
    '{"secret":{"service_id":"x","game_ids":[],"permissions":["stream:read"]}}',
    '{"secret":{"service_id":"x","game_ids":["demo"],"permissions":["stream:read"],"expires_at_ms":true}}',
    '{"secret":{"service_id":"x","game_ids":["demo"],"permissions":["stream:read"],"extra":true}}',
    '{"secret":{},"secret":{}}'])
def test_invalid_identity_config_fails_closed_without_secret_echo(monkeypatch, value):
    monkeypatch.setenv("TOI_SERVICE_TOKENS", value)
    with pytest.raises(ValueError) as error:
        service_identities()
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("name,value", [("TOI_STREAM_TICK_HZ", "3"), ("TOI_STREAM_ENABLED", "perhaps"),
    ("TOI_STREAM_QUEUE_MAX_BYTES", "12"), ("TOI_STREAM_MAX_CONNECTIONS", "-1"),
    ("TOI_STREAM_CLOCK_STALE_MS", "0"), ("TOI_STREAM_JOURNAL_RETENTION_DAYS", "3651")])
def test_invalid_stream_config_rejected(monkeypatch, name, value):
    monkeypatch.delenv("TOI_SERVICE_TOKENS", raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        StreamConfig()


def test_wire_validator_accepts_machine_contract_client_examples():
    document = json.loads((Path(__file__).resolve().parents[2] / "docs/interfaces/examples.json").read_text())
    for example in document["valid"]:
        if example["direction"] == "client":
            parsed = parse_message(json.dumps(example["message"]), example["message"]["game_id"])
            assert parsed == example["message"], example["name"]


@pytest.mark.parametrize("change", [
    lambda body: body.update(game_id="another-game"),
    lambda body: body.update(protocol=True),
    lambda body: body.update(message_id="not-uuid"),
    lambda body: body.update(type=[]),
    lambda body: body["data"].update(expected_version=2**60),
    lambda body: body["data"].update(expected_version=True),
    lambda body: body["data"]["command"]["payload"].update(to="INVALID"),
    lambda body: body["data"]["command"]["payload"].update(extra=True),
])
def test_wire_validator_rejects_invalid_and_unsafe_values(change):
    body = command("move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0)
    change(body)
    with pytest.raises(WireError):
        parse_message(json.dumps(body), "demo")


def test_queue_has_bounded_records_and_replaceable_tick(client):
    service = client.app.state.service_stream
    session = Session(service, None, resolve_service(HEADERS["Authorization"]), HEADERS["Authorization"], "demo")
    session.sample = {"version": 0}
    session.push("state.snapshot", {"value": "new"})
    assert session.sample is None
    for _ in range(128):
        session.push("test", {})
    assert len(session.queue) == 128
    assert session.fatal[0]["code"] == "SERVER_OVERLOADED"


def test_rate_buckets_keep_budget_across_reconnects():
    bucket = RateBucket(2, 5)
    assert [bucket.take() for _ in range(5)] == [0] * 5
    assert bucket.take() > 0


def test_rest_does_not_compute_once_per_same_version_subscriber(client):
    service = client.app.state.service_stream
    projection = client.get("/api/v2/games/demo/live", headers=HEADERS).json()
    assert service.feeds["demo"].projection["state"]["sample_seq"] == projection["state"]["sample_seq"]
    assert isinstance(service.feeds["demo"], GameFeed)


def test_actual_socket_frames_match_published_json_schema(client):
    jsonschema = pytest.importorskip("jsonschema", reason="Install docs/interfaces/requirements.txt for contract assertions")
    schema = json.loads((Path(__file__).resolve().parents[2] / "docs/interfaces/service-stream-v2.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        frames = open_session(socket, "beginning", tick_hz=10)
        socket.send_json(command("move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0))
        while not {"command.ack", "live.tick"} <= {frame["type"] for frame in frames}:
            frames.append(socket.receive_json())
        for frame in frames:
            validator.validate(frame)


def test_one_in_flight_mutation_is_enforced(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.store.apply_service

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.store, "apply_service", delayed)
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json(command("move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0))
        assert entered.wait(1)
        socket.send_json(command("move", {"player_id": "demo-home-p03", "to": "BENCH"}, 0))
        try:
            error, _ = until(socket, "command.error")
            assert error["data"]["code"] == "TOO_MANY_IN_FLIGHT"
        finally:
            release.set()
        ack, _ = until(socket, "command.ack")
        assert ack["data"]["committed_version"] == 1


def test_disconnect_during_transaction_keeps_durable_receipt(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.store.apply_service

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(client.app.state.store, "apply_service", delayed)
    submit = command("move", {"player_id": "demo-home-p02", "to": "BENCH"}, 0)
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json(submit)
        assert entered.wait(1)
        socket.close()
        release.set()
    # Context shutdown awaited the already admitted transaction instead of
    # pretending that a lost socket cancelled a database commit.
    monkeypatch.setattr(client.app.state.store, "apply_service", original)
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json({**submit, "message_id": str(uuid4())})
        ack, _ = until(socket, "command.ack")
        assert ack["data"]["duplicate"] is True


def test_clock_watchdog_runs_without_connected_services(client, monkeypatch):
    monkeypatch.setenv("TOI_STREAM_CLOCK_STALE_MS", "150")
    authority = client.post("/api/v1/games/demo/clock/authority", json={
        "request_id": str(uuid4()), "expected_version": 0, "mode": "external",
        "source": "scoreboard", "service_id": "clock-bridge"})
    assert authority.status_code == 200
    with client.websocket_connect(URL, headers={"Authorization": "Bearer clock-secret"}, subprotocols=PROTOCOLS) as socket:
        frames = open_session(socket)
        generation = frames[1]["data"]["state"]["clock_authority"]["generation"]
        socket.send_json(message("clock.observe", {"request_id": str(uuid4()), "expected_version": 1,
            "authority_generation": generation, "source": "scoreboard", "source_sequence": 1,
            "observed_at_ms": now_ms(), "period": 1, "remaining_ms": 1_100_000, "running": True}))
        until(socket, "command.ack")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = client.get("/api/v1/games/demo/state").json()
        if state["version"] == 3:
            break
        time.sleep(0.05)
    assert state["version"] == 3
    assert state["clock"]["running"] is False
    assert state["clock"]["playing_ms"] <= 150
    records = client.get("/api/v2/games/demo/journal", headers=HEADERS).json()["records"]
    assert [record["kind"] for record in records].count("clock.feed.stale") == 1


def test_invalid_resume_cursor_fails_instead_of_silently_using_now(client):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        frames = open_session(socket, "resume", "invalid-cursor")
        assert len(frames) == 1
        assert frames[0]["type"] == "session.error"
        assert frames[0]["data"]["code"] == "CURSOR_INVALID"


def test_large_initial_replay_demands_rest_catchup(client, monkeypatch):
    original = client.app.state.store.journal_page

    def large(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, "has_more": True}

    monkeypatch.setattr(client.app.state.store, "journal_page", large)
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        frames = open_session(socket, "beginning")
        assert [frame["type"] for frame in frames] == ["session.welcome", "replay.required"]
        assert frames[-1]["data"]["through_cursor"] == frames[0]["data"]["high_water_cursor"]
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1013


def test_revocation_is_rechecked_without_client_traffic(client, monkeypatch):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        monkeypatch.setenv("TOI_SERVICE_TOKENS", "{}")
        error, _ = until(socket, "session.error")
        assert error["data"]["code"] == "AUTHENTICATION_FAILED"


def test_heartbeat_deadline_closes_idle_client(client):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        session = next(iter(client.app.state.service_stream.connections))
        session.pending_ping = ({"nonce": "missed", "client_sent_at_ms": now_ms() - 6000}, time.monotonic() - 6)
        error, _ = until(socket, "session.error")
        assert error["data"]["code"] == "HEARTBEAT_TIMEOUT"


@pytest.mark.parametrize("payload", [b"binary application data", " " * 65_537], ids=["binary", "oversized"])
def test_binary_or_oversized_frames_are_rejected(client, payload):
    with client.websocket_connect(URL, headers=HEADERS, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        if isinstance(payload, bytes):
            socket.send_bytes(payload)
            error, _ = until(socket, "command.error")
        else:
            socket.send_text(payload)
            error, _ = until(socket, "session.error")
        assert error["data"]["code"] == "INVALID_MESSAGE"
        if isinstance(payload, str):
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1009


def test_clock_rate_limit_survives_reconnect(client):
    headers = {"Authorization": "Bearer clock-secret"}
    observation = {"request_id": str(uuid4()), "expected_version": 0, "authority_generation": 1,
        "source": "scoreboard", "source_sequence": 1, "observed_at_ms": now_ms(),
        "period": 1, "remaining_ms": 1_100_000, "running": False}
    # These observations are rejected because no clock owner was granted. Their
    # admission still consumes the service/game budget, preventing an invalid
    # write flood from bypassing limits or resetting them by reconnecting.
    with client.websocket_connect(URL, headers=headers, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        for _ in range(6):
            socket.send_json(message("clock.observe", {**observation, "request_id": str(uuid4())}))
            error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "RATE_LIMITED"
        assert error["data"]["retry_after_ms"] > 0
    with client.websocket_connect(URL, headers=headers, subprotocols=PROTOCOLS) as socket:
        open_session(socket)
        socket.send_json(message("clock.observe", {**observation, "request_id": str(uuid4())}))
        error, _ = until(socket, "command.error")
        assert error["data"]["code"] == "RATE_LIMITED"
