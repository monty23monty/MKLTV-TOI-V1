from copy import deepcopy
from uuid import uuid4

import pytest

from toi.domain import DomainError, apply_action, materialize_clock, snapshot


def game_state():
    positions = ["C", "LW", "RW", "D", "D", "G", "C", "LW", "RW", "D", "D", "G", "C", "LW", "RW", "D", "D", "G"]
    return {
        "game": {"id": "game", "name": "Test match", "status": "SETUP", "competition": "Test", "scheduled_start": None, "external_id": None, "period_lengths_ms": [1200000] * 3},
        "teams": [{"id": "home", "name": "Home", "short_name": "HOM", "color": "#D8B95A", "side": "home"}, {"id": "away", "name": "Away", "short_name": "AWY", "color": "#ffffff", "side": "away"}],
        "players": [
            {"id": f"{prefix}{index}", "team_id": team, "name": f"Player {index}", "number": index, "position": position, "eligible": index != 18, "location": "ON_ICE" if index <= 6 else "BENCH"}
            for prefix, team in [("h", "home"), ("a", "away")]
            for index, position in enumerate(positions, 1)
        ],
        "lines": [
            {"id": "line1", "team_id": "home", "name": "Line 1", "kind": "FORWARD", "player_ids": ["h1", "h2", "h3"]},
            {"id": "line2", "team_id": "home", "name": "Line 2", "kind": "FORWARD", "player_ids": ["h7", "h8", "h9"]},
            {"id": "pair2", "team_id": "home", "name": "Pair 2", "kind": "DEFENCE", "player_ids": ["h10", "h11"]},
        ],
        "clock": {"period": 1, "remaining_ms": 1200000, "running": False, "playing_ms": 0, "anchor_unix_ms": 1000, "period_playing_ms": {"1": 0}},
        "events": [], "shifts": [], "version": 0,
    }


def command(state, kind, payload, now=1000, **extra):
    return apply_action(state, {"request_id": str(uuid4()), "expected_version": state["version"], "type": kind, "payload": payload, **extra}, now)


def stats(state, player_id, now=1000):
    return next(p for p in snapshot(state, now)["players"] if p["id"] == player_id)


def started():
    return command(game_state(), "clock", {"operation": "start"})


def substitute(state, outgoing, incoming, now=61000, **extra):
    return command(state, "substitute", {"team_id": "home", "outgoing_ids": outgoing, "incoming_ids": incoming, **extra}, now)


def test_initial_snapshot_has_one_open_shift_per_ice_player_without_mutating_seed():
    state = game_state()
    public = snapshot(state, 1000)
    assert len(public["shifts"]) == 12
    assert all(s["end_playing_ms"] is None for s in public["shifts"])
    assert state["shifts"] == []
    assert not any(key.startswith("_") for key in public)
    assert public["strengths"] == {"home": 5, "away": 5}
    assert all(p["strength_toi"] is None for p in public["players"])


def test_individual_substitution_uses_one_captured_timestamp():
    state = substitute(started(), ["h1"], ["h7"], at_playing_ms=30000)
    assert stats(state, "h1", 61000)["total_toi_ms"] == 30000
    assert stats(state, "h7", 61000)["current_shift_ms"] == 30000
    assert stats(state, "h1", 61000)["completed_shifts"] == 1
    assert stats(state, "h7", 61000)["completed_shifts"] == 0
    event = snapshot(state, 61000)["events"][-1]
    assert event["playing_ms"] == 30000
    assert event["remaining_ms"] == 1170000
    assert event["voided"] is False


