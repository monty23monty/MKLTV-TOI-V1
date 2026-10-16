"""Exercise the real v2 service interface through nginx; stdout is checkpoint JSON.

Run inside the backend image, like smoke_portainer.py. It creates its own paused
match, never changes a pre-existing operational match, and uses configured CI
operator/service credentials. --verify-game checks the same journal after restart.
"""

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


class StreamSession:
    def __init__(self, *args, **kwargs):
        self.context = connect(*args, **kwargs)
        self.socket = self.context.__enter__()
        self.closed = False

    def send(self, data):
        self.socket.send(data)

    def recv(self, **kwargs):
        return self.socket.recv(**kwargs)

    def close(self):
        if not self.closed:
            self.closed = True
            self.context.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Client:
    def __init__(self, base, origin):
        self.base = base.rstrip("/")
        self.origin = origin
        self.operator = os.getenv("TOI_AUTH_TOKEN", "")
        tokens = json.loads(os.getenv("TOI_SERVICE_TOKENS", "{}") or "{}")
        self.token = os.getenv("TOI_SERVICE_TOKEN") or next(iter(tokens), "")
        require(self.token in tokens, "Smoke test requires a configured scoped service token")
        details = tokens[self.token]
        self.service_id = details["service_id"]
        self.source = details["clock_sources"][0]

    def http(self, path, body=None, *, service=True, authenticated=True, expected=200):
        token = self.token if service else self.operator
        headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=payload, headers=headers)
        try:
            with urlopen(request, timeout=15) as response:
                require(response.status == expected, f"Unexpected HTTP status for {path}")
                return json.load(response)
        except HTTPError as error:
            require(error.code == expected, f"HTTP {error.code} for {path.split('?')[0]}")
            return json.load(error)

    def open(self, game_id, after=None, tick_hz=0):
        parts = urlsplit(self.base)
        url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                          f"/api/v2/games/{game_id}/stream", "", ""))
        socket = StreamSession(url, subprotocols=["lightning.toi.stream.v2"],
                         additional_headers={"Authorization": f"Bearer {self.token}"},
                         origin=self.origin, open_timeout=10, close_timeout=2)
        data = {"client_id": "container-smoke", "journal_mode": "resume" if after else "now", "tick_hz": tick_hz}
        if after:
            data["after_cursor"] = after
        send(socket, game_id, "session.open", data)
        frames = []
        for _ in range(1100):
            event = receive(socket, game_id)
            frames.append(event)
            if event["type"] == "stream.ready":
                require(any(frame["type"] == "session.welcome" for frame in frames), "Missing welcome")
                snapshot = next(frame for frame in frames if frame["type"] == "state.snapshot")
                return socket, frames, snapshot["data"]
            require(event["type"] not in {"session.error", "replay.required"}, "Bootstrap failed")
        raise RuntimeError("Stream never became ready")


def send(socket, game_id, kind, data):
    socket.send(json.dumps({"protocol": 2, "type": kind, "message_id": str(uuid4()),
                            "game_id": game_id, "data": data}))


def receive(socket, game_id):
    event = json.loads(socket.recv(timeout=10))
    require(event.get("protocol") == 2 and event.get("game_id") == game_id, "Invalid server envelope")
    if event["type"] == "ping":
        send(socket, game_id, "pong", event["data"])
        return receive(socket, game_id)
    return event


def command(socket, game_id, data, *, kind="command.submit", duplicate=False, errors=None):
    started = time.monotonic()
    send(socket, game_id, kind, data)
    ack = None
    record = None
    for _ in range(100):
        event = receive(socket, game_id)
        if event["type"] == "command.error" and event["data"]["request_id"] == data["request_id"]:
            require(errors and event["data"]["code"] in errors, f"Command rejected: {event['data']['code']}")
            return event["data"], None, (time.monotonic() - started) * 1000
        if event["type"] == "command.ack" and event["data"]["request_id"] == data["request_id"]:
            ack = event["data"]
            require(ack["duplicate"] == duplicate, "Wrong idempotency outcome")
        if event["type"] == "event.recorded" and event["data"]["record"]["request_id"] == data["request_id"]:
            record = event["data"]["record"]
        if ack is not None and (duplicate or record is not None):
            require(not errors, "Expected command rejection")
            return ack, record, (time.monotonic() - started) * 1000
    raise RuntimeError("No matching command outcome and journal record")


def action(version, kind, payload):
    return {"request_id": str(uuid4()), "expected_version": version,
            "command": {"type": kind, "payload": payload}}


def verify(client, game_id, cursor):
    prefix = f"/api/v2/games/{game_id}"
    live = client.http(prefix + "/live")
    require(live["baseline_cursor"] == cursor, "Journal head changed across restart")
    page = client.http(prefix + "/journal?" + urlencode({"after": cursor}))
    require(page["records"] == [] and page["next_cursor"] == cursor, "Saved cursor did not survive restart")
    socket, frames, _snapshot = client.open(game_id, after=cursor)
    epoch = frames[0]["server_epoch"]
    with socket:
        send(socket, game_id, "state.request", {})
        require(receive(socket, game_id)["type"] == "state.snapshot", "Snapshot unavailable after restart")
    require(live["state"]["clock_authority"]["mode"] == "manual", "Manual takeover did not persist")
    require(not live["state"]["clock"]["running"], "Training clock unexpectedly running")
    require(all(player["total_toi_ms"] == 0 for player in live["state"]["players"]), "Paused TOI changed")
    return {"game_id": game_id, "cursor": cursor, "version": live["state"]["version"],
            "server_epoch": epoch, "persistence": "verified"}


