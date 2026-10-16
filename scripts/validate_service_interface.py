"""Validate the DESIGN-ONLY service-stream v2 schema and examples without a server.

Install docs/interfaces/requirements.txt in a separate environment first. This
script never installs dependencies, contacts services, or retrieves remote refs.
Run ``python scripts/validate_service_interface.py --check-v1`` in an environment
containing backend dependencies to detect drift between the published command
payload schema and current Pydantic models. Rejected examples name a positive
base fixture and local JSON Pointer edits to keep the contract corpus readable.
"""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_TYPES = {"session.open", "command.submit", "clock.observe", "state.request", "ping", "pong"}
SERVER_TYPES = {
    "session.welcome", "state.snapshot", "event.recorded", "stream.ready", "live.tick",
    "command.ack", "command.error", "replay.required", "session.error", "ping", "pong",
}
V1_TYPES = {"substitute", "move", "clock", "undo", "correct", "setup"}


class ContractFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ContractFailure(message)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ContractFailure(f"Non-JSON numeric constant: {value}")


def read_json(path):
    with path.open(encoding="utf-8") as source:
        return json.load(source, object_pairs_hook=no_duplicates, parse_constant=reject_constant)


def walk_refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"$ref", "$dynamicRef"}:
                require(isinstance(value, str) and value.startswith("#/"),
                        f"Only local JSON Pointer references are allowed: {value}")
                yield value
            else:
                yield from walk_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_refs(value)


def export_v1_payloads():
    """Export reachable v1 payload definitions directly from the existing models."""
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from pydantic import TypeAdapter
        from toi.schemas import Action
    except ImportError as error:
        raise ContractFailure("--check-v1 needs the existing backend's Python dependencies") from error
    exported = TypeAdapter(Action).json_schema()
    original = exported["$defs"]
    payload_definitions = {}
    variants = []

    def rename(node):
        if isinstance(node, dict):
            return {key: rename(value) for key, value in node.items()}
        if isinstance(node, list):
            return [rename(value) for value in node]
        if isinstance(node, str) and node.startswith("#/$defs/"):
            return node.replace("#/$defs/", "#/$defs/V1_", 1)
        return node

    def include(node):
        for reference in walk_refs(node):
            name = reference.removeprefix("#/$defs/")
            if name not in payload_definitions:
                payload_definitions[name] = original[name]
                include(original[name])

    for variant in exported["oneOf"]:
        definition = original[variant["$ref"].removeprefix("#/$defs/")]
        fields = definition["properties"]
        payload = deepcopy(fields["payload"])
        payload.pop("default", None)
        include(payload)
        variants.append({
            "type": "object", "additionalProperties": False,
            "required": ["type", "payload"],
            "properties": {"type": deepcopy(fields["type"]), "payload": payload},
        })
    result = {"V1Command": rename({"oneOf": variants})}
    result.update({f"V1_{name}": rename(value) for name, value in sorted(payload_definitions.items())})
    return result


def validate_v1(schema, examples):
    expected = export_v1_payloads()
    actual = {name: value for name, value in schema["$defs"].items() if name == "V1Command" or name.startswith("V1_")}
    require(actual == expected,
            "Embedded v1 payload definitions drifted from backend/toi/schemas.py; regenerate and review the design")
    from pydantic import TypeAdapter
    from toi.schemas import Action
    adapter = TypeAdapter(Action)
    checked = set()
    for example in examples["valid"]:
        message = example["message"]
        if message["type"] != "command.submit":
            continue
        data = message["data"]
        command = data["command"]
        if command["type"] in V1_TYPES:
            adapter.validate_python({"request_id": data["request_id"], "expected_version": data["expected_version"], **command})
            checked.add(command["type"])
    require(checked == V1_TYPES, "Valid examples must cover all six current v1 command payloads")
    return len(expected), len(checked)


def create_validators(schema):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from jsonschema.exceptions import SchemaError
        from referencing import Registry, Resource
    except ImportError as error:
        raise ContractFailure("Install docs/interfaces/requirements.txt in your validation environment first") from error

    require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
            "The contract must explicitly select JSON Schema Draft 2020-12")
    require(schema.get("$id", "").startswith("urn:"), "The schema must have a non-network URN identity")
    for reference in walk_refs(schema):
        target = schema
        for token in reference[2:].split("/"):
            target = target[token.replace("~1", "/").replace("~0", "~")]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ContractFailure("The document is not a valid Draft 2020-12 schema") from error

    def deny_remote(uri):
        raise ContractFailure(f"Remote schema retrieval is forbidden: {uri}")

    registry = Registry(retrieve=deny_remote).with_resource(schema["$id"], Resource.from_contents(schema))
    options = {"format_checker": FormatChecker(), "registry": registry}
    validators = {"either": Draft202012Validator(schema, **options)}
    for direction, definition in (("client", "ClientMessage"), ("server", "ServerMessage")):
        wrapper = {"$ref": f"{schema['$id']}#/$defs/{definition}"}
        validators[direction] = Draft202012Validator(wrapper, **options)
    return validators