@pytest.mark.parametrize("outgoing,incoming", [
    (["h1", "h2", "h3"], ["h7", "h8", "h9"]),
    (["h4", "h5"], ["h10", "h11"]),
    (["h1", "h4"], ["h7", "h10"]),
    (["h1", "h2", "h3", "h4"], ["h7", "h8", "h9", "h10"]),
    (["h1", "h2", "h3", "h4", "h5"], ["h7", "h8", "h9", "h10", "h11"]),
])
def test_line_pair_and_mixed_group_changes_are_atomic(outgoing, incoming):
    state = substitute(started(), outgoing, incoming)
    assert len([e for e in state["events"] if e["type"] == "substitute"]) == 1
    assert all(stats(state, pid, 61000)["location"] == "BENCH" for pid in outgoing)
    assert all(stats(state, pid, 61000)["location"] == "ON_ICE" for pid in incoming)
    starts = {s["start_playing_ms"] for s in state["shifts"] if s["player_id"] in incoming}
    ends = {s["end_playing_ms"] for s in state["shifts"] if s["player_id"] in outgoing}
    assert starts == ends == {60000}


@pytest.mark.parametrize("destination", ["ON_ICE", "BENCH"])
def test_penalty_box_entry_and_manual_return(destination):
    state = command(started(), "move", {"player_id": "h1", "to": "PENALTY_BOX"}, 31000)
    assert stats(state, "h1", 61000)["total_toi_ms"] == 30000
    assert snapshot(state, 61000)["strengths"]["home"] == 4
    state = command(state, "move", {"player_id": "h1", "to": destination}, 61000)
    player = stats(state, "h1", 91000)
    assert player["location"] == destination
    assert player["total_toi_ms"] == (60000 if destination == "ON_ICE" else 30000)
    assert player["completed_shifts"] == 1


def test_bench_to_penalty_box_administrative_correction():
    state = command(game_state(), "move", {"player_id": "h7", "to": "PENALTY_BOX"})
    assert stats(state, "h7")["location"] == "PENALTY_BOX"
    assert stats(state, "h7")["total_toi_ms"] == 0


def test_extra_attacker_and_goalkeeper_return():
    state = substitute(started(), ["h6"], ["h7"])
    public = snapshot(state, 61000)
    assert public["strengths"]["home"] == 6
    assert "HOM: empty net." in public["warnings"]
    state = substitute(state, ["h7"], ["h6"], 91000)
    assert snapshot(state, 91000)["strengths"]["home"] == 5
    assert stats(state, "h6", 121000)["total_toi_ms"] == 90000
    assert stats(state, "h7", 121000)["total_toi_ms"] == 30000


def test_five_on_four_and_five_on_three_are_allowed():
    state = command(started(), "move", {"player_id": "h1", "to": "PENALTY_BOX"}, 11000)
    assert snapshot(state, 11000)["strengths"] == {"home": 4, "away": 5}
    state = command(state, "move", {"player_id": "h2", "to": "PENALTY_BOX"}, 21000)
    assert snapshot(state, 21000)["strengths"] == {"home": 3, "away": 5}
    assert stats(state, "h1", 61000)["total_toi_ms"] == 10000


@pytest.mark.parametrize("outgoing,incoming", [
    (["h1", "h1"], ["h7"]), (["h1"], ["h7", "h7"]),
    (["h1"], ["h1"]), (["h1"], ["a7"]), (["h7"], ["h8"]),
    (["h1"], ["h2"]), (["h1"], ["h18"]), (["missing"], ["h7"]),
])
def test_invalid_substitutions_leave_input_untouched(outgoing, incoming):
    state = started()
    before = deepcopy(state)
    with pytest.raises(DomainError):
        substitute(state, outgoing, incoming)
    assert state == before


def test_group_with_one_invalid_incoming_does_not_partly_move_players():
    state = command(started(), "move", {"player_id": "h8", "to": "PENALTY_BOX"}, 11000)
    before = deepcopy(state)
    with pytest.raises(DomainError):
        substitute(state, ["h1", "h2"], ["h7", "h8"])
    assert state == before


def test_running_stopped_pause_and_resume_do_not_accumulate_stoppage_time():
    state = command(started(), "clock", {"operation": "pause"}, 31000)
    assert stats(state, "h1", 121000)["total_toi_ms"] == 30000
    state = command(state, "clock", {"operation": "start"}, 121000)
    state = command(state, "clock", {"operation": "pause"}, 151000)
    assert stats(state, "h1", 900000)["total_toi_ms"] == 60000
    assert state["clock"]["remaining_ms"] == 1140000


