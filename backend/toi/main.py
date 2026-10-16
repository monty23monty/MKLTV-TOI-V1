"""Versioned HTTP/WebSocket integration surface for the standalone operator tool."""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .adapters import Principal, resolve_principal
from .database import database_url
from .coordinator import MutationCoordinator
from .import_service import ImportService, game_id_for, preview_response, require_supervisor
from .integrations.hokejovy_zapis import HokejovyZapisProvider, ProviderError
from .recommendations import recommend
from .schemas import Action, ExternalClock, ImportGame, NewGame, RefreshRoster, StateSnapshot
from .store import Store, StoreError, now_ms, public_snapshot
from .service_stream import install_service_api
from .version import APP_VERSION, build_metadata


class ClockAuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=100)
    expected_version: int = Field(ge=0)
    mode: Literal["manual", "external"]
    source: str | None = Field(default=None, min_length=1, max_length=100)
    service_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def ownership(self):
        if self.mode == "external" and (not self.source or not self.service_id):
            raise ValueError("External ownership requires a configured source and service")
        if self.mode == "manual" and (self.source is not None or self.service_id is not None):
            raise ValueError("Manual ownership must not name an external source or service")
        return self


class LiveHub:
    """Match-scoped ordered delivery; a slow subscriber receives a fresh resync."""

    def __init__(self):
        self.clients: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, game_id: str):
        queue = asyncio.Queue(maxsize=32)
        self.clients[game_id].add(queue)
        return queue

    def unsubscribe(self, game_id: str, queue):
        self.clients[game_id].discard(queue)
        if not self.clients[game_id]:
            self.clients.pop(game_id, None)

    def publish(self, game_id: str, event: dict):
        for queue in self.clients.get(game_id, ()):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({**event, "type": "state.resync", "reason": "slow_subscriber"})
            else:
                queue.put_nowait(event)


EVENT_NAMES = {
    "substitute": "substitution.updated",
    "move": "player.location.updated",
    "clock": "clock.updated",
    "external_clock": "clock.updated",
    "undo": "correction.updated",
    "correct": "correction.updated",
    "setup": "state.resync",
}


def authenticate(authorization: str | None = Header(default=None)) -> Principal:
    try:
        return resolve_principal(authorization)
    except PermissionError as error:
        raise HTTPException(401, str(error), headers={"WWW-Authenticate": "Bearer"}) from error
    except ValueError as error:
        raise HTTPException(503, "Authentication configuration is invalid") from error


