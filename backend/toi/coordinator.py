"""One ordering boundary for operator, import and service mutations per game."""

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager

from starlette.concurrency import run_in_threadpool

from .store import StoreError, now_ms, public_snapshot


class MutationCoordinator:
    def __init__(self, store, hub, *, max_admissions=64):
        if type(max_admissions) is not int or max_admissions < 1:
            raise ValueError("Per-game admission capacity must be a positive integer")
        self.store = store
        self.hub = hub
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._admissions: dict[str, int] = defaultdict(int)
        self.max_admissions = max_admissions
        self.notify = None

    @asynccontextmanager
    async def lock(self, game_id: str):
        """Bound active work plus waiters across operator and service ingress.

        Admission and counters run on the backend event loop. Accepted callers
        retain asyncio.Lock's FIFO order; cancellation releases their admission
        even while they are still waiting for the game's mutation boundary.
        """
        if self._admissions[game_id] >= self.max_admissions:
            raise StoreError(503, "This game is busy; retry after a short delay", "SERVER_OVERLOADED")
        self._admissions[game_id] += 1
        acquired = False
        lock = self._locks[game_id]
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            self._admissions[game_id] -= 1
            if not self._admissions[game_id]:
                del self._admissions[game_id]

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
