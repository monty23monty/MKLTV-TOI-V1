"""Exercise a running container stack through nginx, without external providers.

Run inside the backend image so its locked WebSocket dependency is available:
  docker compose -f compose.portainer.yaml exec -T backend python - < scripts/smoke_portainer.py
The only stdout is JSON suitable for saving as a persistence checkpoint.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from copy import deepcopy
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect


class SmokeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type") == "module" and attributes.get("src"):
            self.scripts.append(attributes["src"])


class StackClient:
    def __init__(self, base_url, token, origin):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.origin = origin

    def request(self, path, *, method="GET", body=None, authenticate=True, expected=200):
        headers = {"Authorization": f"Bearer {self.token}"} if authenticate else {}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as error:
            if error.code == expected:
                error.close()
                return b"", {}
            raise SmokeFailure(f"{method} {path.split('?')[0]} returned {error.code}, expected {expected}") from None
        with response:
            require(response.status == expected, f"Unexpected HTTP status for {method} {path.split('?')[0]}")
            return response.read(), dict(response.headers)

    def json(self, path, **kwargs):
        payload, _headers = self.request(path, **kwargs)
        return json.loads(payload)

    def command(self, state, kind, payload):
        return self.json(f"/api/v1/games/{state['game']['id']}/actions", method="POST", body={
            "request_id": f"container-smoke-{uuid4()}", "expected_version": state["version"],
            "type": kind, "payload": payload,
        })

    def websocket_url(self, game_id, *, authenticate=True):
        base = urlsplit(self.base_url)
        query = urlencode({"token": self.token}) if authenticate else ""
        return urlunsplit(("wss" if base.scheme == "https" else "ws", base.netloc,
                           f"/api/v1/games/{game_id}/ws", query, ""))

    def websocket(self, game_id, *, authenticate=True):
        return connect(self.websocket_url(game_id, authenticate=authenticate), origin=self.origin,
                       proxy=None, open_timeout=15, close_timeout=3)


def fingerprint(state):
    stable = deepcopy(state)
    stable.pop("server_time_ms", None)
    stable["clock"].pop("anchor_unix_ms", None)
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def receive_event(socket, expected_type, expected_version):
    deadline = time.monotonic() + 15
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, f"Timed out waiting for {expected_type}")
        event = json.loads(socket.recv(timeout=remaining))
        if event["type"] == "clock.tick":
            continue
        require(event["type"] == expected_type, f"Expected WebSocket {expected_type}, got {event['type']}")
        require(event["version"] == expected_version, "WebSocket version differs from HTTP command response")
        return event


def check_routes(client):
    page, _headers = client.request("/", authenticate=False)
    require(b'Lightning TV' in page and b'id="root"' in page, "nginx did not serve the operator HTML")
    assets = AssetParser()
    assets.feed(page.decode())
    require(assets.scripts, "Built frontend module asset is missing")
    for asset in assets.scripts:
        path = urlsplit(urljoin(f"{client.base_url}/", asset)).path
        script, _headers = client.request(path, authenticate=False)
        require(len(script) > 100 and not script.lstrip().startswith(b"<!doctype"),
                "nginx did not serve a built JavaScript asset")
    require(client.json("/health?smoke=1", authenticate=False)["status"] == "ok", "Database health failed")
    require("paths" in client.json("/openapi.json?smoke=1", authenticate=False), "OpenAPI proxy failed")
    client.request("/api/v1/games", authenticate=False, expected=401)
    games = client.json("/api/v1/games")
    require(isinstance(games, list) and games, "Authenticated game listing failed")


def check_websocket_auth(client, game_id):
    try:
        with client.websocket(game_id, authenticate=False):
            raise SmokeFailure("Unauthenticated WebSocket was accepted")
    except InvalidStatus as error:
        require(error.response.status_code == 403, "Unauthenticated WebSocket returned an unexpected status")


def create_and_exercise(client):
    state = client.json("/api/v1/games", method="POST", expected=201, body={
        "name": f"Container smoke training {uuid4()}", "home_name": "Smoke Home", "away_name": "Smoke Away",
    })
    game_id = state["game"]["id"]
    require(not state["clock"]["running"], "Training game must start paused")
    require(len(state["teams"]) == 2 and len(state["players"]) == 40, "Training roster is incomplete")
    before_locations = {player["id"]: player["location"] for player in state["players"]}
    home = next(team for team in state["teams"] if team["side"] == "home")
    outgoing = next(player for player in state["players"] if player["team_id"] == home["id"]
                    and player["location"] == "ON_ICE" and player["position"] != "G")
    suggestions = client.json(f"/api/v1/games/{game_id}/suggestions?" + urlencode({
        "team_id": home["id"], "outgoing_ids": outgoing["id"],
    }))
    require(suggestions["players"], "Recommendation endpoint returned no eligible players")
    incoming_id = suggestions["players"][0]["player_id"]
    check_websocket_auth(client, game_id)
    with client.websocket(game_id) as socket:
        initial = receive_event(socket, "state.resync", state["version"])
        require(fingerprint(initial["state"]) == fingerprint(state), "WebSocket initial state differs from HTTP")
        changed = client.command(state, "substitute", {
            "team_id": home["id"], "outgoing_ids": [outgoing["id"]], "incoming_ids": [incoming_id],
        })
        players = {player["id"]: player for player in changed["players"]}
        require(players[outgoing["id"]]["location"] == "BENCH", "Outgoing player did not move to bench")
        require(players[incoming_id]["location"] == "ON_ICE", "Suggested player did not enter ice")
        event = receive_event(socket, "substitution.updated", changed["version"])
        require(fingerprint(event["state"]) == fingerprint(changed), "Substitution broadcast differs from HTTP")
        restored = client.command(changed, "undo", {})
        receive_event(socket, "correction.updated", restored["version"])
    require(before_locations == {player["id"]: player["location"] for player in restored["players"]},
            "Undo did not restore the lineup")
    require(restored["clock"]["playing_ms"] == 0 and not restored["clock"]["running"],
            "Paused smoke operations changed game time")
    require(all(player["total_toi_ms"] == 0 for player in restored["players"]),
            "Paused smoke operations accrued TOI")
    require(any(event.get("voided") for event in restored["events"]), "Undo history was not retained")
    fetched = client.json(f"/api/v1/games/{game_id}/state?smoke=1")
    require(fingerprint(fetched) == fingerprint(restored), "Stored state differs from the last command")
    return fetched


def verify_persistence(client, game_id, expected_fingerprint):
    state = client.json(f"/api/v1/games/{game_id}/state?smoke=1")
    require(fingerprint(state) == expected_fingerprint, "Game state changed across container recreation")
    check_websocket_auth(client, game_id)
    with client.websocket(game_id) as socket:
        initial = receive_event(socket, "state.resync", state["version"])
        require(fingerprint(initial["state"]) == expected_fingerprint, "Persisted WebSocket state differs")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://frontend")
    parser.add_argument("--origin", help="Browser origin to exercise; defaults to base URL")
    parser.add_argument("--verify-game", help="Verify an existing game after recreating containers")
    parser.add_argument("--fingerprint", help="Expected fingerprint from the first smoke run")
    args = parser.parse_args()
    require(bool(args.verify_game) == bool(args.fingerprint), "--verify-game and --fingerprint must be used together")
    token = os.getenv("TOI_AUTH_TOKEN", "")
    require(token, "TOI_AUTH_TOKEN must be configured for the authenticated smoke test")
    client = StackClient(args.base_url, token, args.origin or args.base_url.rstrip("/"))
    check_routes(client)
    state = (verify_persistence(client, args.verify_game, args.fingerprint) if args.verify_game
             else create_and_exercise(client))
    print(json.dumps({"game_id": state["game"]["id"], "fingerprint": fingerprint(state), "version": state["version"],
                      "checks": "persistence" if args.verify_game else "html,assets,health,auth,api,recommendations,websocket,substitution,undo"}))


if __name__ == "__main__":
    try:
        main()
    except (SmokeFailure, OSError, ValueError, WebSocketException) as error:
        message = str(error)
        for name in ("TOI_AUTH_TOKEN", "TOI_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"):
            value = os.getenv(name)
            if value:
                message = message.replace(value, "[redacted]")
        print(f"Container smoke failed: {message}", file=sys.stderr)
        sys.exit(1)
