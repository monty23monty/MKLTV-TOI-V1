"""Bounded duplex service transport over the existing authoritative Store.

The database journal is the outbox. Notifications only shorten polling latency;
no successful change depends on a socket, a callback or an in-memory queue.
"""

import asyncio
from collections import defaultdict, deque
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import time
from uuid import uuid4

from fastapi import Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .service_auth import (READ_PERMISSIONS, ServiceAuthenticationError, StreamConfig,
                           require_game, resolve_service, service_identities)
from .service_schemas import WireError, parse_message
from .store import StoreError, now_ms


PROTOCOL = "lightning.toi.stream.v2"
LOG = logging.getLogger(__name__)
MAX_SERVER_BYTES = 1_048_576
MAX_REPLAY_BYTES = 8_388_608
MAX_REPLAY_RECORDS = 1000


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _store_code(error):
    code = getattr(error, "code", None)
    if code:
        return code
    return {403: "FORBIDDEN", 404: "GAME_NOT_FOUND", 409: "VERSION_CONFLICT",
            413: "MESSAGE_TOO_LARGE", 422: "INVALID_COMMAND", 429: "RATE_LIMITED"}.get(error.status, "JOURNAL_UNAVAILABLE")


class RateBucket:
    def __init__(self, rate, burst):
        self.rate, self.burst, self.tokens = rate, burst, float(burst)
        self.updated = time.monotonic()

    def take(self):
        current = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (current - self.updated) * self.rate)
        self.updated = current
        if self.tokens < 1:
            return max(1, int((1 - self.tokens) / self.rate * 1000) + 1)
        self.tokens -= 1
        return 0


@dataclass
class GameFeed:
    game_id: str
    clients: set = field(default_factory=set)
    projection: dict | None = None
    sample_seq: int = 0
    sampled_monotonic: float = 0
    polled_monotonic: float = 0
    dirty: bool = True
    projection_dirty: bool = True


