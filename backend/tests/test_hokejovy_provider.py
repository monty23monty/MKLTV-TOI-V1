"""Recorded source fixture plus mocked HTTP only; ordinary tests never use DNS."""

from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from toi.integrations.hokejovy_zapis import (
    HokejovyZapisProvider, ProviderConfig, ProviderError, normalize_game, parse_roster_code,
)
from toi.integrations.hokejovy_zapis.client import MAX_BODY_BYTES


@pytest.fixture
def source():
    return json.loads((Path(__file__).parent / "fixtures" / "hokejovy_966.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("code,role,line,slot", [
    ("101", "GK", None, None), ("102", "GK", None, None),
    ("211", "LD", 1, 1), ("212", "RD", 1, 2), ("221", "LD", 2, 1),
    ("232", "RD", 3, 2), ("311", "LW", 1, 1), ("312", "C", 1, 2),
    ("313", "RW", 1, 3), ("321", "LW", 2, 1), ("322", "C", 2, 2),
    ("323", "RW", 2, 3), ("343", "RW", 4, 3), ("393", "RW", 9, 3),
])
def test_confirmed_roster_code_specification(code, role, line, slot):
    parsed = parse_roster_code(code)
    assert parsed["valid"]
    assert parsed["raw_code"] == code
    assert parsed["role"] == role
    assert parsed["line_number"] == line
    assert parsed["line_position"] == slot
    assert parsed["is_goalkeeper"] == (role == "GK")
    assert parsed["is_starting_goalkeeper_slot"] == (code == "101")


@pytest.mark.parametrize("value", [None, "", "31", "0312", "000", "301", "203", "213", "103", "111", "999", " 312", "３１２", "312 ", True, [], {}])
def test_unfamiliar_codes_are_not_inferred(value):
    parsed = parse_roster_code(value)
    assert not parsed["valid"]
    assert parsed["role"] is None
    assert parsed["line_number"] is None
    assert parsed["raw_code"] == (str(value) if value is not None else "")


def test_live_recording_has_complete_real_teams_rosters_and_names(source):
    result = normalize_game(source, "966", 12345)
    assert result["game"]["name"] == "Milton Keynes Lightning vs Solway IHC"
    assert result["game"]["external_status"] == "end"
    assert result["game"]["results"]["score"]["final"] == {"score_home": 5, "score_guest": 4}
    assert result["teams"][0]["external_id"] == "5"
    assert result["teams"][1]["external_id"] == "37"
    assert result["teams"][1]["side"] == "away"
    assert result["fetched_at_ms"] == 12345
    assert len(result["players"]) == 36
    assert sum(p["team_id"] == "hz-team-5" for p in result["players"]) == 19
    assert sum(p["team_id"] == "hz-team-37" for p in result["players"]) == 17
    hedley = next(p for p in result["players"] if p["name"] == "Jordan Hedley")
    assert hedley["id"] == "hz-966-roster-48611"
    assert hedley["person_id"] == "hz-player-90"
    assert hedley["external_id"] == "90"
    assert hedley["match_player_id"] == "48611"
    assert hedley["number"] == 1
    assert hedley["position"] == "G"
    assert hedley["assigned_role"] == "GK"
    assert hedley["location"] == "ON_ICE"
    assert hedley["source"]["raw_position"] == "GK"


def test_supplied_nested_roster_shape_is_also_supported(source):
    live = normalize_game(source, "966", 1000)
    source["gameInfo"]["roster"] = source.pop("roster")
    supplied = normalize_game(source, "966", 1000)
    assert supplied == live


def test_supplied_shortened_basingstoke_example_retains_its_own_identity():
    supplied = {
        "gameInfo": {
            "status": "end", "stadium": "Planet Ice, 1 S Row, Elder Gate, Milton Keynes, MK9 1DL",
            "teamInfo": {"home": {"id": 5, "name": "Milton Keynes Lightning", "shortcut": "MKL"}, "visitor": {"id": 19, "name": "Basingstoke Bison", "shortcut": "BAS"}},
            "roster": {
                "home": {
                    "101": {"playerId": 90, "matchPlayerId": 26727, "name": "Jordan", "surname": "Hedley", "jersey": 1, "position": "GK", "startingSix": True, "roster": "101"},
                    "312": {"playerId": 49, "matchPlayerId": 26744, "name": "Jordan", "surname": "Cownie", "jersey": 95, "position": "FW", "startingSix": True, "roster": "312"},
                },
                "visitor": {"101": {"playerId": 235, "matchPlayerId": 26761, "name": "Max", "surname": "Wright", "jersey": 42, "position": "GK", "startingSix": True, "roster": "101"}},
            },
        },
    }
    result = normalize_game(supplied, "966", 0)
    assert result["teams"][1]["id"] == "hz-team-19"
    assert result["game"]["name"] == "Milton Keynes Lightning vs Basingstoke Bison"
    assert {p["external_player_id"] for p in result["players"]} == {"90", "49", "235"}
    assert next(p for p in result["players"] if p["external_player_id"] == "49")["position"] == "C"
    assert len(result["players"]) == 3
    assert result["warnings"]  # abbreviated example has incomplete starting sixes


