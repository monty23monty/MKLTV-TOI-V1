"""Accelerated, isolated SQLite storage measurement; not a LAN/soak benchmark.

Run with the backend dependencies installed, for example:
  python scripts/benchmark_service_store.py --history-events 300 --observations 300
The temporary database is always separate from application data. Each clock
reading advances logical time by one second without sleeping in real time.
"""
import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import toi.store as storage
from sqlalchemy import select, text
from toi.models import AuditRow, ChangeRow, ReceiptRow
from toi.store import Store


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def percentile(values, percent):
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent / 100
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)


def measure(store):
    state = store.get("benchmark")
    with store.session() as session:
        receipts = list(session.scalars(select(ReceiptRow)))
        journal = list(session.scalars(select(ChangeRow)))
        audits = list(session.scalars(select(AuditRow)))
    with store.engine.connect() as connection:
        pages = connection.execute(text("PRAGMA page_count")).scalar_one()
        page_size = connection.execute(text("PRAGMA page_size")).scalar_one()
    return {
        "state_json_bytes": json_bytes(state),
        "state_event_count": len(state["events"]),
        "state_shift_count": len(state["shifts"]),
        "history_json_bytes": json_bytes({"events": state["events"], "shifts": state["shifts"]}),
        "receipt_count": len(receipts),
        "receipt_response_json_bytes": sum(json_bytes(row.response) for row in receipts),
        "largest_receipt_response_json_bytes": max((json_bytes(row.response) for row in receipts), default=0),
        "journal_count": len(journal),
        "journal_json_bytes": sum(json_bytes(row.record) for row in journal),
        "clock_journal_count": sum(row.record["kind"] == "clock.observed" for row in journal),
        "clock_journal_json_bytes": sum(json_bytes(row.record) for row in journal if row.record["kind"] == "clock.observed"),
        "audit_command_json_bytes": sum(json_bytes(row.command) for row in audits),
        "sqlite_allocated_bytes": pages * page_size,
    }


def run(history_events, observations):
    os.environ["TOI_STREAM_ENABLED"] = "true"
    os.environ["TOI_STREAM_JOURNAL_RETENTION_DAYS"] = "7"
    os.environ["TOI_STREAM_CLOCK_STALE_MS"] = "3000"
    instant = [1_000_000]
    original_now = storage.now_ms
    storage.now_ms = lambda: instant[0]
    principal = SimpleNamespace(actor="service:benchmark-clock", service_id="benchmark-clock",
                                permissions=frozenset({"players:write", "clock:control", "clock:authority", "clock:write"}),
                                team_ids=None, clock_sources=frozenset({"benchmark-scoreboard"}), can_control_clock=True)
    version = 0
    try:
        with TemporaryDirectory(prefix="toi-storage-benchmark-") as directory:
            store = Store(f"sqlite:///{Path(directory) / 'isolated.db'}")
            try:
                store.migrate()
                store.create("benchmark")

                def command(kind, payload):
                    nonlocal version
                    ack, changed = store.apply_service("benchmark", {
                        "request_id": str(uuid4()), "expected_version": version,
                        "command": {"type": kind, "payload": payload},
                    }, principal, instant[0])
                    assert changed
                    version = ack["committed_version"]

                command("clock", {"operation": "start"})
                preload_start = time.perf_counter()
                for index in range(history_events):
                    instant[0] += 1000
                    command("move", {"player_id": "benchmark-home-p01", "to": "BENCH" if index % 2 == 0 else "ON_ICE"})
                preload_seconds = time.perf_counter() - preload_start
                command("clock.authority.set", {"mode": "external", "source": "benchmark-scoreboard", "service_id": "benchmark-clock"})
                initial = store.get("benchmark")
                authority = initial["clock_authority"]["generation"]
                remaining = initial["clock"]["remaining_ms"]
                before = measure(store)
                latencies = []
                for index in range(observations):
                    instant[0] += 1000
                    data = {"request_id": str(uuid4()), "expected_version": version,
                            "authority_generation": authority, "source": "benchmark-scoreboard",
                            "source_sequence": index + 1, "observed_at_ms": instant[0],
                            "period": 1, "remaining_ms": max(0, remaining - index * 1000),
                            "running": remaining - index * 1000 > 0}
                    started = time.perf_counter_ns()
                    ack, changed = store.apply_service("benchmark", data, principal, instant[0])
                    latencies.append((time.perf_counter_ns() - started) / 1_000_000)
                    assert changed
                    version = ack["committed_version"]
                after = measure(store)
                assert after["state_event_count"] == before["state_event_count"]
                assert after["state_shift_count"] == before["state_shift_count"]
                assert after["history_json_bytes"] == before["history_json_bytes"]
                assert after["clock_journal_count"] - before["clock_journal_count"] == observations
                report = {
                    "scope": "isolated accelerated SQLite storage only; no network, subscribers, real-time pacing or long-duration soak",
                    "measured_at_utc": datetime.now(UTC).isoformat(),
                    "platform": platform.platform(), "machine": platform.machine(),
                    "processor": platform.processor(), "python": platform.python_version(),
                    "cpu_logical_count": os.cpu_count(), "history_movements": history_events,
                    "observations": observations, "logical_clock_observation_interval_ms": 1000,
                    "preload_seconds": round(preload_seconds, 3),
                    "apply_service_latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95),
                                                 "p99": percentile(latencies, 99), "max": round(max(latencies), 3),
                                                 "mean": round(statistics.mean(latencies), 3)},
                    "before": before, "after": after,
                    "delta": {key: after[key] - before[key] for key in before},
                }
            finally:
                store.engine.dispose()
    finally:
        storage.now_ms = original_now
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-events", type=int, default=300)
    parser.add_argument("--observations", type=int, default=300)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 1 <= args.history_events <= 5000 or not 1 <= args.observations <= 100000:
        parser.error("history-events must be 1..5000 and observations 1..100000")
    report = json.dumps(run(args.history_events, args.observations), indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
