"""Deterministic replacement rankings rebuilt from authoritative movements.

Scores are ranking points, never probabilities. No learned mutable cache is kept:
voiding or replacing an event immediately changes the next recommendation.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Any

from .domain import DomainError

DEFAULT_WEIGHTS: dict[str, float] = {
    "same_position": 28.0,
    "same_family": 16.0,
    "same_defence_side": 12.0,
    "goalkeeper_mismatch": -45.0,
    "direct_replacement": 18.0,
    "max_direct_observations": 5.0,
    "rest_per_second": 0.18,
    "max_rest_seconds": 120.0,
    "recent_exit_penalty": 24.0,
    "line_teammate": 8.0,
    "observed_teammate": 3.0,
    "configured_group": 32.0,
    "learned_group": 25.0,
    "observed_group": 12.0,
    "special_group": 12.0,
    "group_role_mismatch": 40.0,
}


def _family(position: str) -> str:
    return "F" if position in {"C", "LW", "RW"} else position


def _active_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in state.get("events", []) if e.get("active", not e.get("voided", False)) and e["type"] in {"substitute", "move"}]


def _observed_combinations(state: dict[str, Any], team_id: str, players: dict[str, Any]) -> Counter[tuple[str, ...]]:
    """Count confirmed lineup observations; repeated snapshots never add evidence."""
    counts: Counter[tuple[str, ...]] = Counter()
    moments = sorted({
        e.get("at_playing_ms", e.get("playing_ms", 0)) for e in _active_events(state)
        if e["payload"].get("team_id") == team_id
        or players.get(e["payload"].get("player_id"), {}).get("team_id") == team_id
    })
    shifts = [s for s in state.get("shifts", []) if s["team_id"] == team_id]
    for moment in moments:
        on_ice = [s["player_id"] for s in shifts if s.get("start_playing_ms", s.get("start_ms", 0)) <= moment and (s.get("end_playing_ms", s.get("end_ms")) is None or s.get("end_playing_ms", s.get("end_ms")) > moment)]
        forwards = tuple(sorted(pid for pid in on_ice if _family(players[pid]["position"]) == "F"))
        defence = tuple(sorted(pid for pid in on_ice if players[pid]["position"] == "D"))
        skaters = tuple(sorted(pid for pid in on_ice if players[pid]["position"] != "G"))
        for group in {forwards, defence, skaters}:
            if 2 <= len(group) <= 6:
                counts[group] += 1
    return counts


def recommend(
    state: dict[str, Any], team_id: str, outgoing_ids: list[str],
    *, weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Rank every eligible bench player and useful atomic replacement groups.

    All data must be from one authoritative snapshot. Configuration can override
    individual weights without changing learning or eligibility rules.
    """
    weight = {**DEFAULT_WEIGHTS, **(weights or {})}
    players = {p["id"]: p for p in state["players"]}
    if team_id not in {t["id"] for t in state["teams"]}:
        raise DomainError("Unknown recommendation team.")
    if len(set(outgoing_ids)) != len(outgoing_ids):
        raise DomainError("Outgoing players contain a duplicate.")
    if any(pid not in players or players[pid]["team_id"] != team_id for pid in outgoing_ids):
        raise DomainError("Outgoing players must belong to the selected team.")
    outgoing = [players[pid] for pid in outgoing_ids]
    eligible = {
        p["id"]: p for p in state["players"]
        if p["team_id"] == team_id and p["location"] == "BENCH" and p.get("eligible", True)
    }
    remaining = {
        p["id"] for p in state["players"]
        if p["team_id"] == team_id and p["location"] == "ON_ICE" and p["id"] not in outgoing_ids
    }
    current_strength = sum(players[pid]["position"] != "G" for pid in remaining | set(outgoing_ids))
    other_strength = sum(p["location"] == "ON_ICE" and p["position"] != "G" for p in players.values() if p["team_id"] != team_id)
    now = state["clock"]["playing_ms"]
    events = _active_events(state)
    direct: Counter[tuple[str, str]] = Counter()
    learned_groups: Counter[tuple[str, ...]] = Counter()
    for event in events:
        if event["type"] != "substitute" or event["payload"].get("team_id") != team_id:
            continue
        departed = event["payload"].get("outgoing_ids", [])
        arrived = event["payload"].get("incoming_ids", [])
        if len(arrived) > 1:
            learned_groups[tuple(sorted(arrived))] += 1
        for old in departed:
            for new in arrived:
                if old in players and new in players and (len(departed) == len(arrived) == 1 or _family(players[old]["position"]) == _family(players[new]["position"])):
                    direct[old, new] += 1
    observed = _observed_combinations(state, team_id, players)
    rankings: list[dict[str, Any]] = []
    for player_id, player in eligible.items():
        reasons: list[str] = []
        score = 0.0
        if outgoing:
            if any(p["position"] == player["position"] for p in outgoing):
                score += weight["same_position"]
                reasons.append("Same position")
                # The engine keeps a general D role; imported LD/RD assignments
                # add a transparent preference without constraining who can enter.
                assigned_role = player.get("assigned_role")
                if player["position"] == "D" and assigned_role in {"LD", "RD"} and any(
                    p["position"] == "D" and p.get("assigned_role") == assigned_role for p in outgoing
                ):
                    score += weight["same_defence_side"]
                    reasons.append("Same assigned defensive side")
            elif any(_family(p["position"]) == _family(player["position"]) for p in outgoing):
                score += weight["same_family"]
                reasons.append("Compatible forward position")
            elif player["position"] == "G" or all(p["position"] == "G" for p in outgoing):
                score += weight["goalkeeper_mismatch"]
                reasons.append("Different goalkeeper role")
        observations = sum(direct[old, player_id] for old in outgoing_ids)
        if observations:
            score += min(observations, weight["max_direct_observations"]) * weight["direct_replacement"]
            reasons.append(f"Frequent replacement ({observations} confirmed)")
        closed = [s for s in state.get("shifts", []) if s["player_id"] == player_id and s.get("end_playing_ms", s.get("end_ms")) is not None]
        last_end = max((s.get("end_playing_ms", s.get("end_ms")) for s in closed), default=0)
        rest_ms = max(0, now - last_end)
        rest_seconds = rest_ms / 1000
        score += min(rest_seconds, weight["max_rest_seconds"]) * weight["rest_per_second"]
        if rest_seconds >= 30:
            reasons.append("Recently rested")
        if closed and rest_seconds < 12:
            score -= weight["recent_exit_penalty"] * (1 - rest_seconds / 12)
            reasons.append("Just left the ice")
        configured_mates: set[str] = set()
        for line in state.get("lines", []):
            if line["team_id"] == team_id and player_id in line["player_ids"]:
                configured_mates.update(set(line["player_ids"]) & remaining)
        if configured_mates:
            score += len(configured_mates) * weight["line_teammate"]
            reasons.append("Typical defensive partner" if player["position"] == "D" else "Regular line combination")
        partner_observations = sum(count for group, count in observed.items() if player_id in group and remaining & set(group))
        if partner_observations:
            score += min(partner_observations, 5) * weight["observed_teammate"]
            reasons.append("Observed with remaining players")
        if not reasons:
            reasons.append("Available on the bench")
        rankings.append({
            "player_id": player_id, "score": round(score, 2), "reasons": reasons,
            "eligible": True, "observations": observations, "rest_ms": rest_ms,
            "confidence": "established" if observations >= 4 else "developing" if observations else "cold_start",
        })
    rankings.sort(key=lambda item: (-item["score"], players[item["player_id"]]["number"], item["player_id"]))
    by_id = {item["player_id"]: item for item in rankings}
    group_candidates: dict[tuple[str, ...], dict[str, Any]] = {}
    outgoing_roles = Counter(_family(p["position"]) for p in outgoing)

    def group_candidate(ids: tuple[str, ...], name: str, reason: str, bonus: float) -> None:
        if not ids or not set(ids) <= eligible.keys() or (outgoing_ids and len(ids) != len(outgoing_ids)):
            return
        if ids not in group_candidates:
            incoming_roles = Counter(_family(players[pid]["position"]) for pid in ids)
            # Equal-sized groups can still replace the wrong kinds of players.
            # Keep unusual combinations available, but charge each missing role
            # so a configured pair cannot dominate a mixed forward/defence change.
            mismatches = sum((outgoing_roles - incoming_roles).values()) if outgoing_ids else 0
            group_candidates[ids] = {
                "player_ids": list(ids), "name": name,
                "score": sum(by_id[pid]["score"] for pid in ids) / len(ids) - mismatches * weight["group_role_mismatch"],
                "reasons": ["Different position mix"] if mismatches else [],
                "eligible": True, "observations": learned_groups[ids] + observed[ids],
            }
        group = group_candidates[ids]
        if reason not in group["reasons"]:
            group["score"] += bonus
            group["reasons"].append(reason)

    for line in state.get("lines", []):
        if line["team_id"] == team_id:
            special_bonus = weight["special_group"] if line["kind"] == "SPECIAL" and current_strength != other_strength else 0
            group_candidate(tuple(sorted(line["player_ids"])), line["name"], "Configured line" if line["kind"] != "DEFENCE" else "Configured defensive pair", weight["configured_group"] + special_bonus)
    for group, count in learned_groups.items():
        group_candidate(group, "Learned change", f"Entered together {count} times", min(count, 5) * weight["learned_group"])
    for group, count in observed.items():
        group_candidate(group, "Observed combination", f"Observed together {count} times", min(count, 5) * weight["observed_group"])
    # Mixed, unconfigured changes still get immediately useful complete groups.
    group_size = len(outgoing_ids)
    if 2 <= group_size <= 5:
        best_ids = [item["player_id"] for item in rankings[:10]]
        for choice in combinations(best_ids, group_size):
            key = tuple(sorted(choice))
            if key in group_candidates:
                continue
            incoming_roles = Counter(_family(players[pid]["position"]) for pid in key)
            if outgoing_roles == incoming_roles:
                group_candidate(key, "Suggested mixed change", "Compatible positions", 4)
    groups = sorted(group_candidates.values(), key=lambda item: (-item["score"], item["player_ids"]))[:8]
    for group in groups:
        group["score"] = round(group["score"], 2)
    observed_output = [
        {"player_ids": list(group), "observations": count, "eligible": set(group) <= eligible.keys(), "name": "Observed combination"}
        for group, count in sorted(observed.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"players": rankings, "groups": groups, "combinations": observed_output}