def test_conflicting_nested_and_top_level_rosters_require_reconciliation(source):
    source["gameInfo"]["roster"] = deepcopy(source["roster"])
    source["gameInfo"]["roster"]["home"]["101"]["jersey"] = 91
    with pytest.raises(ProviderError) as error:
        normalize_game(source, "966", 0)
    assert error.value.code == "roster_schema_conflict"


def test_namespaced_person_identity_is_stable_across_matches(source):
    one = normalize_game(source, "966", 0)["players"]
    two = normalize_game(source, "967", 0)["players"]
    by_person = {p["person_id"]: p for p in two}
    for player in one:
        assert player["person_id"] in by_person
        assert player["id"] != by_person[player["person_id"]]["id"]
        assert player["number"] == by_person[player["person_id"]]["number"]


def test_starters_follow_starting_six_even_if_from_different_lines(source):
    source["roster"]["home"]["311"]["startingSix"] = False
    source["roster"]["home"]["321"]["startingSix"] = True
    result = normalize_game(source, "966", 0)
    starters = [p for p in result["players"] if p["team_id"] == "hz-team-5" and p["location"] == "ON_ICE"]
    assert len(starters) == 6
    assert any(p["source"]["roster_code"] == "321" for p in starters)
    assert not any(p["source"]["roster_code"] == "311" for p in starters)
    assert not any(p["location"] == "PENALTY_BOX" for p in result["players"])


def test_goalkeeper_slot_disagreement_warns_but_starting_flag_wins(source):
    source["roster"]["home"]["101"]["startingSix"] = False
    source["roster"]["home"]["102"]["startingSix"] = True
    result = normalize_game(source, "966", 0)
    keeper = next(p for p in result["players"] if p["team_id"] == "hz-team-5" and p["position"] == "G" and p["location"] == "ON_ICE")
    assert keeper["source"]["roster_code"] == "102"
    assert any("disagrees with startingSix" in warning for warning in result["warnings"])


def test_forward_and_defence_slots_are_explicit_and_allow_partial_groups(source):
    result = normalize_game(source, "966", 0)
    forwards = next(line for line in result["lines"] if line["id"] == "hz-966-home-forward-1")
    assert set(forwards["slots"]) == {"LW", "C", "RW"}
    by_id = {p["id"]: p for p in result["players"]}
    assert by_id[forwards["slots"]["C"]]["name"] == "David Thomson"
    assert forwards["kind"] == "FORWARD"
    pair = next(line for line in result["lines"] if line["id"] == "hz-966-away-defence-3")
    assert pair["slots"]["LD"] is not None
    assert pair["slots"]["RD"] is None
    assert len(pair["player_ids"]) == 1
    assert any("empty lineup slots" in warning for warning in result["warnings"])


def test_roster_order_does_not_change_normalized_output(source):
    first = normalize_game(source, "966", 0)
    for side in source["roster"]:
        source["roster"][side] = dict(reversed(list(source["roster"][side].items())))
    second = normalize_game(source, "966", 0)
    assert second["players"] == first["players"]
    assert second["lines"] == first["lines"]


@pytest.mark.parametrize("raw_position", ["DE", "DF"])
def test_defensive_general_position_variants_recognized(source, raw_position):
    source["roster"]["home"]["211"]["position"] = raw_position
    result = normalize_game(source, "966", 0)
    player = next(p for p in result["players"] if p["id"] == "hz-966-roster-48624")
    assert player["position"] == "D" and player["assigned_role"] == "LD"
    assert not any("conflicts with roster code 211" in warning for warning in result["warnings"])


def test_conflicting_raw_position_is_preserved_and_valid_code_wins(source):
    source["roster"]["home"]["311"]["position"] = "DE"
    result = normalize_game(source, "966", 0)
    player = next(p for p in result["players"] if p["id"] == "hz-966-roster-48633")
    assert player["position"] == "LW" and player["eligible"]
    assert player["source"]["raw_position"] == "DE"
    assert any("conflicts with roster code 311" in warning for warning in result["warnings"])


