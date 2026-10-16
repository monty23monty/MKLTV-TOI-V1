"""Fail-closed identities for the additive service API; operator tokens never apply."""

from dataclasses import dataclass
import hmac
import json
import os

from .store import now_ms


PERMISSIONS = frozenset({"stream:read", "journal:read", "players:write", "clock:write",
                         "clock:control", "clock:authority", "history:write", "setup:write"})
READ_PERMISSIONS = frozenset({"stream:read", "journal:read"})
MAX_INTEGER = 9_007_199_254_740_991


class ServiceAuthenticationError(PermissionError):
    def __init__(self, detail="A valid service bearer token is required", *, expired=False):
        self.expired = expired
        super().__init__(detail)


@dataclass(frozen=True)
class ServicePrincipal:
    service_id: str
    game_ids: frozenset[str]
    permissions: frozenset[str]
    team_ids: frozenset[str] | None = None
    clock_sources: frozenset[str] = frozenset()
    expires_at_ms: int | None = None

    @property
    def actor(self):
        return f"service:{self.service_id}"

    @property
    def can_control_clock(self):
        return "clock:control" in self.permissions

    def allows_game(self, game_id):
        return "*" in self.game_ids or game_id in self.game_ids


def _strings(value, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError("Invalid service identity configuration")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 100 for item in value):
        raise ValueError("Invalid service identity configuration")
    if len(set(value)) != len(value):
        raise ValueError("Invalid service identity configuration")
    return frozenset(value)


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def service_identities():
    """Re-read on every authentication so configuration replacement revokes sockets."""
    configured = os.getenv("TOI_SERVICE_TOKENS", "").strip()
    if not configured:
        return []
    try:
        identities = json.loads(configured, object_pairs_hook=_unique_object)
        if not isinstance(identities, dict):
            raise ValueError
        result = []
        allowed = {"service_id", "game_ids", "permissions", "team_ids", "clock_sources", "expires_at_ms"}
        for token, entry in identities.items():
            if not isinstance(token, str) or not token.strip() or len(token) > 4096:
                raise ValueError
            if not isinstance(entry, dict) or set(entry) - allowed:
                raise ValueError
            service_id = entry.get("service_id")
            # The journal actor has a 100-character wire bound, including 'service:'.
            if not isinstance(service_id, str) or not service_id.strip() or len(service_id) > 92:
                raise ValueError
            games = _strings(entry.get("game_ids"), nonempty=True)
            if "*" in games and games != {"*"}:
                raise ValueError
            permissions = _strings(entry.get("permissions"), nonempty=True)
            if not permissions <= PERMISSIONS:
                raise ValueError
            teams = entry.get("team_ids")
            teams = None if teams is None else _strings(teams)
            sources = _strings(entry.get("clock_sources", []))
            if "*" in sources:
                raise ValueError
            expiry = entry.get("expires_at_ms")
            if expiry is not None and (type(expiry) is not int or not 0 <= expiry <= MAX_INTEGER):
                raise ValueError
            result.append((token, ServicePrincipal(service_id, games, permissions, teams, sources, expiry)))
        return result
    except (ValueError, TypeError, RecursionError) as error:
        # Never put the token mapping or JSON decoder excerpt into a response/log.
        raise ValueError("TOI_SERVICE_TOKENS configuration is invalid") from error


def resolve_service(authorization):
    identities = service_identities()
    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not credential:
        raise ServiceAuthenticationError()
    for token, principal in identities:
        if hmac.compare_digest(credential.encode(), token.encode()):
            if principal.expires_at_ms is not None and principal.expires_at_ms <= now_ms():
                raise ServiceAuthenticationError("Service credential has expired", expired=True)
            return principal
    raise ServiceAuthenticationError()


def require_game(principal, game_id, permissions=READ_PERMISSIONS):
    if not principal.allows_game(game_id) or not permissions <= principal.permissions:
        raise PermissionError("Service is not authorized for this game and operation")


class StreamConfig:
    """Fixed wire limits stay in the protocol; tunable admission settings are strict."""

    def __init__(self):
        enabled = os.getenv("TOI_STREAM_ENABLED", "false").lower()
        if enabled not in {"true", "false", "1", "0"}:
            raise ValueError("TOI_STREAM_ENABLED must be true or false")
        self.enabled = enabled in {"true", "1"}
        self.tick_hz = self._integer("TOI_STREAM_TICK_HZ", 5, 1, 10)
        if self.tick_hz not in {1, 2, 5, 10}:
            raise ValueError("TOI_STREAM_TICK_HZ must be 1, 2, 5 or 10")
        self.retention_days = self._integer("TOI_STREAM_JOURNAL_RETENTION_DAYS", 7, 1, 3650)
        self.clock_stale_ms = self._integer("TOI_STREAM_CLOCK_STALE_MS", 3000, 1, 3_600_000)
        self.max_connections = self._integer("TOI_STREAM_MAX_CONNECTIONS", 32, 1, 1024)
        self.queue_bytes = self._integer("TOI_STREAM_QUEUE_MAX_BYTES", 8_388_608, 1_048_576, 67_108_864)
        service_identities()

    @staticmethod
    def _integer(name, default, minimum, maximum):
        raw = os.getenv(name, str(default))
        if not raw.isascii() or not raw.isdecimal() or not minimum <= int(raw) <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return int(raw)
