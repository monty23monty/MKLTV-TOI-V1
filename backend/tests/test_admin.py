"""A restored database cannot silently reuse external service checkpoints."""

import json

import pytest

from toi.admin import main
from toi.store import Store, StoreError


def test_restore_rotation_preserves_toi_and_invalidates_old_cursor(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'restore.db'}"
    monkeypatch.setenv("TOI_DATABASE_URL", url)
    monkeypatch.setenv("TOI_STREAM_ENABLED", "true")
    store = Store(url)
    store.migrate()
    store.create("restored-game")
    before = store.live("restored-game")
    assert main(["rotate-journals", "--all", "--confirm-offline"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["games"][0]["game_id"] == "restored-game"
    after = store.live("restored-game")
    assert before["state"]["version"] == after["state"]["version"]
    assert before["state"]["players"] == after["state"]["players"]
    with pytest.raises(StoreError) as caught:
        store.journal_page("restored-game", after=before["baseline_cursor"])
    assert caught.value.code == "CURSOR_WRONG_GENERATION"
    store.engine.dispose()


def test_offline_confirmation_required():
    with pytest.raises(SystemExit):
        main(["rotate-journals", "--all"])
