"""Versioned command contracts, also exposed by the generated OpenAPI schema."""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Location = Literal["ON_ICE", "BENCH", "PENALTY_BOX"]
Position = Literal["C", "LW", "RW", "D", "G"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Movement(Contract):
    player_id: str = Field(min_length=1, max_length=100)
    to: Location


class Substitution(Contract):
    team_id: str = Field(min_length=1, max_length=100)
    outgoing_ids: list[str] = Field(default_factory=list)
    incoming_ids: list[str] = Field(default_factory=list)
    at_playing_ms: int | None = Field(default=None, ge=0)


class ClockControl(Contract):
    operation: Literal["start", "pause", "set", "advance"]
    period: int | None = Field(default=None, ge=1, le=20)
    remaining_ms: int | None = Field(default=None, ge=0)
    period_lengths_ms: list[int] | None = None


class Undo(Contract):
    event_id: str | None = None


class SubstituteReplacement(Contract):
    type: Literal["substitute"]
    payload: Substitution


class MoveReplacement(Contract):
    type: Literal["move"]
    payload: Movement


Replacement = Annotated[Union[SubstituteReplacement, MoveReplacement], Field(discriminator="type")]


class Correction(Contract):
    event_id: str
    replacement: Replacement


class RosterPatch(Contract):
    id: str = Field(min_length=1, max_length=100)
    team_id: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    number: int | None = Field(default=None, ge=0, le=99, strict=True)
    position: Position | None = None
    eligible: bool | None = Field(default=None, strict=True)
    location: Location | None = None
    external_id: str | None = Field(default=None, max_length=200)
    assigned_role: Literal["GK", "G", "LD", "RD", "D", "LW", "C", "RW"] | None = None


class LineConfiguration(Contract):
    id: str = Field(min_length=1, max_length=100)
    team_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["FORWARD", "DEFENCE", "SPECIAL"]
    player_ids: list[str] = Field(default_factory=list, max_length=40)
    slots: dict[str, str | None] | None = None
    line_number: int | None = None


class ImportLineConfiguration(LineConfiguration):
    # Empty planned lines are useful during pre-match review.
    player_ids: list[str] = Field(default_factory=list, max_length=40)


class Coverage(Contract):
    period: int = Field(default=1, ge=1, le=20)
    remaining_ms: int = Field(default=1200000, ge=0)


class ImportGame(Contract):
    provider: Literal["hokejovy-zapis"]
    external_game_id: str = Field(pattern=r"^[0-9]{1,15}$")
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    players: list[RosterPatch] | None = Field(default=None, max_length=100)
    lines: list[ImportLineConfiguration] | None = Field(default=None, max_length=100)
    starting_mode: Literal["official", "manual"] = "official"
    coverage: Coverage = Field(default_factory=Coverage)


class RefreshRoster(Contract):
    request_id: str = Field(min_length=8, max_length=100)
    expected_version: int = Field(ge=0)
    confirm: bool = False
    restore_lines: bool = False
    preview_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)


class GameConfiguration(Contract):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    competition: str | None = Field(default=None, max_length=200)
    scheduled_start: str | None = None
    external_id: str | None = Field(default=None, max_length=200)
    status: Literal["SETUP", "LIVE", "COMPLETED"] | None = None
    period_lengths_ms: list[int] | None = None
    home_name: str | None = Field(default=None, min_length=1, max_length=100)
    away_name: str | None = Field(default=None, min_length=1, max_length=100)


class Setup(Contract):
    players: list[RosterPatch] | None = Field(default=None, max_length=100)
    lines: list[LineConfiguration] | None = Field(default=None, max_length=100)
    game: GameConfiguration | None = None


class CommandBase(Contract):
    request_id: str = Field(min_length=8, max_length=100)
    expected_version: int = Field(ge=0)
    actor: str | None = None  # Accepted for client compatibility; server identity always wins.


class SubstituteCommand(CommandBase):
    type: Literal["substitute"]
    payload: Substitution


class MoveCommand(CommandBase):
    type: Literal["move"]
    payload: Movement


class ClockCommand(CommandBase):
    type: Literal["clock"]
    payload: ClockControl


class UndoCommand(CommandBase):
    type: Literal["undo"]
    payload: Undo = Field(default_factory=Undo)


class CorrectCommand(CommandBase):
    type: Literal["correct"]
    payload: Correction


class SetupCommand(CommandBase):
    type: Literal["setup"]
    payload: Setup


Action = Annotated[Union[SubstituteCommand, MoveCommand, ClockCommand, UndoCommand, CorrectCommand, SetupCommand], Field(discriminator="type")]


class NewGame(Contract):
    name: str | None = Field(default=None, max_length=200)
    home_name: str | None = Field(default=None, max_length=100)
    away_name: str | None = Field(default=None, max_length=100)


class ExternalClock(Contract):
    request_id: str = Field(min_length=8, max_length=100)
    expected_version: int = Field(ge=0)
    period: int = Field(ge=1, le=20)
    remaining_ms: int = Field(ge=0)
    running: bool
    observed_at_ms: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=100)


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    game: dict[str, Any]
    teams: list[dict[str, Any]]
    players: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    clock: dict[str, Any]
    events: list[dict[str, Any]]
    shifts: list[dict[str, Any]]
    version: int
    warnings: list[str] = Field(default_factory=list)
