"""Short, real TCP fanout check; intentionally not the documented three-hour SLO test.

Starts an isolated loopback uvicorn backend with temporary SQLite, preloads a
history, then measures one clock plus two player writes/second with 20 v2 and
two v1 sockets. Nothing touches an existing match or a deployed service.
"""

import argparse
import asyncio
import json
import math
import os
import platform
import secrets
import site
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
PERMISSIONS = ["stream:read", "journal:read", "players:write", "clock:write", "clock:control", "clock:authority"]
PROTOCOL = "lightning.toi.stream.v2"


def http(base, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def envelope(kind, data):
    return {"protocol": 2, "type": kind, "message_id": str(uuid4()), "game_id": "demo", "data": data}


def percentiles(values):
    ordered = sorted(values)

    def percentile(fraction):
        offset = (len(ordered) - 1) * fraction
        lower, upper = math.floor(offset), math.ceil(offset)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (offset - lower), 3)

    return {"count": len(ordered), "min_ms": round(ordered[0], 3), "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95), "p99_ms": percentile(0.99), "max_ms": round(ordered[-1], 3)}


class Observer:
    def __init__(self, identity, socket_instance, sent, received, futures):
        self.identity, self.socket = identity, socket_instance
        self.sent, self.received, self.futures = sent, received, futures
        self.seq = 0
        self.journal_seq = None
        self.tick_count = 0
        self.tick_times = []
        self.frames = 0
        self.bootstrap_ms = None

    async def handle(self, frame):
        received_at = time.perf_counter()
        assert frame["seq"] == self.seq + 1, "Connection frame sequence gap"
        self.seq = frame["seq"]
        self.frames += 1
        kind, data = frame["type"], frame["data"]
        if kind == "ping":
            await self.socket.send(json.dumps(envelope("pong", data)))
        elif kind == "event.recorded":
            record = data["record"]
            if self.journal_seq is not None:
                assert record["journal_seq"] == self.journal_seq + 1, "Journal sequence gap"
            self.journal_seq = record["journal_seq"]
            request_id = record["request_id"]
            if request_id in self.sent:
                self.received[request_id][self.identity] = (received_at - self.sent[request_id]) * 1000
        elif kind == "live.tick":
            assert len(data["toi"]) == 40, "Incomplete TOI tick"
            self.tick_count += 1
            self.tick_times.append(received_at)
        elif kind == "command.ack":
            future = self.futures.get(data["request_id"])
            if future and not future.done():
                future.set_result((data, received_at))
        elif kind in {"command.error", "session.error", "replay.required"}:
            raise AssertionError(f"Benchmark protocol rejection: {kind}: {data.get('code')}")

    async def run(self):
        async for raw in self.socket:
            await self.handle(json.loads(raw))


async def measure(base, ws_base, tokens, mutation_count):
    sent, received, futures = {}, {}, {}
    observers, readers, operators = [], [], []
    operator_updates = [0, 0]

    async def attach(index, token):
        opened_at = time.perf_counter()
        stream = await connect(ws_base + "/api/v2/games/demo/stream", subprotocols=[PROTOCOL],
                               additional_headers={"Authorization": f"Bearer {token}"}, max_size=1_048_576)
        observer = Observer(index, stream, sent, received, futures)
        await stream.send(json.dumps(envelope("session.open", {"client_id": f"benchmark-{index:02d}",
                                                               "journal_mode": "now", "tick_hz": 5})))
        while True:
            frame = json.loads(await stream.recv())
            await observer.handle(frame)
            if frame["type"] == "stream.ready":
                break
        observer.bootstrap_ms = (time.perf_counter() - opened_at) * 1000
        return observer

    async def operator_reader(index, stream):
        async for raw in stream:
            frame = json.loads(raw)
            assert frame["game_id"] == "demo"
            if "state" in frame:
                assert len(frame["state"]["players"]) == 40
            elif frame["type"] == "clock.tick":
                assert len(frame["toi"]) == 40
            operator_updates[index] += 1

    try:
        observers = list(await asyncio.gather(*(attach(index, token) for index, token in enumerate(tokens))))
        readers = [asyncio.create_task(observer.run()) for observer in observers]
        for index in range(2):
            operator = await connect(ws_base + "/api/v1/games/demo/ws", max_size=16_777_216)
            operators.append(operator)
            readers.append(asyncio.create_task(operator_reader(index, operator)))
        state = await asyncio.to_thread(http, base, "/api/v1/games/demo/state")
        version, generation = state["version"], state["clock_authority"]["generation"]
        writer = observers[0].socket
        acknowledgements = []
        start = time.perf_counter()
        clock_sequence = 0
        moves = 0
        for index in range(mutation_count):
            await asyncio.sleep(max(0, start + index / 3 - time.perf_counter()))
            request_id = str(uuid4())
            data = {"request_id": request_id, "expected_version": version}
            if index % 3 == 0:
                clock_sequence += 1
                kind = "clock.observe"
                data.update(authority_generation=generation, source="benchmark-clock", source_sequence=clock_sequence,
                            observed_at_ms=time.time_ns() // 1_000_000, period=1,
                            remaining_ms=max(1, 1_100_000 - int((time.perf_counter() - start) * 1000)), running=True)
            else:
                moves += 1
                kind = "command.submit"
                data["command"] = {"type": "move", "payload": {"player_id": "demo-home-p02",
                                                                   "to": "BENCH" if moves % 2 else "ON_ICE"}}
            future = asyncio.get_running_loop().create_future()
            futures[request_id] = future
            received[request_id] = {}
            sent[request_id] = time.perf_counter()
            await writer.send(json.dumps(envelope(kind, data)))
            ack, acknowledged_at = await asyncio.wait_for(future, 5)
            assert ack["duplicate"] is False
            version = ack["committed_version"]
            acknowledgements.append((acknowledged_at - sent[request_id]) * 1000)
            for task in readers:
                if task.done():
                    task.result()
        deadline = time.perf_counter() + 3
        while any(len(deliveries) != len(observers) for deliveries in received.values()):
            if time.perf_counter() > deadline:
                raise AssertionError("Not every consumer received every committed journal record")
            await asyncio.sleep(0.01)
        elapsed = time.perf_counter() - start
        assert all(observer.tick_count for observer in observers)
        assert all(count >= mutation_count for count in operator_updates)
        maximum_delivery = [max(deliveries.values()) for deliveries in received.values()]
        individual_delivery = [latency for deliveries in received.values() for latency in deliveries.values()]
        tick_intervals = [(right - left) * 1000 for observer in observers
                          for left, right in zip(observer.tick_times, observer.tick_times[1:])]
        return {
            "duration_seconds": round(elapsed, 3), "service_connections": len(observers), "v1_operator_connections": len(operators),
            "mutations": mutation_count, "clock_observations": clock_sequence, "player_moves": moves,
            "ack_latency": percentiles(acknowledgements), "all_consumers_record_latency": percentiles(maximum_delivery),
            "individual_record_latency": percentiles(individual_delivery),
            "bootstrap_latency": percentiles([observer.bootstrap_ms for observer in observers]),
            "tick_interval": percentiles(tick_intervals),
            "ticks_per_consumer": [observer.tick_count for observer in observers],
            "v1_operator_updates": operator_updates, "frame_sequence_gaps": 0, "journal_sequence_gaps": 0,
            "records_expected": mutation_count * len(observers), "records_received": len(individual_delivery),
            "all_ticks_contain_40_players": True,
        }
    finally:
        for observer in observers:
            await observer.socket.close()
        for operator in operators:
            await operator.close()
        for task in readers:
            task.cancel()
        for task in readers:
            with suppress(asyncio.CancelledError):
                await task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--preload", type=int, default=200)
    parser.add_argument("--mutations", type=int, default=30)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/service-network-benchmark.json")
    args = parser.parse_args()
    if args.preload < 0 or args.preload % 2 or args.mutations < 3:
        parser.error("preload must be nonnegative/even; mutations must be at least three")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", args.port))
    base, ws_base = f"http://127.0.0.1:{args.port}", f"ws://127.0.0.1:{args.port}"
    tokens = [secrets.token_urlsafe(32) for _ in range(20)]
    identities = {token: {"service_id": f"benchmark-{index:02d}", "game_ids": ["demo"],
                          "permissions": PERMISSIONS if index == 0 else ["stream:read", "journal:read"],
                          "clock_sources": ["benchmark-clock"] if index == 0 else []}
                  for index, token in enumerate(tokens)}
    env = {key: value for key, value in os.environ.items() if not key.startswith("TOI_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "backend"), *site.getsitepackages()])
    env["TOI_STREAM_ENABLED"], env["TOI_SERVICE_TOKENS"] = "true", json.dumps(identities)
    executable = getattr(sys, "_base_executable", sys.executable)
    with tempfile.TemporaryDirectory(prefix="toi-network-benchmark-") as directory:
        database = Path(directory) / "benchmark.db"
        env["TOI_DATABASE_URL"] = "sqlite:///" + str(database).replace("\\", "/")
        shutdown_secret = secrets.token_urlsafe(32)
        env["BENCHMARK_SHUTDOWN_SECRET"] = shutdown_secret
        child_code = (
            "import os, uvicorn\nfrom fastapi import Header, HTTPException\nfrom toi.main import app\n"
            f"server=uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port={args.port}, log_level='warning'))\n"
            "async def shutdown(authorization: str = Header(default='')):\n"
            "    if authorization != os.environ['BENCHMARK_SHUTDOWN_SECRET']: raise HTTPException(403)\n"
            "    server.should_exit=True\n    return {'stopping': True}\n"
            "app.add_api_route('/_benchmark/shutdown', shutdown, methods=['POST'])\nserver.run()\n")
        log_path = Path(directory) / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen([executable, "-c", child_code], cwd=ROOT, env=env, stdout=log,
                                       stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        http(base, "/health")
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Isolated benchmark backend failed to start") from None
                        time.sleep(0.05)
                preload_start = time.perf_counter()
                version = 0
                for index in range(args.preload):
                    state = http(base, "/api/v1/games/demo/actions", {"request_id": str(uuid4()), "expected_version": version,
                        "type": "move", "payload": {"player_id": "demo-home-p02", "to": "BENCH" if index % 2 == 0 else "ON_ICE"}})
                    version = state["version"]
                preload_seconds = time.perf_counter() - preload_start
                http(base, "/api/v1/games/demo/clock/authority", {"request_id": str(uuid4()), "expected_version": version,
                     "mode": "external", "source": "benchmark-clock", "service_id": "benchmark-00"})
                size_before = database.stat().st_size
                metrics = asyncio.run(measure(base, ws_base, tokens, args.mutations))
                metrics.update(preloaded_player_changes=args.preload, preload_seconds=round(preload_seconds, 3),
                               sqlite_bytes_before=size_before, sqlite_bytes_after=database.stat().st_size,
                               platform=platform.platform(), python=platform.python_version(), database="temporary SQLite",
                               transport="real loopback TCP uvicorn HTTP/WebSocket; no nginx, TLS or LAN",
                               verdict="Bounded local measurement only; not the three-hour PostgreSQL/LAN reference SLO",
                               observed_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(metrics, separators=(",", ":")))
            finally:
                if process.poll() is None:
                    with suppress(OSError):
                        request = Request(base + "/_benchmark/shutdown", data=b"{}",
                                          headers={"Authorization": shutdown_secret, "Content-Type": "application/json"})
                        with urlopen(request, timeout=5):
                            pass
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