def create_app(url: str | None = None) -> FastAPI:
    store = Store(url or database_url())
    hub = LiveHub()
    coordinator = MutationCoordinator(store, hub)

    @asynccontextmanager
    async def lifespan(_app):
        store.migrate()
        store.create("demo")
        await app.state.service_stream.start()
        try:
            yield
        finally:
            await app.state.service_stream.stop()
            store.engine.dispose()

    app = FastAPI(title="Lightning TV • Live TOI", version=APP_VERSION, lifespan=lifespan,
                  description="Authoritative hockey player locations, game-time shifts and adaptive substitutions. See API.md for ordered WebSocket envelopes and integration semantics.")
    app.state.store = store
    app.state.hub = hub
    app.state.coordinator = coordinator
    app.state.hokejovy_provider = HokejovyZapisProvider()
    imports = ImportService(store)
    origins = [s.strip() for s in os.getenv("TOI_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080").split(",") if s.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    app.state.service_stream = install_service_api(app, store, coordinator, origins)
    coordinator.notify = app.state.service_stream.notify

    @app.exception_handler(StoreError)
    async def store_error(_request, error: StoreError):
        return JSONResponse(status_code=error.status, content={"detail": str(error), "resync_required": error.status == 409})

    @app.exception_handler(ProviderError)
    async def provider_error(_request, error: ProviderError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.as_dict()})

    @app.get("/health")
    def health():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "service": "lightning-toi", "api_version": "v1",
                "api_versions": ["v1", "v2"] if app.state.service_stream.enabled else ["v1"],
                "stream_enabled": app.state.service_stream.enabled, **build_metadata()}

    @app.get("/ready", tags=["Operations"])
    def ready():
        with store.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok",
                "stream_enabled": app.state.service_stream.enabled,
                "services": app.state.service_stream.readiness(), **build_metadata()}

    @app.get("/api/v1/games", tags=["Games"])
    def games(_principal: Principal = Depends(authenticate)):
        return store.list_games()

    @app.post("/api/v1/games", response_model=StateSnapshot, status_code=201, tags=["Games"])
    async def new_game(details: NewGame, principal: Principal = Depends(authenticate)):
        if principal.team_ids is not None or not principal.can_control_clock:
            raise HTTPException(403, "Creating a game requires a match supervisor")
        game_id = str(uuid4())
        async with coordinator.lock(game_id):
            return await run_in_threadpool(store.create, game_id, actor=principal.actor, **details.model_dump())

    @app.get("/api/v1/integrations/hokejovy-zapis/games/{external_game_id}/preview", tags=["Hokejový Zápis"])
    def source_preview(external_game_id: str, force: bool = False, principal: Principal = Depends(authenticate)):
        require_supervisor(principal)
        game_id_for(external_game_id)
        return preview_response(app.state.hokejovy_provider.preview(external_game_id, force=force))

    @app.post("/api/v1/games/import", response_model=StateSnapshot, tags=["Hokejový Zápis"])
    async def import_game(body: ImportGame, principal: Principal = Depends(authenticate)):
        require_supervisor(principal)
        game_id = game_id_for(body.external_game_id)
        try:
            existing = store.get(game_id)
        except StoreError as error:
            if error.status != 404:
                raise
        else:
            return public_snapshot(existing)
        dto = await run_in_threadpool(app.state.hokejovy_provider.preview, body.external_game_id)
        async with coordinator.lock(game_id):
            result = await run_in_threadpool(imports.import_game, dto, body.model_dump(exclude_none=True), principal)
            await coordinator.changed(game_id, snapshot=result)
        return result

    @app.get("/api/v1/games/{game_id}/lines", tags=["Games"])
    def lines(game_id: str, _principal: Principal = Depends(authenticate)):
        return store.get(game_id)["lines"]

    @app.post("/api/v1/games/{game_id}/roster/refresh", tags=["Hokejový Zápis"])
    async def refresh_source(game_id: str, body: RefreshRoster, principal: Principal = Depends(authenticate)):
        require_supervisor(principal)
        refresh_body = body.model_dump(exclude_none=True)
        if body.confirm:
            previous_receipt = imports.receipt(game_id, refresh_body, principal)
            if previous_receipt is not None:
                return previous_receipt
        state = store.get(game_id)
        integration = state.get("integration")
        if not integration or integration.get("provider") != "hokejovy-zapis":
            raise StoreError(422, "This match has no Hokejový Zápis source")
        dto = await run_in_threadpool(app.state.hokejovy_provider.preview, integration["external_game_id"], force=not body.confirm)
        async with coordinator.lock(game_id):
            result = await run_in_threadpool(imports.refresh, game_id, dto, refresh_body, principal)
            if body.confirm:
                await coordinator.changed(game_id, snapshot=result)
        return result

    @app.get("/api/v1/games/{game_id}/state", response_model=StateSnapshot, tags=["Live state"])
    def state(game_id: str, _principal: Principal = Depends(authenticate)):
        return public_snapshot(store.get(game_id))

    @app.get("/api/v1/games/{game_id}/teams", tags=["Games"])
    def teams(game_id: str, _principal: Principal = Depends(authenticate)):
        return store.get(game_id)["teams"]

    @app.get("/api/v1/games/{game_id}/roster", tags=["Games"])
    def roster(game_id: str, _principal: Principal = Depends(authenticate)):
        return store.get(game_id)["players"]

    @app.get("/api/v1/games/{game_id}/locations", tags=["Live state"])
    def locations(game_id: str, _principal: Principal = Depends(authenticate)):
        snapshot = public_snapshot(store.get(game_id))
        return {"version": snapshot["version"], "players": [{"player_id": p["id"], "team_id": p["team_id"], "location": p["location"], "eligible": p["eligible"]} for p in snapshot["players"]]}

    @app.get("/api/v1/games/{game_id}/statistics", tags=["Statistics"])
    def statistics(game_id: str, _principal: Principal = Depends(authenticate)):
        snapshot = public_snapshot(store.get(game_id))
        return {"game_id": game_id, "version": snapshot["version"], "clock": snapshot["clock"], "players": sorted(snapshot["players"], key=lambda p: (-p["total_toi_ms"], p["id"])), "strength_classification": "unavailable"}

    @app.get("/api/v1/games/{game_id}/shifts", tags=["Statistics"])
    def shifts(game_id: str, player_id: str | None = None, _principal: Principal = Depends(authenticate)):
        rows = public_snapshot(store.get(game_id))["shifts"]
        return [s for s in rows if player_id is None or s["player_id"] == player_id]

    @app.get("/api/v1/games/{game_id}/events", tags=["History"])
    def events(game_id: str, _principal: Principal = Depends(authenticate)):
        return public_snapshot(store.get(game_id))["events"]

    @app.get("/api/v1/games/{game_id}/audit", tags=["History"])
    def audit(game_id: str, _principal: Principal = Depends(authenticate)):
        return store.audit(game_id)

    @app.get("/api/v1/games/{game_id}/clock", tags=["Clock"])
    def clock(game_id: str, _principal: Principal = Depends(authenticate)):
        return public_snapshot(store.get(game_id))["clock"]

    @app.get("/api/v1/games/{game_id}/suggestions", tags=["Suggestions"])
    def suggestions(game_id: str, team_id: str, outgoing_ids: str = "", _principal: Principal = Depends(authenticate)):
        current = public_snapshot(store.get(game_id))
        if not any(t["id"] == team_id for t in current["teams"]):
            raise HTTPException(404, "Team not found in this game")
        outgoing = [s for s in outgoing_ids.split(",") if s]
        if len(set(outgoing)) != len(outgoing) or any(not any(p["id"] == pid and p["team_id"] == team_id and p["location"] == "ON_ICE" and p["eligible"] for p in current["players"]) for pid in outgoing):
            raise HTTPException(422, "Outgoing players must be distinct eligible on-ice teammates")
        return recommend(current, team_id, outgoing)

    @app.post("/api/v1/games/{game_id}/actions", response_model=StateSnapshot, tags=["Commands"])
    async def actions(game_id: str, action: Action, principal: Principal = Depends(authenticate)):
        command_body = action.model_dump(exclude_none=True)
        async with coordinator.lock(game_id):
            result, changed = await run_in_threadpool(store.apply, game_id, command_body, principal)
            if changed:
                kind = EVENT_NAMES[action.type]
                if action.type == "substitute" and len(action.payload.outgoing_ids) > 1:
                    kind = "substitution.group.updated"
                if action.type == "move" and (action.payload.to == "PENALTY_BOX" or any(e.get("type") == "move" and e.get("payload", {}).get("player_id") == action.payload.player_id and e.get("payload", {}).get("from") == "PENALTY_BOX" for e in result["events"][-1:])):
                    kind = "penalty.location.updated"
                await coordinator.changed(game_id, snapshot=result, kind=kind, request_id=action.request_id)
        return result

    @app.post("/api/v1/games/{game_id}/clock/observations", response_model=StateSnapshot, tags=["Clock"])
    async def clock_observations(game_id: str, observation: ExternalClock, principal: Principal = Depends(authenticate)):
        payload = observation.model_dump()
        action = {"request_id": payload.pop("request_id"), "expected_version": payload.pop("expected_version"), "type": "external_clock", "payload": payload}
        async with coordinator.lock(game_id):
            result, changed = await run_in_threadpool(store.apply, game_id, action, principal)
            if changed:
                await coordinator.changed(game_id, snapshot=result, kind="clock.updated", request_id=action["request_id"])
        return result

    @app.get("/api/v1/games/{game_id}/clock/authority", tags=["Clock"])
    async def clock_authority(game_id: str, principal: Principal = Depends(authenticate)):
        require_supervisor(principal)
        if not app.state.service_stream.enabled:
            raise HTTPException(404, "Service streaming is disabled")
        async with coordinator.lock(game_id):
            result = await run_in_threadpool(store.live, game_id)
        return {"version": result["state"]["version"],
                "authority": result["state"]["clock_authority"],
                "sources": app.state.service_stream.clock_sources(game_id)}

    @app.post("/api/v1/games/{game_id}/clock/authority", response_model=StateSnapshot, tags=["Clock"])
    async def set_clock_authority(game_id: str, body: ClockAuthorityRequest,
                                  principal: Principal = Depends(authenticate)):
        require_supervisor(principal)
        if not app.state.service_stream.enabled:
            raise HTTPException(404, "Service streaming is disabled")
        if body.mode == "external" and {"source": body.source, "service_id": body.service_id} not in app.state.service_stream.clock_sources(game_id):
            raise HTTPException(422, "Select a configured clock service and source for this match")
        service_principal = SimpleNamespace(actor=principal.actor, service_id="operator",
                                            permissions={"clock:authority"}, team_ids=None,
                                            can_control_clock=True, clock_sources=[])
        payload = body.model_dump(exclude={"request_id", "expected_version"})
        data = {"request_id": body.request_id, "expected_version": body.expected_version,
                "command": {"type": "clock.authority.set", "payload": payload}}
        async with coordinator.lock(game_id):
            _ack, changed = await run_in_threadpool(store.apply_service, game_id, data, service_principal)
            result = await run_in_threadpool(lambda: public_snapshot(store.get(game_id)))
            if changed:
                await coordinator.changed(game_id, snapshot=result, kind="clock.updated", request_id=body.request_id)
        return result

    @app.websocket("/api/v1/games/{game_id}/ws")
    async def websocket(websocket: WebSocket, game_id: str, token: str | None = Query(default=None)):
        origin = websocket.headers.get("origin")
        # Same-origin reverse-proxy use plus explicitly configured operator origins.
        host_origin = f"{'https' if websocket.url.scheme == 'wss' else 'http'}://{websocket.headers.get('host')}"
        if origin and origin not in origins and origin != host_origin:
            await websocket.close(code=1008, reason="Origin not allowed")
            return
        try:
            resolve_principal(websocket.headers.get("authorization") or (f"Bearer {token}" if token else None))
            await run_in_threadpool(store.get, game_id)
        except (PermissionError, ValueError, StoreError):
            await websocket.close(code=1008, reason="Authentication failed or game unavailable")
            return
        try:
            async with coordinator.lock(game_id):
                queue = hub.subscribe(game_id)
                try:
                    initial = await run_in_threadpool(lambda: public_snapshot(store.get(game_id)))
                except Exception:
                    hub.unsubscribe(game_id, queue)
                    raise
        except StoreError as error:
            await websocket.close(code=1013 if error.status == 503 else 1008,
                                  reason="Game busy; retry shortly" if error.status == 503 else "Game unavailable")
            return
        try:
            # Subscribe before yielding to accept: no accepted command can fall
            # between the initial snapshot and the event stream.
            await websocket.accept()
            await websocket.send_json({"type": "state.resync", "game_id": game_id, "version": initial["version"], "state": initial})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    current = await run_in_threadpool(lambda: public_snapshot(store.get(game_id)))
                    fields = ("total_toi_ms", "current_shift_ms", "completed_shifts", "average_shift_ms", "longest_shift_ms", "toi_by_period")
                    event = {"type": "clock.tick", "game_id": game_id, "version": current["version"], "server_time_ms": now_ms(), "clock": current["clock"], "toi": [{"player_id": p["id"], **{key: p[key] for key in fields}} for p in current["players"]], "warnings": current["warnings"], "strengths": current["strengths"]}
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            hub.unsubscribe(game_id, queue)

    return app


app = create_app()
