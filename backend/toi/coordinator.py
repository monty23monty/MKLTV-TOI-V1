"""One ordering boundary for operator, import and service mutations per game."""

import asyncio
from collections import defaultdict

from starlette.concurrency import run_in_threadpool

from .store import now_ms, public_snapshot


class MutationCoordinator:
    def __init__(self, store, hub):
        self.store = store
        self.hub = hub
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.notify = None

    def lock(self, game_id: str):
        return self._locks[game_id]

    async def changed(self, game_id, snapshot=None, kind="state.resync", request_id=None):
        """Call inside the game lock, after a successful database commit."""
        if snapshot is None:
            snapshot = await run_in_threadpool(lambda: public_snapshot(self.store.get(game_id)))
        event = {
            "type": kind, "game_id": game_id, "version": snapshot["version"],
            "server_time_ms": now_ms(), "state": snapshot,
        }
        if request_id is not None:
            event["request_id"] = request_id
        self.hub.publish(game_id, event)
        if self.notify is not None:
            self.notify(game_id)
