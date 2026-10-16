"""Preview-only provider with short-lived cache and explicit stale recovery."""

from collections import OrderedDict
from copy import deepcopy
import threading
import time
from typing import Any, Callable

import httpx

from .client import HokejovyZapisClient, ProviderConfig
from .errors import ProviderError
from .normalizer import normalize_game, positive_id


class HokejovyZapisProvider:
    def __init__(
        self, config: ProviderConfig | None = None, *, client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config or ProviderConfig.from_env()
        self.client = HokejovyZapisClient(self.config, client=client, transport=transport, sleep=sleep)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def _cached(self, value: dict, now: int, *, stale: bool = False, error: ProviderError | None = None) -> dict:
        result = deepcopy(value)
        age = max(0, now - result["fetched_at_ms"])
        result["cache"] = {"hit": True, "stale": stale, "age_ms": age, "expires_at_ms": result["fetched_at_ms"] + int(self.config.cache_ttl_seconds * 1000)}
        if stale:
            result["warnings"].append("Source refresh failed; this is a stale cached preview from the last successful fetch. Review its age before importing.")
        if error:
            result["cache_error"] = {"code": error.code, "message": error.message}
        return result

    def preview(self, game_id: str, force: bool = False) -> dict[str, Any]:
        """Fetch/normalize without touching local games, clocks or player states.

        A normal expired-cache fetch may fall back with a prominent stale warning.
        A forced refresh always reports its new failure; the structured exception
        exposes a previous cached preview explicitly rather than pretending success.
        """
        game_id = positive_id(game_id, "Game ID")
        with self._lock:
            now = self._now_ms()
            cached = self._cache.get(game_id)
            if cached:
                self._cache.move_to_end(game_id)
                if not force and now - cached["fetched_at_ms"] < self.config.cache_ttl_seconds * 1000:
                    return self._cached(cached, now)
            try:
                raw = self.client.fetch(game_id)
                result = normalize_game(raw, game_id, self._now_ms())
            except ProviderError as exc:
                if cached:
                    fallback = self._cached(cached, self._now_ms(), stale=True, error=exc)
                    if not force:
                        return fallback
                    exc.cached_preview = fallback
                raise
            self._cache[game_id] = deepcopy(result)
            self._cache.move_to_end(game_id)
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
            result["cache"] = {"hit": False, "stale": False, "age_ms": 0, "expires_at_ms": result["fetched_at_ms"] + int(self.config.cache_ttl_seconds * 1000)}
            return result
