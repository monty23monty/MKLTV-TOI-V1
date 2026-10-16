from toi.domain import snapshot
from toi.recommendations import recommend

from test_domain import command, game_state, started, substitute


def ranked(state, outgoing, now=1000, **kwargs):
    return recommend(snapshot(state, now), "home", outgoing, **kwargs)


def by_id(result, player_id):
    return next(p for p in result["players"] if p["player_id"] == player_id)


def test_cold_start_same_position_ranking_is_deterministic():
    state = game_state()
    first = ranked(state, ["h1"])
    assert first == ranked(state, ["h1"])
    assert first["players"][0]["player_id"] == "h7"
    assert by_id(first, "h7")["score"] > by_id(first, "h8")["score"] > by_id(first, "h10")["score"]
    assert by_id(first, "h7")["confidence"] == "cold_start"
    assert "Same position" in by_id(first, "h7")["reasons"]


def test_repeated_confirmed_replacements_learn_with_explanations():
    state = started()
    for now in (31000, 121000, 211000):
        state = substitute(state, ["h1"], ["h13"], now)
        state = substitute(state, ["h13"], ["h1"], now + 30000)
    result = ranked(state, ["h1"], 301000)
    assert result["players"][0]["player_id"] == "h13"
    suggestion = by_id(result, "h13")
    assert suggestion["observations"] == 3
    assert suggestion["confidence"] == "developing"
    assert any("Frequent replacement" in reason for reason in suggestion["reasons"])


def test_eligibility_excludes_ice_penalty_box_unavailable_and_other_team():
    state = command(game_state(), "move", {"player_id": "h7", "to": "PENALTY_BOX"})
    ids = {p["player_id"] for p in ranked(state, ["h1"])["players"]}
    assert "h7" not in ids
    assert "h18" not in ids
    assert not ids & {f"h{i}" for i in range(1, 7)}
    assert not any(pid.startswith("a") for pid in ids)
    assert ids == {p["id"] for p in state["players"] if p["team_id"] == "home" and p["eligible"] and p["location"] == "BENCH"}


def test_just_left_player_penalized_and_rest_uses_playing_time():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    state = substitute(state, ["h7"], ["h1"], 61000)
    immediate = ranked(state, ["h1"], 61000)
    state = command(state, "clock", {"operation": "pause"}, 61000)
    stopped = ranked(state, ["h1"], 661000)
    assert by_id(immediate, "h7")["rest_ms"] == by_id(stopped, "h7")["rest_ms"] == 0
    assert "Just left the ice" in by_id(stopped, "h7")["reasons"]
    state = command(state, "clock", {"operation": "start"}, 661000)
    rested = ranked(state, ["h1"], 721000)
    assert by_id(rested, "h7")["rest_ms"] == 60000
    assert by_id(rested, "h7")["score"] > by_id(stopped, "h7")["score"]


def test_configured_forward_line_and_defensive_pair_group_suggestions():
    state = game_state()
    forwards = ranked(state, ["h1", "h2", "h3"])
    assert set(forwards["groups"][0]["player_ids"]) == {"h7", "h8", "h9"}
    assert "Configured line" in forwards["groups"][0]["reasons"]
    defenders = ranked(state, ["h4", "h5"])
    assert set(defenders["groups"][0]["player_ids"]) == {"h10", "h11"}


def test_arbitrary_mixed_changes_receive_complete_compatible_groups():
    result = ranked(game_state(), ["h1", "h4"])
    assert result["groups"]
    assert all(len(g["player_ids"]) == 2 for g in result["groups"])
    assert any(set(g["player_ids"]) == {"h7", "h10"} for g in result["groups"])


def test_mixed_change_prefers_a_forward_and_defender_over_configured_defensive_pair():
    state = game_state()
    for player in state["players"]:
        if player["team_id"] == "home" and player["location"] == "BENCH" and player["id"] not in {"h7", "h10", "h11"}:
            player["eligible"] = False
    result = ranked(state, ["h1", "h4"])
    players = {p["id"]: p for p in state["players"]}
    top_roles = [players[pid]["position"] for pid in result["groups"][0]["player_ids"]]
    assert sum(role in {"LW", "C", "RW"} for role in top_roles) == 1
    assert top_roles.count("D") == 1
    pair = next(group for group in result["groups"] if set(group["player_ids"]) == {"h10", "h11"})
    assert pair["eligible"] is True
    assert "Different position mix" in pair["reasons"]
    assert pair["score"] < result["groups"][0]["score"]