def test_materialization_is_idempotent_and_backward_wall_time_does_not_double_count():
    state = materialize_clock(started(), 61000)
    assert materialize_clock(state, 61000) == state
    older = materialize_clock(state, 51000)
    later = materialize_clock(older, 71000)
    assert later["clock"]["playing_ms"] == 70000


def test_clock_corrections_change_display_without_negative_or_duplicate_toi():
    state = command(started(), "clock", {"operation": "set", "remaining_ms": 1180000}, 61000)
    assert state["clock"]["playing_ms"] == 60000
    assert stats(state, "h1", 121000)["total_toi_ms"] == 60000
    state = command(state, "clock", {"operation": "set", "remaining_ms": 1100000}, 121000)
    state = command(state, "clock", {"operation": "start"}, 121000)
    state = command(state, "clock", {"operation": "pause"}, 151000)
    assert stats(state, "h1", 151000)["total_toi_ms"] == 90000
    assert sum(stats(state, "h1", 151000)["toi_by_period"].values()) == 90000


def test_period_boundaries_keep_active_shifts_and_split_per_period_toi():
    state = game_state()
    state["game"]["period_lengths_ms"] = [10000, 10000, 5000]
    state["clock"]["remaining_ms"] = 10000
    state = command(state, "clock", {"operation": "start"})
    state = command(state, "clock", {"operation": "advance"}, 51000)
    assert state["clock"]["playing_ms"] == 10000  # 40s after zero never accrues
    assert not state["clock"]["running"]
    state = command(state, "clock", {"operation": "start"}, 61000)
    state = substitute(state, ["h1"], ["h7"], 66000)
    p1 = stats(state, "h1", 68000)
    p7 = stats(state, "h7", 68000)
    assert p1["total_toi_ms"] == 15000
    assert p1["toi_by_period"] == {"1": 10000, "2": 5000, "3": 0}
    assert p1["completed_shifts"] == 1
    assert p7["toi_by_period"] == {"1": 0, "2": 2000, "3": 0}


def test_advance_requires_explicit_overtime_configuration():
    state = game_state()
    state["clock"]["period"] = 3
    with pytest.raises(DomainError):
        command(state, "clock", {"operation": "advance"})
    state = command(state, "clock", {"operation": "advance", "period_lengths_ms": [1200000, 1200000, 1200000, 300000]})
    assert state["clock"]["period"] == 4
    assert state["clock"]["remaining_ms"] == 300000


def test_multiple_shifts_average_excludes_current_open_shift():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    state = substitute(state, ["h7"], ["h1"], 61000)
    state = substitute(state, ["h1"], ["h7"], 111000)
    state = substitute(state, ["h7"], ["h1"], 131000)
    player = stats(state, "h1", 171000)
    assert player["total_toi_ms"] == 120000
    assert player["completed_shifts"] == 2
    assert player["average_shift_ms"] == 40000
    assert player["current_shift_ms"] == 40000
    assert player["longest_shift_ms"] == 50000


def test_undo_restores_open_shift_history_and_clock_survives():
    state = substitute(started(), ["h1", "h2", "h3"], ["h7", "h8", "h9"])
    state = command(state, "undo", {}, 91000)
    assert state["clock"]["playing_ms"] == 90000
    assert stats(state, "h1", 91000)["current_shift_ms"] == 90000
    assert stats(state, "h1", 91000)["completed_shifts"] == 0
    assert stats(state, "h7", 91000)["total_toi_ms"] == 0
    assert len(state["events"]) == 3
    assert state["events"][1]["active"] is False


def test_historical_correction_rebuilds_all_shift_statistics():
    state = substitute(started(), ["h1"], ["h7"])
    event_id = state["events"][-1]["id"]
    state = command(state, "correct", {"event_id": event_id, "replacement": {"type": "substitute", "payload": {"team_id": "home", "outgoing_ids": ["h1"], "incoming_ids": ["h13"]}}}, 91000)
    assert stats(state, "h7", 91000)["total_toi_ms"] == 0
    assert stats(state, "h13", 91000)["total_toi_ms"] == 30000
    assert stats(state, "h1", 91000)["total_toi_ms"] == 60000
    assert state["clock"]["playing_ms"] == 90000
    assert state["events"][1]["active"] is False


