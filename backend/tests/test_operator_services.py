"""Operator clock handover and build metadata for the released v2 application."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from toi.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TOI_STREAM_ENABLED", "true")
    monkeypatch.setenv("TOI_AUTH_TOKEN", "supervisor")
    monkeypatch.setenv("TOI_OPERATOR_TOKENS", json.dumps({"team-only": {
        "actor": "team-operator", "team_ids": ["demo-home"], "can_control_clock": False}}))
    monkeypatch.setenv("TOI_SERVICE_TOKENS", json.dumps({"clock-token": {
        "service_id": "clock-bridge", "game_ids": ["demo"],
        "permissions": ["stream:read", "journal:read", "clock:write"],
        "clock_sources": ["scoreboard"]}}))
    with TestClient(create_app(f"sqlite:///{tmp_path / 'operator-service.db'}")) as test_client:
        yield test_client


SUPERVISOR = {"Authorization": "Bearer supervisor"}


def test_operator_can_handover_and_manual_clock_fences_external_owner(client):
    response = client.get("/api/v1/games/demo/clock/authority", headers=SUPERVISOR)
    assert response.status_code == 200
    assert response.json()["sources"] == [{"source": "scoreboard", "service_id": "clock-bridge"}]
    body = {"request_id": str(uuid4()), "expected_version": response.json()["version"],
            "mode": "external", "source": "scoreboard", "service_id": "clock-bridge"}
    changed = client.post("/api/v1/games/demo/clock/authority", headers=SUPERVISOR, json=body)
    assert changed.status_code == 200
    authority = changed.json()["clock_authority"]
    assert authority["mode"] == "external" and authority["generation"] > 0
    retry = client.post("/api/v1/games/demo/clock/authority", headers=SUPERVISOR, json=body)
    assert retry.json()["version"] == changed.json()["version"]
    paused = client.post("/api/v1/games/demo/actions", headers=SUPERVISOR, json={
        "request_id": str(uuid4()), "expected_version": changed.json()["version"],
        "type": "clock", "payload": {"operation": "pause"}})
    assert paused.status_code == 200
    assert paused.json()["clock_authority"]["mode"] == "manual"
    assert paused.json()["clock_authority"]["generation"] > authority["generation"]


def test_clock_sources_do_not_expose_tokens_or_allow_team_operator(client):
    denied = client.get("/api/v1/games/demo/clock/authority", headers={"Authorization": "Bearer team-only"})
    assert denied.status_code == 403
    sources = client.get("/api/v1/games/demo/clock/authority", headers=SUPERVISOR)
    assert "clock-token" not in sources.text
    body = {"request_id": str(uuid4()), "expected_version": 0,
            "mode": "external", "source": "unconfigured", "service_id": "clock-bridge"}
    assert client.post("/api/v1/games/demo/clock/authority", headers=SUPERVISOR, json=body).status_code == 422
    assert client.get("/api/v1/games/demo/state", headers=SUPERVISOR).json()["version"] == 0


def test_health_exposes_version_and_readiness_separates_clock_services(client):
    health = client.get("/health").json()
    assert health["version"] == "2.0.1" and health["api_versions"] == ["v1", "v2"]
    assert health["revision"]
    ready = client.get("/ready").json()
    assert ready["database"] == "ok"
    assert ready["services"]["enabled"] and ready["services"]["configured"]
    assert ready["services"]["dispatcher_running"]