def test_group_learning_and_observed_combinations_adapt_without_presets():
    state = started()
    for now in (31000, 121000):
        state = substitute(state, ["h1", "h2", "h3"], ["h13", "h14", "h15"], now)
        state = substitute(state, ["h13", "h14", "h15"], ["h1", "h2", "h3"], now + 30000)
    result = ranked(state, ["h1", "h2", "h3"], 211000)
    assert set(result["groups"][0]["player_ids"]) == {"h13", "h14", "h15"}
    assert any("Entered together 2" in reason for reason in result["groups"][0]["reasons"])
    observed = next(c for c in result["combinations"] if set(c["player_ids"]) == {"h13", "h14", "h15"})
    assert observed["observations"] == 2
    assert observed["eligible"] is True


def test_regular_defensive_partner_influences_individual_ranking():
    state = substitute(started(), ["h4"], ["h10"], 31000)
    result = ranked(state, ["h5"], 61000)
    assert result["players"][0]["player_id"] == "h11"
    assert "Typical defensive partner" in by_id(result, "h11")["reasons"]


def test_undo_removes_direct_learning_and_observed_combinations():
    state = substitute(started(), ["h1", "h2", "h3"], ["h13", "h14", "h15"], 31000)
    state = command(state, "undo", {}, 61000)
    result = ranked(state, ["h1", "h2", "h3"], 91000)
    assert by_id(result, "h13")["observations"] == 0
    assert not any(set(c["player_ids"]) == {"h13", "h14", "h15"} for c in result["combinations"])
    assert not any("Entered together" in reason for g in result["groups"] for reason in g["reasons"])


def test_historical_correction_removes_wrong_learning_and_adds_correct_relationship():
    state = substitute(started(), ["h1"], ["h7"], 31000)
    old_id = state["events"][-1]["id"]
    state = command(state, "correct", {"event_id": old_id, "replacement": {"type": "substitute", "payload": {"team_id": "home", "outgoing_ids": ["h1"], "incoming_ids": ["h13"]}}}, 61000)
    state = substitute(state, ["h13"], ["h1"], 91000)
    result = ranked(state, ["h1"], 181000)
    assert by_id(result, "h7")["observations"] == 0
    assert by_id(result, "h13")["observations"] == 1


def test_configurable_weights_are_ranking_points_not_probabilities():
    result = ranked(game_state(), ["h1"], weights={"same_position": 200.0})
    assert by_id(result, "h7")["score"] == 200
    assert "probability" not in by_id(result, "h7")


def test_goalkeeper_suggestions_keep_goalkeepers_explicit():
    result = ranked(game_state(), ["h6"])
    assert result["players"][0]["player_id"] == "h12"
    assert by_id(result, "h7")["eligible"] is True  # extra attackers remain available


def test_imported_left_and_right_defence_assignments_refine_cold_start():
    state = game_state()
    for player in state["players"]:
        if player["id"] in {"h4", "h11"}:
            player["assigned_role"] = "LD"
        elif player["id"] in {"h5", "h10"}:
            player["assigned_role"] = "RD"
    left = ranked(state, ["h4"])
    right = ranked(state, ["h5"])
    assert by_id(left, "h11")["score"] > by_id(left, "h10")["score"]
    assert by_id(right, "h10")["score"] > by_id(right, "h11")["score"]
    assert "Same assigned defensive side" in by_id(left, "h11")["reasons"]


def test_learned_replacements_can_override_official_defensive_side():
    state = game_state()
    for player in state["players"]:
        if player["id"] in {"h4", "h11"}:
            player["assigned_role"] = "LD"
        elif player["id"] == "h10":
            player["assigned_role"] = "RD"
    state = command(state, "clock", {"operation": "start"})
    for at in (31000, 121000, 211000):
        state = substitute(state, ["h4"], ["h10"], at)
        state = substitute(state, ["h10"], ["h4"], at + 30000)
    result = ranked(state, ["h4"], 301000)
    assert by_id(result, "h10")["score"] > by_id(result, "h11")["score"]