def test_unknown_forward_code_requires_position_review_without_losing_player(source):
    player = source["roster"]["home"].pop("311")
    player["roster"] = "399"
    source["roster"]["home"]["399"] = player
    result = normalize_game(source, "966", 0)
    imported = next(p for p in result["players"] if p["id"] == "hz-966-roster-48633")
    assert imported["eligible"] is False and imported["location"] == "BENCH"
    assert imported["source"]["parsed_role"] is None
    assert imported["assigned_role"] is None
    assert imported["source"]["position_unresolved"] is True
    assert any("position_unresolved" in warning for warning in result["warnings"])


def test_unknown_defence_code_can_retain_safe_general_role(source):
    source["roster"]["home"]["211"]["roster"] = "299"
    result = normalize_game(source, "966", 0)
    player = next(p for p in result["players"] if p["id"] == "hz-966-roster-48624")
    assert player["position"] == "D" and player["eligible"]
    assert player["assigned_role"] == "D"
    assert player["source"]["parsed_role"] is None


def test_malformed_player_with_safe_identity_is_retained_for_review(source):
    raw = source["roster"]["home"]["101"]
    raw["name"] = None
    raw["jersey"] = -5
    result = normalize_game(source, "966", 0)
    player = next(p for p in result["players"] if p["match_player_id"] == "48611")
    assert player["eligible"] is False and player["location"] == "BENCH"
    assert player["number"] == 0
    assert player["source"]["original_jersey"] == -5
    assert len(result["players"]) == 36


def test_duplicate_jerseys_are_flagged_and_excluded_until_operator_review(source):
    source["roster"]["home"]["102"]["jersey"] = 1
    result = normalize_game(source, "966", 0)
    duplicates = [p for p in result["players"] if p["team_id"] == "hz-team-5" and p["number"] == 1]
    assert len(duplicates) == 2
    assert all(not p["eligible"] and p["location"] == "BENCH" for p in duplicates)
    assert any("duplicate jersey" in warning for warning in result["warnings"])


def test_missing_starting_flag_never_invents_on_ice_state(source):
    del source["roster"]["home"]["101"]["startingSix"]
    result = normalize_game(source, "966", 0)
    player = next(p for p in result["players"] if p["match_player_id"] == "48611")
    assert player["location"] == "BENCH"
    assert any("missing or invalid startingSix" in warning for warning in result["warnings"])
    assert any("starting lineup has 5 skaters and 0 goalkeepers" in warning for warning in result["warnings"])


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p.pop("gameInfo"), "invalid_schema"),
    (lambda p: p["gameInfo"].pop("teamInfo"), "invalid_schema"),
    (lambda p: p.pop("roster"), "invalid_schema"),
    (lambda p: p["roster"].update(home={}), "invalid_schema"),
    (lambda p: p["roster"]["home"]["101"].pop("playerId"), "invalid_identity"),
    (lambda p: p["roster"]["home"]["102"].update(playerId=90), "duplicate_identity"),
    (lambda p: p["roster"]["visitor"]["101"].update(matchPlayerId=48611), "duplicate_identity"),
    (lambda p: p["roster"]["home"]["102"].update(roster="101"), "duplicate_roster_slot"),
    (lambda p: p["gameInfo"]["teamInfo"]["visitor"].update(id=5), "duplicate_identity"),
    (lambda p: p["roster"]["home"].update({"101": None}), "invalid_identity"),
])
def test_unrecoverable_schema_and_identity_fail_before_partial_import(source, mutation, code):
    mutation(source)
    with pytest.raises(ProviderError) as error:
        normalize_game(source, "966", 0)
    assert error.value.code == code


@pytest.mark.parametrize("game_id", ["0", "-1", "966/../../", "https://evil.test", "966?x=1", "1.0", "", True, "９６６"])
def test_invalid_game_ids_never_reach_transport(game_id):
    calls = []
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(lambda request: calls.append(request)))
    with pytest.raises(ProviderError) as error:
        provider.preview(game_id)
    assert error.value.code == "invalid_identity" and not calls


@pytest.mark.parametrize("base_url,prefix", [
    ("http://api.hokejovyzapis.cz", "/v1/key"),
    ("https://evil.test", "/v1/key"),
    ("https://user:pass@api.hokejovyzapis.cz", "/v1/key"),
    ("https://api.hokejovyzapis.cz:8443", "/v1/key"),
    ("https://api.hokejovyzapis.cz/path", "/v1/key"),
    ("https://api.hokejovyzapis.cz?url=evil", "/v1/key"),
    ("https://api.hokejovyzapis.cz", "/v1/../key"),
    ("https://api.hokejovyzapis.cz", "/v1/key?x=1"),
])
def test_provider_configuration_rejects_arbitrary_origins_and_path_traversal(base_url, prefix):
    with pytest.raises(ProviderError) as error:
        ProviderConfig(base_url=base_url, api_prefix=prefix)
    assert error.value.code == "invalid_configuration"


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "source.local", "source.internal"])
def test_even_explicit_allowlist_cannot_target_local_ips_or_local_domains(host):
    with pytest.raises(ProviderError) as error:
        ProviderConfig(base_url=f"https://{host}", allowed_hosts=(host,))
    assert error.value.code == "invalid_configuration"


