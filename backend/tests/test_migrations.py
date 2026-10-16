from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from toi.adapters import Principal
from toi.database import make_engine
from toi.models import LocationRow, ShiftRow
from toi.store import Store, public_snapshot


def test_cli_migration_creates_sqlite_parent_on_clean_checkout(tmp_path, monkeypatch):
    database_path = tmp_path / "not-created" / "nested" / "toi.db"
    monkeypatch.setenv("TOI_DATABASE_URL", f"sqlite:///{database_path}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    assert database_path.is_file()
    engine = make_engine(f"sqlite:///{database_path}")
    assert {"games", "action_receipts", "audit_events", "shifts", "alembic_version"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_upgrade_is_repeatable_and_preserves_history_and_projections(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'migration.db'}")
    store.migrate()
    initial = store.create("demo")
    store.apply("demo", {"request_id": "migration-test-move", "expected_version": initial["version"], "type": "move", "payload": {"player_id": "demo-home-p01", "to": "PENALTY_BOX"}}, Principal("test"))
    store.migrate()
    state = public_snapshot(store.get("demo"))
    assert state["version"] == 1
    assert len(state["events"]) == 1
    with store.session() as session:
        locations = {r.player_id: r.location for r in session.scalars(select(LocationRow))}
        assert locations == {p["id"]: p["location"] for p in state["players"]}
        rows = session.scalars(select(ShiftRow)).all()
        assert {s.id: s.end_playing_ms for s in rows} == {s["id"]: s["end_playing_ms"] for s in state["shifts"]}
    store.engine.dispose()
