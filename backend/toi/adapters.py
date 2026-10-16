"""Integration boundaries; no external Lightning TV API is assumed.

Roster providers supply a canonical game snapshot. Authentication providers map a
trusted credential to a principal. Clock providers supply timestamped observations
which the API validates before passing to the domain clock.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .seed import create_demo_state


@dataclass(frozen=True)
class Principal:
    actor: str
    team_ids: set[str] | None = None
    can_control_clock: bool = True


class AuthenticationProvider(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class RosterProvider(Protocol):
    def load_game(self, game_id: str) -> dict[str, Any]: ...


class DemoRosterProvider:
    def load_game(self, game_id: str = "demo") -> dict[str, Any]:
        return create_demo_state(game_id)


@dataclass(frozen=True)
class ClockObservation:
    game_id: str
    period: int
    remaining_ms: int
    running: bool
    observed_at_ms: int
    source: str


class ClockProvider(Protocol):
    source: str

    def read(self, game_id: str) -> ClockObservation | None: ...


class ManualClockProvider:
    """Manual input is submitted through API actions, rather than polled."""

    source = "manual"

    def read(self, game_id: str) -> ClockObservation | None:
        return None


def resolve_principal(authorization: str | None) -> Principal:
    """Resolve prototype bearer tokens or explicitly use local development mode.

    TOI_OPERATOR_TOKENS is a JSON mapping from opaque token to permissions. The
    application refuses malformed configured permissions rather than silently
    opening access. Empty auth configuration intentionally enables local demo use.
    Deploy behind TLS and replace this resolver with existing website auth.
    """
    configured = os.getenv("TOI_OPERATOR_TOKENS", "").strip()
    shared_token = os.getenv("TOI_AUTH_TOKEN", "").strip()
    if not configured and not shared_token:
        return Principal(actor="local-operator")
    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not credential:
        raise PermissionError("A valid bearer token is required")
    if configured:
        operators = json.loads(configured)
        if not isinstance(operators, dict):
            raise ValueError("TOI_OPERATOR_TOKENS must be a JSON object")
        for token, details in operators.items():
            if not isinstance(token, str) or not isinstance(details, dict):
                raise ValueError("Each configured operator must map a token to permissions")
            actor = details.get("actor")
            team_ids = details.get("team_ids")
            clock_permission = details.get("can_control_clock", False)
            if not isinstance(actor, str) or not actor.strip():
                raise ValueError("Each configured operator must have a nonempty actor")
            if team_ids is not None and (not isinstance(team_ids, list) or not all(isinstance(t, str) for t in team_ids)):
                raise ValueError("team_ids must be null or a list of team identifiers")
            if not isinstance(clock_permission, bool):
                raise ValueError("can_control_clock must be boolean")
            if hmac.compare_digest(credential.encode(), token.encode()):
                return Principal(actor, None if team_ids is None else set(team_ids), clock_permission)
    if shared_token and hmac.compare_digest(credential.encode(), shared_token.encode()):
        return Principal(actor="shared-operator")
    raise PermissionError("Invalid bearer token")