def expand_example(example, bases):
    if "message" in example:
        require("base" not in example, "An example cannot have both message and base")
        return example["message"]
    require(example.get("base") in bases, "A rejected example must reference a valid local base fixture")
    message = deepcopy(bases[example["base"]])
    require(isinstance(example.get("changes"), list), "A rejected fixture must explicitly list its local changes")
    for change in example["changes"]:
        operation, pointer = change["op"], change["path"]
        require(operation in {"add", "replace", "remove"}, "Only local add/replace/remove fixture edits are supported")
        require(isinstance(pointer, str) and pointer.startswith("/"), "Fixture edits need a non-root JSON Pointer")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
        target = message
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        key = int(parts[-1]) if isinstance(target, list) else parts[-1]
        if operation == "remove":
            target.pop(key)
        elif operation == "replace":
            require(key in target if isinstance(target, dict) else 0 <= key < len(target),
                    "A replace edit must target an existing fixture field")
            target[key] = deepcopy(change["value"])
        else:
            require(isinstance(target, dict) and key not in target, "An add edit must introduce a new object property")
            target[key] = deepcopy(change["value"])
    return message


def validate_corpus(schema, corpus, validators):
    require(corpus.get("status") == "DESIGN_ONLY", "Examples must explicitly identify their DESIGN_ONLY status")
    require(corpus.get("protocol") == 2, "Examples must identify protocol 2")
    valid = corpus["valid"]
    invalid = corpus["invalid"]
    require(valid and invalid, "Both valid and rejected examples are required")
    covered = {"client": set(), "server": set()}
    names = set()
    bases = {example["name"]: example["message"] for example in valid}
    for expected, examples in ((True, valid), (False, invalid)):
        for example in examples:
            name, direction = example["name"], example["direction"]
            message = expand_example(example, bases)
            require(name not in names, f"Duplicate example name: {name}")
            names.add(name)
            require(direction in validators, f"Invalid direction for {name}")
            passes = validators[direction].is_valid(message)
            require(passes == expected, f"{name}: expected {'acceptance' if expected else 'rejection'} in {direction} direction")
            if expected:
                require(direction != "either", f"{name}: valid examples must state an exact direction")
                require(validators["either"].is_valid(message), f"{name}: root union rejected valid example")
                opposite = "server" if direction == "client" else "client"
                require(not validators[opposite].is_valid(message), f"{name}: direction is ambiguous")
                covered[direction].add(message["type"])
    require(covered["client"] == CLIENT_TYPES, "Examples must cover every client message type")
    require(covered["server"] == SERVER_TYPES, "Examples must cover every server message type")
    require("events" not in schema["$defs"]["LiveState"]["properties"], "LiveState must omit event histories")
    require("shifts" not in schema["$defs"]["LiveState"]["properties"], "LiveState must omit shift histories")
    return len(valid), len(invalid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=ROOT / "docs/interfaces/service-stream-v2.schema.json")
    parser.add_argument("--examples", type=Path, default=ROOT / "docs/interfaces/examples.json")
    parser.add_argument("--check-v1", action="store_true", help="Compare payload definitions and examples with existing Pydantic models")
    parser.add_argument("--message", type=Path, help="Also validate one JSON message from this file")
    parser.add_argument("--direction", choices=["client", "server", "either"], default="either")
    args = parser.parse_args()
    try:
        schema, corpus = read_json(args.schema), read_json(args.examples)
        validators = create_validators(schema)
        accepted, rejected = validate_corpus(schema, corpus, validators)
        if args.check_v1:
            definitions, types = validate_v1(schema, corpus)
            print(f"Existing v1 models: {definitions} definitions match; {types} command types accepted")
        if args.message:
            # Do not print validator errors verbatim: they include the complete
            # supplied instance, which may contain private integration data.
            require(validators[args.direction].is_valid(read_json(args.message)),
                    f"The supplied message is invalid in {args.direction} direction")
            print(f"Message accepted in {args.direction} direction: {args.message}")
        print(f"DESIGN_ONLY protocol 2: schema valid; {accepted} positive and {rejected} negative examples passed")
    except (ContractFailure, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Contract validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