def test_correction_or_undo_rejects_dependent_history_atomically():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    earlier_id = state["events"][-1]["id"]
    state = substitute(state, ["h7"], ["h1"], 61000)
    before = deepcopy(state)
    with pytest.raises(DomainError, match="later movement"):
        command(state, "undo", {"event_id": earlier_id}, 91000)
    assert state == before
    state = command(state, "undo", {}, 91000)
    state = command(state, "undo", {"event_id": earlier_id}, 91000)
    assert stats(state, "h1", 91000)["total_toi_ms"] == 90000


def test_setup_can_repair_initial_lineup_before_playing():
    state = command(game_state(), "setup", {"players": [{"id": "h1", "location": "BENCH"}, {"id": "h7", "location": "ON_ICE"}]})
    state = command(state, "clock", {"operation": "start"})
    assert stats(state, "h7", 61000)["total_toi_ms"] == 60000
    assert stats(state, "h1", 61000)["total_toi_ms"] == 0
    with pytest.raises(DomainError, match="player movement"):
        command(state, "setup", {"players": [{"id": "h7", "location": "BENCH"}]}, 61000)


def test_setup_rejects_duplicate_jerseys_and_unavailable_on_ice():
    with pytest.raises(DomainError, match="distinct jersey"):
        command(game_state(), "setup", {"players": [{"id": "h7", "number": 1}]})
    with pytest.raises(DomainError, match="bench"):
        command(game_state(), "setup", {"players": [{"id": "h1", "eligible": False}]})


def test_setup_imports_new_players_with_stable_ids_before_tracking():
    imported = {"id": "external-new-player", "team_id": "home", "name": "New signing", "number": 88, "position": "LW", "eligible": True, "location": "BENCH", "external_id": "upstream-123"}
    state = command(game_state(), "setup", {"players": [imported]})
    assert stats(state, imported["id"])["name"] == "New signing"
    state = command(state, "clock", {"operation": "start"})
    state = substitute(state, ["h2"], [imported["id"]], 31000)
    assert stats(state, imported["id"], 61000)["total_toi_ms"] == 30000


@pytest.mark.parametrize("invalid", [
    {"id": "new"},
    {"id": "new", "team_id": "nonexistent", "name": "New", "number": 88, "position": "LW", "eligible": True, "location": "BENCH"},
    {"id": "new", "team_id": "home", "name": "New", "number": 88, "position": "INVALID", "eligible": True, "location": "BENCH"},
    {"id": "new", "team_id": "home", "name": "New", "number": 88, "position": "LW", "eligible": False, "location": "ON_ICE"},
    {"id": "h1", "team_id": "away"},
])
def test_setup_rejects_invalid_roster_imports(invalid):
    state = game_state()
    before = deepcopy(state)
    with pytest.raises(DomainError):
        command(state, "setup", {"players": [invalid]})
    assert state == before


def test_setup_rejects_new_players_after_tracking_starts():
    imported = {"id": "new", "team_id": "home", "name": "New", "number": 88, "position": "LW", "eligible": True, "location": "BENCH"}
    with pytest.raises(DomainError, match="before tracking"):
        command(started(), "setup", {"players": [imported]}, 61000)


def test_undo_cannot_restore_a_player_marked_unavailable():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    state = command(state, "setup", {"players": [{"id": "h1", "eligible": False}]}, 61000)
    with pytest.raises(DomainError, match="unavailable player"):
        command(state, "undo", {}, 61000)


def test_historical_correction_may_use_currently_unavailable_player_only_if_they_finish_on_bench():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    old_id = state["events"][-1]["id"]
    state = substitute(state, ["h7"], ["h1"], 61000)
    state = command(state, "setup", {"players": [{"id": "h7", "eligible": False}]}, 91000)
    state = command(state, "correct", {"event_id": old_id, "replacement": {"type": "substitute", "payload": {"team_id": "home", "outgoing_ids": ["h1"], "incoming_ids": ["h7"]}}}, 91000)
    assert stats(state, "h7", 91000)["total_toi_ms"] == 30000
    assert not stats(state, "h7", 91000)["eligible"]


