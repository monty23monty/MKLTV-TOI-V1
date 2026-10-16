"""Bounded server-side requests to one configured, allowlisted HTTPS source."""

from dataclasses import dataclass
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from .errors import ProviderError
from .normalizer import positive_id

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = "https://api.hokejovyzapis.cz"
    api_prefix: str = "/v1/d07ssvctxm"
    timeout_seconds: float = 10.0
    cache_ttl_seconds: float = 60.0
    allowed_hosts: tuple[str, ...] = ("api.hokejovyzapis.cz",)

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            valid = (
                url.scheme == "https" and url.hostname in self.allowed_hosts
                and url.port in {None, 443} and not url.username and not url.password
                and url.path in {"", "/"} and not url.query and not url.fragment
                and bool(re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname or ""))
                and "." in (url.hostname or "")
            )
            try:
                ipaddress.ip_address(url.hostname or "")
                valid = False
            except ValueError:
                pass
            if (url.hostname or "").endswith((".localhost", ".local", ".internal")):
                valid = False
        except ValueError:
            valid = False
        if not valid:
            raise ProviderError("The Hokejový Zápis base URL must be an allowlisted HTTPS host without credentials, query or path.", "invalid_configuration", 503)
        if (
            not re.fullmatch(r"/[A-Za-z0-9_~./-]+", self.api_prefix)
            or "//" in self.api_prefix or any(segment in {".", ".."} for segment in self.api_prefix.split("/"))
        ):
            raise ProviderError("The Hokejový Zápis API prefix is invalid.", "invalid_configuration", 503)
        if not 0 < self.timeout_seconds <= 60 or not 0 <= self.cache_ttl_seconds <= 3600:
            raise ProviderError("Provider timeout must be 0–60 seconds and cache lifetime 0–3600 seconds.", "invalid_configuration", 503)

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        try:
            return cls(
                base_url=os.getenv("HOKEJOVY_ZAPIS_BASE_URL", "https://api.hokejovyzapis.cz").rstrip("/"),
                api_prefix=os.getenv("HOKEJOVY_ZAPIS_API_PREFIX", "/v1/d07ssvctxm").rstrip("/"),
                timeout_seconds=float(os.getenv("HOKEJOVY_ZAPIS_TIMEOUT_SECONDS", "10")),
                cache_ttl_seconds=float(os.getenv("HOKEJOVY_ZAPIS_CACHE_TTL_SECONDS", "60")),
                allowed_hosts=tuple(host.strip().lower() for host in os.getenv("HOKEJOVY_ZAPIS_ALLOWED_HOSTS", "api.hokejovyzapis.cz").split(",") if host.strip()),
            )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError("Provider environment settings contain an invalid numeric value.", "invalid_configuration", 503) from exc

    def endpoint(self, game_id: str) -> str:
        game_id = positive_id(game_id, "Game ID")
        return f"{self.base_url.rstrip('/')}{self.api_prefix.rstrip('/')}/matches/{game_id}/game-info.json"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError(f"The source JSON repeats field or roster slot {key!r}; importing would be ambiguous.", "duplicate_json_key", 409)
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ProviderError(f"The roster source returned invalid JSON numeric value {value}.", "malformed_json", 502)


class HokejovyZapisClient:
    def __init__(
        self, config: ProviderConfig | None = None, *, client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config or ProviderConfig.from_env()
        self._client = client
        self._transport = transport
        self._sleep = sleep

    def fetch(self, game_id: str) -> Any:
        url = self.config.endpoint(game_id)
        if self._client is not None:
            return self._fetch_with_client(self._client, url)
        with httpx.Client(transport=self._transport, trust_env=False) as client:
            return self._fetch_with_client(client, url)

    def _fetch_with_client(self, client: httpx.Client, url: str) -> Any:
        last_error: ProviderError | None = None
        for attempt in range(3):
            retry = False
            try:
                with client.stream("GET", url, timeout=self.config.timeout_seconds, follow_redirects=False,
                                   headers={"Accept": "application/json", "User-Agent": "LightningTV-TOI/1.0 roster-import"}) as response:
                    status = response.status_code
                    if status == 404:
                        raise ProviderError("No match was found for this Hokejový Zápis Game ID.", "game_not_found", 404)
                    if status == 429 or status >= 500:
                        retry = True
                        last_error = ProviderError("The roster source is temporarily busy. Try again shortly.", "rate_limited" if status == 429 else "upstream_error", 503 if status == 429 else 502)
                        LOGGER.warning("Hokejovy Zapis returned HTTP %s on attempt %s", status, attempt + 1)
                    elif 300 <= status < 400:
                        raise ProviderError("The roster source redirected the request; redirects are disabled.", "redirect_blocked", 502)
                    elif status != 200:
                        raise ProviderError(f"The roster source rejected the request (HTTP {status}).", "upstream_rejected", 502)
                    else:
                        length = response.headers.get("content-length")
                        if length and length.isdecimal() and int(length) > MAX_BODY_BYTES:
                            raise ProviderError("The roster source response exceeds the 2 MiB limit.", "response_too_large", 502)
                        chunks = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_BODY_BYTES:
                                raise ProviderError("The roster source response exceeds the 2 MiB limit.", "response_too_large", 502)
                            chunks.append(chunk)
                        try:
                            return json.loads(b"".join(chunks), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                        except ProviderError:
                            raise
                        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                            raise ProviderError("The roster source returned malformed JSON.", "malformed_json", 502) from exc
            except httpx.RequestError as exc:
                retry = True
                last_error = ProviderError("The roster source could not be reached before the request timed out. Try again.", "source_unavailable", 502)
                LOGGER.warning("Hokejovy Zapis request failed on attempt %s: %s", attempt + 1, type(exc).__name__)
            if not retry:
                break
            if attempt < 2:
                self._sleep(0.1 * (2 ** attempt))
        raise last_error or ProviderError("The roster source request failed.")