def exercise(client):
    health = client.http("/health", authenticated=False)
    require("v2" in health.get("api_versions", []), "V2 is not enabled")
    state = client.http("/api/v1/games", {"name": "V2 container smoke"}, service=False, expected=201)
    game_id = state["game"]["id"]
    prefix = f"/api/v2/games/{game_id}"
    client.http(prefix + "/live", authenticated=False, expected=401)
    player = next(player for player in state["players"] if player["location"] == "ON_ICE" and player["position"] != "G")
    socket, frames, snapshot = client.open(game_id)
    epoch = frames[0]["server_epoch"]
    baseline = snapshot["baseline_cursor"]
    latencies = []
    try:
        move = action(snapshot["version"], "move", {"player_id": player["id"], "to": "PENALTY_BOX"})
        ack, record, latency = command(socket, game_id, move)
        latencies.append(latency)
        require(record["cursor"] == ack["journal_cursor"], "ACK is not linked to its committed record")
        duplicate_ack, _, _ = command(socket, game_id, move, duplicate=True)
        require(duplicate_ack["committed_version"] == ack["committed_version"], "Retry produced another mutation")
        individual = client.http(prefix + f"/players/{player['id']}/toi")
        require(individual["player"]["location"] == "PENALTY_BOX", "Individual TOI read is stale")
        on_ice = client.http(prefix + "/on-ice")
        require(all(p["id"] != player["id"] for p in on_ice["home"] + on_ice["away"]), "On-ice projection is stale")
    finally:
        socket.close()
    socket, replay, snapshot = client.open(game_id, after=baseline)
    try:
        replayed = [frame["data"]["record"] for frame in replay if frame["type"] == "event.recorded"]
        require(len(replayed) == 1 and replayed[0]["request_id"] == move["request_id"], "Recorded change was not replayed exactly once in this catch-up")
        handover = action(snapshot["version"], "clock.authority.set",
                          {"mode": "external", "source": client.source, "service_id": client.service_id})
        _, _, latency = command(socket, game_id, handover)
        latencies.append(latency)
        live = client.http(prefix + "/live")["state"]
        generation = live["clock_authority"]["generation"]
        observation = {"request_id": str(uuid4()), "expected_version": live["version"],
                       "authority_generation": generation, "source": client.source, "source_sequence": 1,
                       "observed_at_ms": time.time_ns() // 1_000_000, "period": 1,
                       "remaining_ms": 1_100_000, "running": False}
        _, _, latency = command(socket, game_id, observation, kind="clock.observe")
        latencies.append(latency)
        live = client.http(prefix + "/live")["state"]
        require(live["clock"]["remaining_ms"] == 1_100_000 and live["clock_authority"]["feed_status"] == "fresh", "Clock observation not applied")
        manual = client.http(f"/api/v1/games/{game_id}/actions",
                             {"request_id": str(uuid4()), "expected_version": live["version"],
                              "type": "clock", "payload": {"operation": "pause"}}, service=False)
        require(manual["clock_authority"]["generation"] > generation, "Operator takeover did not fence the source")
        old_packet = {**observation, "request_id": str(uuid4()), "expected_version": manual["version"],
                      "source_sequence": 2, "observed_at_ms": time.time_ns() // 1_000_000 + 1}
        command(socket, game_id, old_packet, kind="clock.observe", errors={"CLOCK_NOT_OWNER", "AUTHORITY_CONFLICT"})
        undo = action(manual["version"], "undo", {})
        _, _, latency = command(socket, game_id, undo)
        latencies.append(latency)
    finally:
        socket.close()
    live = client.http(prefix + "/live")
    require(all(p["total_toi_ms"] == 0 for p in live["state"]["players"]), "Stopped clock accrued TOI")
    page = client.http(prefix + "/journal")
    records = page["records"]
    require(len(records) >= 6 and len({r["event_id"] for r in records}) == len(records), "Journal lost or duplicated committed records")
    require(records[-1]["cursor"] == live["baseline_cursor"], "Live baseline/journal disagree")
    require([r["journal_seq"] for r in records] == list(range(1, len(records) + 1)), "Journal sequence has gaps")
    return {"game_id": game_id, "cursor": live["baseline_cursor"], "version": live["state"]["version"],
            "server_epoch": epoch,
            "records": len(records), "max_smoke_round_trip_ms": round(max(latencies), 2),
            "checks": "auth,duplex,TOI,lineups,journal,retry,replay,clock,manual-fence,undo"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://frontend")
    parser.add_argument("--origin", default="http://localhost:8080")
    parser.add_argument("--verify-game")
    parser.add_argument("--cursor")
    args = parser.parse_args()
    try:
        client = Client(args.base_url, args.origin)
        result = verify(client, args.verify_game, args.cursor) if args.verify_game else exercise(client)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (RuntimeError, ValueError, KeyError, OSError, TimeoutError, WebSocketException) as error:
        detail = str(error)
        for value in (os.getenv("TOI_AUTH_TOKEN"), os.getenv("TOI_SERVICE_TOKEN")):
            if value:
                detail = detail.replace(value, "[redacted]")
        print(f"V2 smoke failed ({type(error).__name__}): {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