def test_stale_and_future_selection_timestamps_rejected():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    with pytest.raises(DomainError, match="after this selection"):
        substitute(state, ["h2"], ["h8"], 61000, at_playing_ms=20000)
    with pytest.raises(DomainError, match="outside"):
        substitute(state, ["h2"], ["h8"], 61000, at_playing_ms=70000)


def test_idempotency_and_optimistic_concurrency():
    initial = started()
    action = {"request_id": "retry-test", "expected_version": initial["version"], "type": "substitute", "payload": {"team_id": "home", "outgoing_ids": ["h1"], "incoming_ids": ["h7"]}}
    state = apply_action(initial, action, 61000)
    retried = apply_action(state, action, 61000)
    assert retried == state
    with pytest.raises(DomainError) as error:
        apply_action(state, {**action, "request_id": "another"}, 61000)
    assert error.value.status_code == 409


@pytest.mark.parametrize("payload", [
    {"operation": "set", "remaining_ms": -1},
    {"operation": "set", "remaining_ms": 1200001},
    {"operation": "set", "period": 0},
    {"operation": "set", "period_lengths_ms": []},
    {"operation": "set", "period_lengths_ms": [True]},
])
def test_invalid_clock_configuration(payload):
    with pytest.raises(DomainError):
        command(game_state(), "clock", payload)


@pytest.mark.parametrize("kind,payload", [
    ("unknown", {}), ("move", []), ("move", {"player_id": [], "to": "BENCH"}),
    ("move", {"player_id": "h1", "to": []}), ("substitute", {"team_id": []}),
    ("clock", {"operation": []}), ("setup", {"players": [None]}),
    ("setup", {"players": [{"id": []}]}), ("setup", {"lines": [None]}),
    ("setup", {"game": None}),
])
def test_malformed_payloads_produce_domain_errors(kind, payload):
    with pytest.raises(DomainError):
        command(game_state(), kind, payload)


def test_realistic_two_period_scenario_has_independently_calculated_toi():
    state = game_state()
    state["game"]["period_lengths_ms"] = [120000, 120000, 120000]
    state["clock"]["remaining_ms"] = 120000
    state = command(state, "clock", {"operation": "start"})
    state = substitute(state, ["h1", "h2", "h3"], ["h7", "h8", "h9"], 31000)
    state = command(state, "clock", {"operation": "pause"}, 41000)
    state = command(state, "clock", {"operation": "start"}, 101000)
    state = command(state, "move", {"player_id": "h7", "to": "PENALTY_BOX"}, 121000)
    state = command(state, "move", {"player_id": "h7", "to": "ON_ICE"}, 151000)
    state = substitute(state, ["h6"], ["h13"], 171000)
    wrong_id = state["events"][-1]["id"]
    state = command(state, "correct", {"event_id": wrong_id, "replacement": {"type": "substitute", "payload": {"team_id": "home", "outgoing_ids": ["h6"], "incoming_ids": ["h1"]}}}, 176000)
    state = command(state, "clock", {"operation": "advance"}, 181000)
    state = command(state, "clock", {"operation": "start"}, 201000)
    state = substitute(state, ["h1"], ["h6"], 221000)
    state = command(state, "clock", {"operation": "pause"}, 241000)
    public = snapshot(state, 999000)
    expected = {"h1": 60000, "h2": 30000, "h3": 30000, "h4": 160000, "h5": 160000, "h6": 130000, "h7": 100000, "h8": 130000, "h9": 130000, "h13": 0}
    assert public["clock"]["playing_ms"] == 160000
    for pid, total in expected.items():
        player = next(p for p in public["players"] if p["id"] == pid)
        assert player["total_toi_ms"] == total, pid
        assert sum(player["toi_by_period"].values()) == total, pid
    assert stats(state, "h6", 999000)["toi_by_period"] == {"1": 110000, "2": 20000, "3": 0}