class Session:
    def __init__(self, service, socket, principal, authorization, game_id):
        self.service, self.socket, self.principal = service, socket, principal
        self.authorization, self.game_id = authorization, game_id
        self.connection_id = str(uuid4())
        self.seq = 0
        self.ready = False
        self.cursor = None
        self.tick_hz = 0
        self.last_tick = 0
        self.expires = min(now_ms() + 3_600_000, principal.expires_at_ms or (now_ms() + 3_600_000))
        self.lifetime_deadline = time.monotonic() + max(0, min(3600, (self.expires - now_ms()) / 1000))
        self.queue = deque()
        self.queue_bytes = 0
        self.bootstrap = deque()
        self.sample = None
        self.signal = asyncio.Event()
        self.finished = asyncio.Event()
        self.fatal = None
        self.command_task = None
        self.pending_ping = None
        self.last_ping = time.monotonic()
        self.read_bucket = RateBucket(60, 120)

    def reauthorize(self):
        principal = resolve_service(self.authorization)
        if principal.service_id != self.principal.service_id:
            raise ServiceAuthenticationError()
        require_game(principal, self.game_id)
        if now_ms() >= self.expires or time.monotonic() >= self.lifetime_deadline:
            raise ServiceAuthenticationError("Connection lifetime has expired; reconnect", expired=True)
        self.principal = principal
        return principal

    def _envelope(self, kind, data):
        return {"protocol": 2, "type": kind, "message_id": str(uuid4()), "game_id": self.game_id,
                "connection_id": self.connection_id, "server_epoch": self.service.server_epoch,
                "seq": self.seq + 1, "server_time_ms": now_ms(), "data": data}

    def push(self, kind, data):
        if self.finished.is_set() or self.fatal:
            return
        size = len(_json(self._envelope(kind, data)).encode())
        if size > MAX_SERVER_BYTES:
            self.fail("SERVER_OVERLOADED", "A recorded message exceeds the service wire limit", 1013)
        elif len(self.queue) >= 128 or self.queue_bytes + size > self.service.config.queue_bytes:
            self.fail("SERVER_OVERLOADED", "Consumer backlog exceeded its bounded queue; resume from your checkpoint", 1013)
        else:
            if kind in {"event.recorded", "state.snapshot"}:
                self.sample = None
            self.queue.append((kind, data, size))
            self.queue_bytes += size
            self.signal.set()

    def fail(self, code, detail, close=1008, bounds=None):
        if not self.fatal:
            bounds = bounds or {}
            self.fatal = ({"code": code, "detail": detail[:2000],
                           "oldest_cursor": bounds.get("oldest_cursor"),
                           "high_water_cursor": bounds.get("high_water_cursor")}, close)
            self.signal.set()

    def error(self, code, detail, request_id=None, *, retryable=False, resync=False, version=None, retry_after=None):
        self.push("command.error", {"request_id": request_id, "code": code, "detail": detail[:2000],
                                    "retryable": retryable, "resync_required": resync,
                                    "current_version": version, "retry_after_ms": retry_after})

    async def sender(self):
        try:
            while not self.finished.is_set():
                if self.fatal:
                    data, close = self.fatal
                    with suppress(Exception):
                        await self.send("session.error", data)
                        await asyncio.wait_for(self.socket.close(close), 5)
                    return
                if self.bootstrap:
                    kind, data = self.bootstrap.popleft()
                elif self.queue:
                    kind, data, size = self.queue.popleft()
                    self.queue_bytes -= size
                elif self.sample is not None:
                    kind, data = "live.tick", self.sample
                    self.sample = None
                else:
                    self.signal.clear()
                    await self.signal.wait()
                    continue
                await self.send(kind, data)
                if kind == "ping":
                    self.last_ping = time.monotonic()
                    if self.pending_ping and self.pending_ping[0] == data:
                        self.pending_ping = (data, self.last_ping)
                if kind == "stream.ready":
                    self.ready = True
                if kind == "replay.required":
                    await asyncio.wait_for(self.socket.close(1013), 5)
                    return
        except (WebSocketDisconnect, RuntimeError, OSError, TimeoutError):
            with suppress(Exception):
                await asyncio.wait_for(self.socket.close(1013), 1)
        finally:
            self.finished.set()

    async def send(self, kind, data):
        encoded = _json(self._envelope(kind, data))
        if len(encoded.encode()) > MAX_SERVER_BYTES:
            raise RuntimeError("Server wire limit exceeded")
        await asyncio.wait_for(self.socket.send_text(encoded), 5)
        self.seq += 1

    async def receive_message(self):
        received = await self.socket.receive()
        if received["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(received.get("code", 1000))
        if self.read_bucket.take():
            self.fail("SERVER_OVERLOADED", "Inbound message rate exceeded", 1013)
            raise WireError("Inbound message rate exceeded")
        return parse_message(received.get("text"), self.game_id)

    async def receiver(self):
        while not self.finished.is_set():
            try:
                message = await self.receive_message()
            except WireError as error:
                if self.fatal:
                    return
                if error.code == "MESSAGE_TOO_LARGE":
                    self.fail("INVALID_MESSAGE", str(error), 1009)
                    return
                self.error(error.code, str(error))
                continue
            kind, data = message["type"], message["data"]
            if kind == "session.open":
                self.fail("SESSION_ALREADY_OPEN", "A connection can open only one session")
                return
            if kind == "pong":
                if self.pending_ping and data == self.pending_ping[0]:
                    self.pending_ping = None
                continue
            if kind == "ping":
                self.push("pong", data)
                continue
            if not self.ready:
                self.error("SESSION_NOT_READY", "Wait for stream.ready before requesting or changing state",
                           data.get("request_id"))
                continue
            if kind == "state.request":
                try:
                    self.reauthorize()
                    await self.service.request_snapshot(self)
                except (ServiceAuthenticationError, PermissionError, ValueError) as error:
                    self.auth_failure(error)
                    return
                except StoreError as error:
                    session_code = _store_code(error)
                    self.fail(session_code, str(error), 1013 if error.status == 503 else 1008,
                              bounds=getattr(error, "details", {}))
                    return
                continue
            if self.command_task and not self.command_task.done():
                self.error("TOO_MANY_IN_FLIGHT", "Resolve the outstanding mutation before submitting another",
                           data["request_id"], retryable=True, retry_after=100)
                continue
            self.command_task = asyncio.create_task(self.service.mutate(self, kind, data))

    def auth_failure(self, error):
        if isinstance(error, ServiceAuthenticationError):
            code = "AUTHENTICATION_EXPIRED" if error.expired else "AUTHENTICATION_FAILED"
        else:
            code = "AUTHORIZATION_FAILED"
        self.fail(code, "Service authorization is no longer valid; reconnect after correcting credentials")

    async def heartbeat(self):
        while not self.finished.is_set():
            await asyncio.sleep(0.5)
            try:
                self.reauthorize()
            except (PermissionError, ValueError) as error:
                self.auth_failure(error)
                return
            current = time.monotonic()
            if self.pending_ping and self.pending_ping[1] is not None and current - self.pending_ping[1] >= 5:
                self.fail("HEARTBEAT_TIMEOUT", "A matching application pong was not received within five seconds")
                return
            if self.pending_ping is None and current - self.last_ping >= 15:
                ping = {"nonce": str(uuid4()), "client_sent_at_ms": now_ms()}
                # Start the pong deadline when the dedicated sender emits the
                # ping, rather than while it is waiting behind journal records.
                self.pending_ping = (ping, None)
                self.push("ping", ping)


class ServiceStream:
    def __init__(self, app, store, coordinator, origins):
        self.app, self.store, self.coordinator, self.origins = app, store, coordinator, origins
        self.config = StreamConfig()
        self.enabled = self.config.enabled
        self.server_epoch = str(uuid4())
        self.feeds = {}
        self.connections = set()
        self.by_service = defaultdict(int)
        self.buckets = {}
        self.wake = asyncio.Event()
        self.task = None
        self.last_poll_at_ms = None
        self.last_discovery = 0
        self.last_error_at_ms = None
        self._install_routes()

    def clock_sources(self, game_id):
        if not self.enabled:
            return []
        result = {(principal.service_id, source) for _, principal in service_identities()
                  if principal.allows_game(game_id) and "clock:write" in principal.permissions
                  and READ_PERMISSIONS <= principal.permissions
                  and (principal.expires_at_ms is None or principal.expires_at_ms > now_ms())
                  for source in principal.clock_sources}
        return [{"service_id": service_id, "source": source} for service_id, source in sorted(result)]

    def readiness(self):
        return {"enabled": self.enabled, "configured": bool(service_identities()),
                "dispatcher_running": self.task is not None and not self.task.done(),
                "last_poll_at_ms": self.last_poll_at_ms, "last_error_at_ms": self.last_error_at_ms,
                "active_games": len(self.feeds)}

    def notify(self, game_id):
        feed = self.feeds.get(game_id)
        if feed:
            feed.dirty = True
            feed.projection_dirty = True
        self.wake.set()

    async def start(self):
        if self.enabled and self.task is None:
            await self.discover()
            self.task = asyncio.create_task(self.dispatcher(), name="toi-service-journal-dispatcher")

    async def stop(self):
        for session in tuple(self.connections):
            session.fail("SERVER_OVERLOADED", "Backend is stopping; reconnect and resume your checkpoint", 1013)
        if self.connections:
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*(session.finished.wait() for session in tuple(self.connections))), 5)
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def discover(self):
        games = await run_in_threadpool(self.store.list_games)
        for game in games:
            game_id = game["id"]
            if game_id not in self.feeds:
                async with self.coordinator.lock(game_id):
                    await run_in_threadpool(self.store.activate_stream, game_id)
                    self.feeds[game_id] = GameFeed(game_id)
        self.last_discovery = time.monotonic()

    async def ensure_feed(self, game_id):
        if game_id not in self.feeds:
            # Callers already hold the coordinator lock for this game.
            await run_in_threadpool(self.store.activate_stream, game_id)
            self.feeds[game_id] = GameFeed(game_id)
        return self.feeds[game_id]

    async def sample(self, feed, *, force=False):
        current = time.monotonic()
        frequency = max([self.config.tick_hz, *(session.tick_hz for session in feed.clients)])
        if force or feed.projection is None or current - feed.sampled_monotonic >= 1 / frequency:
            projection = await run_in_threadpool(self.store.live, feed.game_id)
            feed.sample_seq += 1
            projection["state"]["sample_seq"] = feed.sample_seq
            feed.projection = projection
            feed.sampled_monotonic = current
            feed.projection_dirty = False
        return deepcopy(feed.projection)

    async def dispatcher(self):
        while True:
            try:
                if time.monotonic() - self.last_discovery >= 5:
                    await self.discover()
                for feed in tuple(self.feeds.values()):
                    await self.poll_game(feed)
                self.last_poll_at_ms = now_ms()
                self.last_error_at_ms = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error_at_ms = now_ms()
                # Do not log exception messages: drivers can include credentials.
                LOG.warning("Service journal dispatcher dependency failure; retrying")
            self.wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 0.05)

    async def poll_game(self, feed):
        current = time.monotonic()
        sample_due = feed.clients and any(session.ready and session.tick_hz and
                     current - session.last_tick >= 1 / session.tick_hz for session in feed.clients)
        if not feed.dirty and not sample_due and current - feed.polled_monotonic < 0.25:
            return
        async with self.coordinator.lock(feed.game_id):
            snapshot, changed = await run_in_threadpool(self.store.expire_clock, feed.game_id)
            if changed:
                await self.coordinator.changed(feed.game_id, snapshot=snapshot, kind="clock.updated")
                feed.dirty = True
                feed.projection_dirty = True
            clients = [client for client in tuple(feed.clients) if client.cursor is not None and not client.fatal]
            # Each subscriber keeps its own durable cursor. Queries are grouped by
            # cursor so identical fanout does not rebuild journal records per socket.
            groups = defaultdict(list)
            for client in clients:
                groups[client.cursor].append(client)
            for cursor, subscribers in groups.items():
                try:
                    page = await run_in_threadpool(self.store.journal_page, feed.game_id,
                                                   after=cursor, limit=1000)
                except StoreError as error:
                    for client in subscribers:
                        client.fail(_store_code(error), str(error), bounds=getattr(error, "details", {}))
                    continue
                for client in subscribers:
                    for record in page["records"]:
                        client.push("event.recorded", {"replay": False, "record": record})
                    client.cursor = page["next_cursor"]
                if page["has_more"]:
                    # A running consumer behind a bounded batch must recover via
                    # durable replay rather than receive a misleading newer tick.
                    for client in subscribers:
                        client.fail("SERVER_OVERLOADED", "Recorded backlog exceeds one bounded dispatch batch", 1013)
            if sample_due or feed.dirty:
                projection = await self.sample(feed, force=feed.projection_dirty)
                state = projection["state"]
                for client in clients:
                    if (client.ready and not client.fatal and client.tick_hz and
                            current - client.last_tick >= 1 / client.tick_hz):
                        # Emit only after this subscriber has queued all records at
                        # the sample's baseline. A queued tick remains replaceable.
                        if client.cursor == projection["baseline_cursor"]:
                            client.sample = self.tick(state)
                            client.last_tick = current
                            client.signal.set()
            feed.dirty = False
            feed.polled_monotonic = current

    @staticmethod
    def tick(state):
        keys = ("version", "sampled_at_ms", "sample_seq", "clock", "clock_authority", "on_ice", "warnings", "strengths")
        tick = {key: state[key] for key in keys}
        timing = ("total_toi_ms", "current_shift_ms", "completed_shifts", "average_shift_ms", "longest_shift_ms", "toi_by_period")
        tick["toi"] = [{"player_id": player["id"], **{key: player[key] for key in timing}}
                       for player in state["players"]]
        return tick

    async def request_snapshot(self, session):
        async with self.coordinator.lock(session.game_id):
            # Queue intervening recorded changes before the requested new baseline.
            page = await run_in_threadpool(self.store.journal_page, session.game_id, after=session.cursor, limit=1000)
            for record in page["records"]:
                session.push("event.recorded", {"replay": False, "record": record})
            session.cursor = page["next_cursor"]
            if page["has_more"]:
                session.fail("SERVER_OVERLOADED", "Catch up from your durable checkpoint before requesting state", 1013)
                return
            feed = await self.ensure_feed(session.game_id)
            projection = await self.sample(feed, force=feed.projection_dirty)
            session.push("state.snapshot", self.snapshot_data(projection, "requested"))

    @staticmethod
    def snapshot_data(projection, reason):
        return {"reason": reason, "baseline_cursor": projection["baseline_cursor"],
                "version": projection["state"]["version"], "state": projection["state"]}

    def welcome(self, session, data, bounds):
        return {"client_id": data["client_id"], "permissions": sorted(session.principal.permissions),
                **bounds, "tick_hz": data["tick_hz"], "connection_expires_at_ms": session.expires,
                "auth_expires_at_ms": session.principal.expires_at_ms,
                "heartbeat_interval_ms": 15000, "pong_timeout_ms": 5000,
                "max_command_bytes": 65536, "max_server_message_bytes": MAX_SERVER_BYTES,
                "max_in_flight_commands": 1, "queue_max_messages": 128,
                "queue_max_bytes": self.config.queue_bytes, "send_timeout_ms": 5000,
                "journal_retention_days": self.config.retention_days,
                "rate_limits": {"commands_per_second": 10, "commands_burst": 20,
                                "clock_observations_per_second": 2, "clock_observations_burst": 5}}

    async def bootstrap(self, session, data):
        async with self.coordinator.lock(session.game_id):
            session.reauthorize()
            feed = await self.ensure_feed(session.game_id)
            projection = await self.sample(feed, force=feed.projection_dirty)
            bounds = await run_in_threadpool(self.store.journal_bounds, session.game_id)
            high_water = bounds["high_water_cursor"]
            after = data.get("after_cursor") if data["journal_mode"] == "resume" else bounds["oldest_cursor"]
            records = []
            if data["journal_mode"] != "now":
                page = await run_in_threadpool(self.store.journal_page, session.game_id,
                                               after=after, through=high_water, limit=1000)
                records = page["records"]
                size = sum(len(_json(record).encode()) for record in records)
                if page["has_more"] or len(records) > MAX_REPLAY_RECORDS or size > MAX_REPLAY_BYTES:
                    session.bootstrap.extend([
                        ("session.welcome", self.welcome(session, data, bounds)),
                        ("replay.required", {"after_cursor": after, "through_cursor": high_water,
                                             "max_records": MAX_REPLAY_RECORDS, "max_bytes": MAX_REPLAY_BYTES})])
                    session.signal.set()
                    return
            session.tick_hz = data["tick_hz"]
            session.cursor = high_water
            session.bootstrap.append(("session.welcome", self.welcome(session, data, bounds)))
            session.bootstrap.extend(("event.recorded", {"replay": True, "record": record}) for record in records)
            session.bootstrap.append(("state.snapshot", self.snapshot_data(projection, "initial")))
            session.bootstrap.append(("stream.ready", {"through_cursor": high_water,
                                                      "version": projection["state"]["version"]}))
            feed.clients.add(session)
            session.signal.set()

    async def mutate(self, session, kind, data):
        request_id = data["request_id"]
        try:
            principal = session.reauthorize()
            permission = "clock:write" if kind == "clock.observe" else {
                "substitute": "players:write", "move": "players:write", "clock": "clock:control",
                "undo": "history:write", "correct": "history:write", "setup": "setup:write",
                "clock.authority.set": "clock:authority"}[data["command"]["type"]]
            require_game(principal, session.game_id, frozenset({permission}))
            if kind == "clock.observe" and data["source"] not in principal.clock_sources:
                raise PermissionError("Service is not authorized for this clock source")
            if kind == "command.submit" and data["command"]["type"] == "clock.authority.set":
                owner = data["command"]["payload"]
                if owner["mode"] == "external" and {"service_id": owner["service_id"], "source": owner["source"]} not in self.clock_sources(session.game_id):
                    raise PermissionError("Clock owner must name an available configured service and source")
            buckets = self.buckets.setdefault((principal.service_id, session.game_id),
                                              (RateBucket(10, 20), RateBucket(2, 5)))
            retry = buckets[0].take()
            if kind == "clock.observe":
                retry = max(retry, buckets[1].take())
            if retry:
                session.error("RATE_LIMITED", "Service/game mutation rate exceeded", request_id,
                              retryable=True, retry_after=retry)
                return
            async with self.coordinator.lock(session.game_id):
                # Revalidate after queue admission as credentials can expire while waiting.
                principal = session.reauthorize()
                require_game(principal, session.game_id, frozenset({permission}))
                # A client disconnect must not cancel a transaction already executing.
                result = await run_in_threadpool(self.store.apply_service, session.game_id, data, principal)
                ack, changed = result
                if changed:
                    await self.coordinator.changed(session.game_id, request_id=request_id)
                    self.notify(session.game_id)
                session.push("command.ack", ack)
        except ServiceAuthenticationError as error:
            session.auth_failure(error)
        except PermissionError:
            session.error("FORBIDDEN", "Service is not authorized for this operation", request_id)
        except StoreError as error:
            code = _store_code(error)
            if code == "SERVER_OVERLOADED":
                session.error("RATE_LIMITED", "The game admission queue is full; retry after a short delay", request_id,
                              retryable=True, retry_after=100)
                return
            version = None
            with suppress(Exception):
                version = (await run_in_threadpool(self.store.get, session.game_id))["version"]
            session.error(code, str(error), request_id,
                          retryable=code == "RATE_LIMITED", resync=code in {"VERSION_CONFLICT", "AUTHORITY_CONFLICT"},
                          version=version)
        except Exception:
            # A driver/commit or post-commit notification failure is not proof of
            # rollback. The durable identical request, never a new ID, resolves it.
            session.error("OUTCOME_UNKNOWN", "Outcome could not be confirmed; reconnect and retry the identical request",
                          request_id, retryable=True)

    def _http_principal(self, authorization, game_id, permissions):
        if not self.enabled:
            raise HTTPException(404, "Service API is disabled")
        try:
            principal = resolve_service(authorization)
            require_game(principal, game_id, permissions)
            return principal
        except ServiceAuthenticationError as error:
            raise HTTPException(401, str(error), headers={"WWW-Authenticate": "Bearer"}) from error
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(503, "Service authentication is unavailable") from error

    async def _http_live(self, game_id):
        async with self.coordinator.lock(game_id):
            feed = await self.ensure_feed(game_id)
            projection = await self.sample(feed, force=feed.projection_dirty)
        return projection

    @staticmethod
    def _http_error(error):
        code = _store_code(error)
        status = error.status
        if code.startswith("CURSOR_"):
            status = 409
        return JSONResponse(status_code=status, content={"detail": str(error), "code": code,
                                                         **(getattr(error, "details", None) or {})})

    def _install_routes(self):
        @self.app.get("/api/v2/games/{game_id}/live", tags=["Service v2"])
        async def live(game_id: str, authorization: str | None = Header(default=None)):
            self._http_principal(authorization, game_id, frozenset({"stream:read"}))
            try:
                return await self._http_live(game_id)
            except StoreError as error:
                return self._http_error(error)

        @self.app.get("/api/v2/games/{game_id}/players/{player_id}/toi", tags=["Service v2"])
        async def player_toi(game_id: str, player_id: str, authorization: str | None = Header(default=None)):
            self._http_principal(authorization, game_id, frozenset({"stream:read"}))
            try:
                projection = await self._http_live(game_id)
                player = next((player for player in projection["state"]["players"] if player["id"] == player_id), None)
                if player is None:
                    raise HTTPException(404, "Player not found")
                return {"game_id": game_id, "version": projection["state"]["version"],
                        "baseline_cursor": projection["baseline_cursor"], "sampled_at_ms": projection["sampled_at_ms"],
                        "player": player}
            except StoreError as error:
                return self._http_error(error)

        @self.app.get("/api/v2/games/{game_id}/on-ice", tags=["Service v2"])
        async def on_ice(game_id: str, authorization: str | None = Header(default=None)):
            self._http_principal(authorization, game_id, frozenset({"stream:read"}))
            try:
                projection = await self._http_live(game_id)
                state = projection["state"]
                players = {player["id"]: player for player in state["players"]}
                return {"game_id": game_id, "version": state["version"], "baseline_cursor": projection["baseline_cursor"],
                        "sampled_at_ms": projection["sampled_at_ms"],
                        **{side: [players[player_id] for player_id in state["on_ice"][side]] for side in ("home", "away")}}
            except StoreError as error:
                return self._http_error(error)

        @self.app.get("/api/v2/games/{game_id}/journal", tags=["Service v2"])
        async def journal(game_id: str, after: str | None = Query(default=None, max_length=512),
                          through: str | None = Query(default=None, max_length=512),
                          limit: int = Query(default=100, ge=1, le=1000),
                          authorization: str | None = Header(default=None)):
            self._http_principal(authorization, game_id, frozenset({"journal:read"}))
            try:
                async with self.coordinator.lock(game_id):
                    await self.ensure_feed(game_id)
                    result = await run_in_threadpool(self.store.journal_page, game_id, after=after, through=through, limit=limit)
                if len(_json(result).encode()) > MAX_REPLAY_BYTES:
                    raise StoreError(413, "Journal page exceeds eight MiB")
                return result
            except StoreError as error:
                return self._http_error(error)

        @self.app.websocket("/api/v2/games/{game_id}/stream")
        async def stream(socket: WebSocket, game_id: str):
            await self.connect(socket, game_id)

    async def deny(self, socket, status, detail):
        # Native clients get meaningful HTTP denials on ASGI implementations that
        # support the extension; the portable fallback rejects before acceptance.
        if "websocket.http.response" in socket.scope.get("extensions", {}):
            await socket.send_denial_response(JSONResponse({"detail": detail}, status_code=status))
        else:
            await socket.close(1008)

    async def connect(self, socket, game_id):
        if not self.enabled:
            await self.deny(socket, 404, "Service API is disabled")
            return
        authorization = socket.headers.get("authorization")
        try:
            principal = resolve_service(authorization)
            require_game(principal, game_id, READ_PERMISSIONS)
        except ServiceAuthenticationError:
            await self.deny(socket, 401, "A valid service bearer token is required")
            return
        except PermissionError:
            await self.deny(socket, 403, "Service is not authorized for this game")
            return
        except ValueError:
            await self.deny(socket, 503, "Service authentication is unavailable")
            return
        origin = socket.headers.get("origin")
        if origin is not None and origin not in self.origins:
            await self.deny(socket, 403, "Origin is not allowed")
            return
        if PROTOCOL not in socket.scope.get("subprotocols", []):
            await self.deny(socket, 400, "The lightning.toi.stream.v2 subprotocol is required")
            return
        if len(self.connections) >= self.config.max_connections or self.by_service[principal.service_id] >= 4:
            await self.deny(socket, 503, "Service connection limit reached")
            return
        session = Session(self, socket, principal, authorization, game_id)
        self.connections.add(session)
        self.by_service[principal.service_id] += 1
        await socket.accept(subprotocol=PROTOCOL)
        sender = asyncio.create_task(session.sender())
        receiver = heartbeat = None
        try:
            try:
                message = await asyncio.wait_for(session.receive_message(), 5)
                if message["type"] != "session.open":
                    session.fail("SESSION_NOT_OPEN", "The first message must be session.open")
                else:
                    await self.bootstrap(session, message["data"])
            except WireError as error:
                session.fail(error.code if error.code != "MESSAGE_TOO_LARGE" else "INVALID_MESSAGE", str(error),
                             1009 if error.code == "MESSAGE_TOO_LARGE" else 1008)
            except TimeoutError:
                session.fail("SESSION_NOT_OPEN", "session.open was not received within five seconds")
            except (ServiceAuthenticationError, PermissionError, ValueError) as error:
                session.auth_failure(error)
            except StoreError as error:
                code = _store_code(error)
                if code == "GAME_NOT_FOUND":
                    code = "AUTHORIZATION_FAILED"
                session.fail(code, str(error), 1013 if error.status == 503 else 1008,
                             bounds=getattr(error, "details", {}))
            if not session.fatal:
                receiver = asyncio.create_task(session.receiver())
                heartbeat = asyncio.create_task(session.heartbeat())
                done, _ = await asyncio.wait([sender, receiver, heartbeat], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task is not sender:
                        with suppress(WebSocketDisconnect, RuntimeError):
                            task.result()
                if session.fatal and not sender.done():
                    await sender
            else:
                await sender
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            session.finished.set()
            session.signal.set()
            # Keep an in-progress transaction alive until its receipt is resolved.
            if session.command_task:
                with suppress(Exception):
                    await asyncio.shield(session.command_task)
            for task in (sender, receiver, heartbeat):
                if task and not task.done():
                    task.cancel()
            for task in (sender, receiver, heartbeat):
                if task:
                    with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                        await task
            self.connections.discard(session)
            self.by_service[principal.service_id] -= 1
            if not self.by_service[principal.service_id]:
                del self.by_service[principal.service_id]
            feed = self.feeds.get(game_id)
            if feed:
                feed.clients.discard(session)


def install_service_api(app, store, coordinator, origins):
    return ServiceStream(app, store, coordinator, origins)
