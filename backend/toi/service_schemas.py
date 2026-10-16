"""Strict v2 wire admission that reuses the existing domain command contracts."""

import json
import math
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from .schemas import Action
from .service_auth import MAX_INTEGER, _unique_object


ACTION_ADAPTER = TypeAdapter(Action)


class WireError(ValueError):
    def __init__(self, detail, code="INVALID_MESSAGE"):
        self.code = code
        super().__init__(detail)


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise WireError("Message fields do not match the v2 contract")


def _identifier(value, maximum=100):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise WireError("An identifier is invalid")


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        raise WireError("A UUID is required")
    try:
        UUID(value)
    except ValueError as error:
        raise WireError("A UUID is required") from error


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise WireError("An integer is outside the protocol range")


def bounded_json(value, depth=0):
    """Nested v1 commands must also respect JSON's interoperable numeric bounds."""
    if depth > 64:
        raise WireError("JSON nesting limit exceeded")
    if isinstance(value, dict):
        for key, item in value.items():
            bounded_json(key, depth + 1)
            bounded_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            bounded_json(item, depth + 1)
    elif type(value) is int and abs(value) > MAX_INTEGER:
        raise WireError("An integer is outside the protocol range")
    elif isinstance(value, float) and (not math.isfinite(value) or abs(value) > MAX_INTEGER):
        raise WireError("A number is outside the protocol range")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise WireError("JSON strings must encode as valid UTF-8") from error


def parse_message(raw, game_id):
    if not isinstance(raw, str):
        raise WireError("Application messages must be UTF-8 JSON text")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise WireError("Application messages must be valid UTF-8") from error
    if size > 65_536:
        raise WireError("Client message exceeds 64 KiB", "MESSAGE_TOO_LARGE")
    try:
        message = json.loads(raw, object_pairs_hook=_unique_object,
                             parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as error:
        raise WireError("Malformed JSON or duplicate property") from error
    bounded_json(message)
    _shape(message, {"protocol", "type", "message_id", "game_id", "data"})
    if type(message["protocol"]) is not int or message["protocol"] != 2:
        raise WireError("Protocol 2 is required", "UNSUPPORTED_PROTOCOL")
    _uuid(message["message_id"])
    _identifier(message["game_id"])
    if message["game_id"] != game_id:
        raise WireError("Message game_id does not match the connection")
    kind, data = message["type"], message["data"]
    if not isinstance(kind, str):
        raise WireError("Message type must be a string")
    if kind == "session.open":
        _shape(data, {"client_id", "journal_mode", "tick_hz"}, {"after_cursor"})
        _identifier(data["client_id"])
        if not isinstance(data["journal_mode"], str) or data["journal_mode"] not in {"now", "beginning", "resume"}:
            raise WireError("Unsupported journal mode")
        if type(data["tick_hz"]) is not int or data["tick_hz"] not in {0, 1, 2, 5, 10}:
            raise WireError("Unsupported live sample rate")
        if (data["journal_mode"] == "resume") != ("after_cursor" in data):
            raise WireError("Only resume mode requires after_cursor")
        if "after_cursor" in data:
            _identifier(data["after_cursor"], 512)
    elif kind == "command.submit":
        _shape(data, {"request_id", "expected_version", "command"})
        _uuid(data["request_id"])
        _integer(data["expected_version"])
        _shape(data["command"], {"type", "payload"})
        command = data["command"]
        if not isinstance(command["type"], str):
            raise WireError("Command type must be a string", "INVALID_COMMAND")
        if command["type"] == "clock.authority.set":
            payload = command["payload"]
            _shape(payload, {"mode", "source", "service_id"})
            if payload["mode"] == "manual":
                if payload["source"] is not None or payload["service_id"] is not None:
                    raise WireError("Manual authority requires null source and service_id", "INVALID_COMMAND")
            elif payload["mode"] == "external":
                _identifier(payload["source"])
                _identifier(payload["service_id"])
            else:
                raise WireError("Unsupported authority mode", "INVALID_COMMAND")
        else:
            try:
                ACTION_ADAPTER.validate_python({"request_id": data["request_id"],
                                               "expected_version": data["expected_version"], **command}, strict=True)
            except ValidationError as error:
                raise WireError("Command payload does not match the domain contract", "INVALID_COMMAND") from error
    elif kind == "clock.observe":
        _shape(data, {"request_id", "expected_version", "authority_generation", "source", "source_sequence",
                      "observed_at_ms", "period", "remaining_ms", "running"})
        _uuid(data["request_id"])
        for key in {"expected_version", "observed_at_ms", "remaining_ms"}:
            _integer(data[key])
        for key in {"authority_generation", "source_sequence"}:
            _integer(data[key], 1)
        _integer(data["period"], 1, 20)
        _identifier(data["source"])
        if type(data["running"]) is not bool:
            raise WireError("running must be boolean")
    elif kind == "state.request":
        _shape(data, set())
    elif kind in {"ping", "pong"}:
        _shape(data, {"nonce", "client_sent_at_ms"})
        _identifier(data["nonce"])
        _integer(data["client_sent_at_ms"])
    else:
        raise WireError("Unknown client message type")
    return message
