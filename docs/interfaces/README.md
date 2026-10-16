# Service stream contract tools

**Protocol v2, implemented in release 2.0.1.** Start with [SERVICE_INTERFACE.md](../../SERVICE_INTERFACE.md) and enable/configure service streaming as documented there. Legacy endpoints remain in [API.md](../../API.md).

| File | Purpose |
| --- | --- |
| `service-stream-v2.schema.json` | Draft 2020-12 JSON Schema; separate `$defs.ClientMessage` and `$defs.ServerMessage`, plus their root union |
| `examples.json` | Complete accepted wire frames and rejected cases with named directions; repeated negative cases use a named base frame plus JSON Pointer edits |
| `requirements.txt` | Optional documentation validator dependency; not part of the production images |
| `../../scripts/validate_service_interface.py` | Offline schema, examples, direction and optional existing-v1 payload consistency checks |

The example live rosters are representative small rosters, not a six-player rule or a claim that the application has only those players. Real messages carry every rostered player. IDs/cursors/timestamps are fixture data, not real credentials or a working service connection.

## Run the checks

Create an isolated tooling environment under the ignored `artifacts` directory. Linux:

```bash
python3 -m venv artifacts/interface-tools
artifacts/interface-tools/bin/python -m pip install -r docs/interfaces/requirements.txt
artifacts/interface-tools/bin/python scripts/validate_service_interface.py
```

PowerShell:

```powershell
python -m venv artifacts/interface-tools
& ./artifacts/interface-tools/Scripts/python.exe -m pip install -r docs/interfaces/requirements.txt
& ./artifacts/interface-tools/Scripts/python.exe scripts/validate_service_interface.py
```

For the `--check-v1` option, install `backend/requirements.lock.txt` into the same tooling environment, or run with an existing backend environment plus the documentation requirement:

```bash
python -m pip install -r backend/requirements.lock.txt -r docs/interfaces/requirements.txt
python scripts/validate_service_interface.py --check-v1
```

This compares the embedded existing command-payload schemas against their current Pydantic source and checks examples for all six v1 command types. It detects contract drift without introducing another application command implementation.

Validate an individual complete JSON frame with:

```bash
python scripts/validate_service_interface.py --message path/to/frame.json --direction client
```

Use `--direction server` for server frames; the default `either` accepts either direction. The tool does not contact a service, install packages or fetch remote schema references. Credential values are unnecessary. Package installation above requires access to PyPI; validation itself is offline.

## What validation proves

The check validates the schema itself, all accepted/rejected examples, direction separation, every defined message type, local-only references and optional payload consistency with the current backend. JSON Schema does not prove authorization, monotonic sequences, cursor binding, durable transactions, timing calculations, throughput or replay correctness. Those semantic and runtime acceptance tests are specified in the main design and are covered by runtime tests separately; full-duration capacity testing still needs operational measurement.