def test_mocked_client_constructs_fixed_endpoint_and_cache_returns_copies(source):
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://api.hokejovyzapis.cz/v1/d07ssvctxm/matches/966/game-info.json"
        return httpx.Response(200, json=source)
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(handler), now_ms=lambda: 1000)
    first = provider.preview("0966")
    assert first["cache"]["hit"] is False
    first["players"][0]["name"] = "Mutation"
    second = provider.preview("966")
    assert second["cache"]["hit"] is True
    assert second["players"][0]["name"] != "Mutation"
    assert len(requests) == 1
    assert provider.preview("966", force=True)["cache"]["hit"] is False
    assert len(requests) == 2


@pytest.mark.parametrize("status,expected_code,attempts", [(404, "game_not_found", 1), (429, "rate_limited", 3), (503, "upstream_error", 3), (401, "upstream_rejected", 1), (302, "redirect_blocked", 1)])
def test_http_failures_have_bounded_retries(status, expected_code, attempts):
    calls, sleeps = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/"})
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(handler), sleep=sleeps.append)
    with pytest.raises(ProviderError) as error:
        provider.preview("966")
    assert error.value.code == expected_code
    assert len(calls) == attempts
    assert len(sleeps) == attempts - 1


def test_timeout_retries_then_recovers_without_external_network(source):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ReadTimeout("simulated", request=request)
        return httpx.Response(200, json=source)
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(handler), sleep=lambda _: None)
    assert provider.preview("966")["external_game_id"] == "966"
    assert len(calls) == 3


@pytest.mark.parametrize("body,code", [(b"<html>Unavailable</html>", "malformed_json"), (b'{"gameInfo":{},"gameInfo":{}}', "duplicate_json_key"), (b'{"roster":{"home":{"101":{},"101":{}}}}', "duplicate_json_key"), (b'{"gameInfo":{"results":{"score":NaN}}}', "malformed_json"), (b'{"gameInfo":{"results":{"score":Infinity}}}', "malformed_json")])
def test_invalid_json_and_duplicate_json_slots_are_explicit(body, code):
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))
    with pytest.raises(ProviderError) as error:
        provider.preview("966")
    assert error.value.code == code


def test_response_size_is_bounded_even_without_content_length():
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * (MAX_BODY_BYTES // 2)
            yield b"x" * (MAX_BODY_BYTES // 2 + 1)
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Body())))
    with pytest.raises(ProviderError) as error:
        provider.preview("966")
    assert error.value.code == "response_too_large"


def test_expired_cache_fallback_is_explicit_but_forced_failure_is_not_success(source):
    now = [1000]
    fail = [False]
    def handler(request):
        if fail[0]:
            raise httpx.ConnectError("simulated offline", request=request)
        return httpx.Response(200, json=source)
    provider = HokejovyZapisProvider(transport=httpx.MockTransport(handler), now_ms=lambda: now[0], sleep=lambda _: None)
    provider.preview("966")
    now[0] = 62000
    fail[0] = True
    fallback = provider.preview("966")
    assert fallback["cache"]["stale"] is True
    assert fallback["cache_error"]["code"] == "source_unavailable"
    assert any("stale cached preview" in warning for warning in fallback["warnings"])
    with pytest.raises(ProviderError) as error:
        provider.preview("966", force=True)
    assert error.value.code == "source_unavailable"
    assert error.value.cached_preview["cache"]["stale"] is True
    assert error.value.as_dict()["cached_preview"]["external_game_id"] == "966"


def test_configured_prefix_timeout_and_ttl_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("HOKEJOVY_ZAPIS_API_PREFIX", "/v2/new-key")
    monkeypatch.setenv("HOKEJOVY_ZAPIS_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("HOKEJOVY_ZAPIS_CACHE_TTL_SECONDS", "15")
    config = ProviderConfig.from_env()
    assert config.endpoint("966").endswith("/v2/new-key/matches/966/game-info.json")
    assert config.timeout_seconds == 4 and config.cache_ttl_seconds == 15


def test_invalid_numeric_configuration_is_structured(monkeypatch):
    monkeypatch.setenv("HOKEJOVY_ZAPIS_TIMEOUT_SECONDS", "nonsense")
    with pytest.raises(ProviderError) as error:
        ProviderConfig.from_env()
    assert error.value.status_code == 503
