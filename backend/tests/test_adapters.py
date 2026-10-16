import json

import pytest

from toi.adapters import DemoRosterProvider, ManualClockProvider, resolve_principal
from toi.seed import create_demo_state


def test_demo_has_independent_identifiers_and_realistic_complete_rosters():
    game = create_demo_state(now_ms=123)
    assert len(game["players"]) == 40
    assert len({p["id"] for p in game["players"]}) == 40
    for team in game["teams"]:
        players = [p for p in game["players"] if p["team_id"] == team["id"]]
        assert len(players) == 20
        assert len([p for p in players if p["position"] == "G"]) == 2
        assert len([p for p in players if p["position"] == "D"]) == 6
        assert len([p for p in players if p["location"] == "ON_ICE"]) == 6
        assert len({p["number"] for p in players}) == 20
        lines = [line for line in game["lines"] if line["team_id"] == team["id"]]
        assert len(lines) == 7
        assert all(set(line["player_ids"]) <= {p["id"] for p in players} for line in lines)
    other = create_demo_state("other", now_ms=123)
    assert not {p["id"] for p in game["players"]} & {p["id"] for p in other["players"]}
    assert game["clock"]["anchor_unix_ms"] == 123
    assert game["shifts"] == []  # Reconstructed by the domain, never duplicated here.


def test_demo_provider_returns_isolated_games():
    provider = DemoRosterProvider()
    first = provider.load_game("match")
    first["players"][0]["name"] = "Changed"
    assert provider.load_game("match")["players"][0]["name"] != "Changed"
    assert ManualClockProvider().read("match") is None


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    monkeypatch.delenv("TOI_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TOI_OPERATOR_TOKENS", raising=False)


def test_auth_is_explicitly_open_only_without_configuration():
    assert resolve_principal(None).actor == "local-operator"


def test_bearer_auth_rejects_missing_or_wrong_credentials(monkeypatch):
    monkeypatch.setenv("TOI_AUTH_TOKEN", "test-token")
    for header in (None, "Basic test-token", "Bearer wrong"):
        with pytest.raises(PermissionError):
            resolve_principal(header)
    principal = resolve_principal("Bearer test-token")
    assert principal.team_ids is None
    assert principal.can_control_clock


def test_per_team_operator_permissions_are_preserved(monkeypatch):
    monkeypatch.setenv("TOI_OPERATOR_TOKENS", json.dumps({
        "team-token": {"actor": "home-operator", "team_ids": ["demo-home"], "can_control_clock": False},
    }))
    principal = resolve_principal("Bearer team-token")
    assert principal.actor == "home-operator"
    assert principal.team_ids == {"demo-home"}
    assert not principal.can_control_clock


def test_bad_auth_configuration_never_opens_access(monkeypatch):
    monkeypatch.setenv("TOI_OPERATOR_TOKENS", '["not a mapping"]')
    with pytest.raises(ValueError):
        resolve_principal("Bearer anything")


def test_malformed_permission_types_are_rejected(monkeypatch):
    monkeypatch.setenv("TOI_OPERATOR_TOKENS", json.dumps({
        "token": {"actor": "operator", "team_ids": "all", "can_control_clock": True},
    }))
    with pytest.raises(ValueError):
        resolve_principal("Bearer token")
