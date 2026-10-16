"""Shared admission remains bounded, FIFO and recoverable after cancellation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from toi.coordinator import MutationCoordinator
from toi.main import create_app
from toi.store import StoreError


def test_admission_limit_is_per_game_and_cancellation_recovers_capacity():
    async def exercise():
        coordinator = MutationCoordinator(None, None, max_admissions=2)
        entered = []

        async def queued(name):
            async with coordinator.lock("demo"):
                entered.append(name)

        async with coordinator.lock("demo"):
            first = asyncio.create_task(queued("cancelled"))
            await asyncio.sleep(0)
            with pytest.raises(StoreError) as overloaded:
                async with coordinator.lock("demo"):
                    raise AssertionError("An over-capacity caller was admitted")
            assert overloaded.value.status == 503
            assert overloaded.value.code == "SERVER_OVERLOADED"
            # Another game's work can enter independently.
            async with coordinator.lock("another-game"):
                pass
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            replacement = asyncio.create_task(queued("replacement"))
            await asyncio.sleep(0)
            assert coordinator._admissions["demo"] == 2
        await replacement
        assert entered == ["replacement"]
        assert not coordinator._admissions
        # An exception from an admitted operation must also release capacity.
        with pytest.raises(RuntimeError):
            async with coordinator.lock("demo"):
                raise RuntimeError("operation failed")
        assert not coordinator._admissions

    asyncio.run(exercise())


def test_admitted_operations_keep_fifo_order():
    async def exercise():
        coordinator = MutationCoordinator(None, None, max_admissions=5)
        order = []

        async def operation(index):
            async with coordinator.lock("demo"):
                order.append(index)

        async with coordinator.lock("demo"):
            tasks = [asyncio.create_task(operation(index)) for index in range(4)]
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)
        assert order == [0, 1, 2, 3]

    asyncio.run(exercise())


def test_http_and_socket_overload_reject_without_mutating_and_recover(tmp_path, monkeypatch):
    monkeypatch.setenv("TOI_STREAM_ENABLED", "true")
    monkeypatch.delenv("TOI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TOI_OPERATOR_TOKENS", raising=False)
    monkeypatch.setenv("TOI_SERVICE_TOKENS", json.dumps({"test-token": {
        "service_id": "controller", "game_ids": ["demo"],
        "permissions": ["stream:read", "journal:read", "players:write"]}}))
    app = create_app(f"sqlite:///{tmp_path / 'admission.db'}")
    entered, release = threading.Event(), threading.Event()

    async def hold():
        async with app.state.coordinator.lock("demo"):
            entered.set()
            assert await run_in_threadpool(release.wait, 5)
        return {"released": True}

    app.add_api_route("/_test/hold", hold)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        # Isolate admission from the periodic worker, which legitimately uses
        # the same budget and could occupy our deliberately tiny single slot.
        client.portal.call(app.state.service_stream.stop)
        # Normal operation uses 64; one slot makes overflow deterministic here.
        app.state.coordinator.max_admissions = 1
        with client.websocket_connect("/api/v2/games/demo/stream", headers=headers,
                                      subprotocols=["lightning.toi.stream.v2"]) as socket:
            socket.send_json({"protocol": 2, "type": "session.open", "message_id": str(uuid4()), "game_id": "demo",
                              "data": {"client_id": "admission-test", "journal_mode": "now", "tick_hz": 0}})
            while socket.receive_json()["type"] != "stream.ready":
                pass
            with ThreadPoolExecutor(max_workers=1) as executor:
                holding = executor.submit(client.get, "/_test/hold")
                assert entered.wait(2)
                mutation = {"request_id": str(uuid4()), "expected_version": 0,
                            "type": "move", "payload": {"player_id": "demo-home-p02", "to": "BENCH"}}
                try:
                    rejected = client.post("/api/v1/games/demo/actions", json=mutation)
                    assert rejected.status_code == 503
                    rejected_live = client.get("/api/v2/games/demo/live", headers=headers)
                    assert rejected_live.status_code == 503
                    assert rejected_live.json()["code"] == "SERVER_OVERLOADED"
                    with pytest.raises(WebSocketDisconnect) as legacy_closed:
                        with client.websocket_connect("/api/v1/games/demo/ws"):
                            raise AssertionError("Overloaded legacy socket was accepted")
                    assert legacy_closed.value.code == 1013
                    socket_command = {"protocol": 2, "type": "command.submit", "message_id": str(uuid4()), "game_id": "demo",
                        "data": {"request_id": mutation["request_id"], "expected_version": 0,
                                 "command": {"type": mutation["type"], "payload": mutation["payload"]}}}
                    socket.send_json(socket_command)
                    frame = socket.receive_json()
                    assert frame["type"] == "command.error"
                    assert frame["data"]["code"] == "RATE_LIMITED"
                    assert frame["data"]["retryable"] is True
                    assert frame["data"]["retry_after_ms"] == 100
                    # A fresh connection gets a typed temporary-overload close.
                    with client.websocket_connect("/api/v2/games/demo/stream", headers=headers,
                                                  subprotocols=["lightning.toi.stream.v2"]) as another:
                        another.send_json({"protocol": 2, "type": "session.open", "message_id": str(uuid4()), "game_id": "demo",
                            "data": {"client_id": "busy-test", "journal_mode": "now", "tick_hz": 0}})
                        frame = another.receive_json()
                        assert frame["type"] == "session.error"
                        assert frame["data"]["code"] == "SERVER_OVERLOADED"
                        with pytest.raises(WebSocketDisconnect) as closed:
                            another.receive_json()
                        assert closed.value.code == 1013
                finally:
                    release.set()
                    assert holding.result(timeout=2).status_code == 200
            # Retry the identical intent after pressure clears. No rejected
            # attempt wrote a receipt, a journal record or a state version.
            socket.send_json({**socket_command, "message_id": str(uuid4())})
            while True:
                frame = socket.receive_json()
                if frame["type"] == "command.ack":
                    break
            assert frame["data"]["committed_version"] == 1
            assert frame["data"]["duplicate"] is False
