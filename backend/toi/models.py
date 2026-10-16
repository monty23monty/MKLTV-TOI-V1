"""Relational records and rebuildable projections of the authoritative match log."""

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GameRow(Base):
    __tablename__ = "games"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSON)


class TeamRow(Base):
    __tablename__ = "teams"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    data: Mapped[dict] = mapped_column(JSON)


class PlayerRow(Base):
    __tablename__ = "players"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    default_position: Mapped[str] = mapped_column(String(10))
    person_id: Mapped[str | None] = mapped_column(ForeignKey("person_identities.id"), nullable=True, index=True)


class PersonIdentityRow(Base):
    """Persistent provider identity shared by match-specific tracking entries."""
    __tablename__ = "person_identities"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    external_player_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(200))


class RosterRow(Base):
    __tablename__ = "game_rosters"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(100))
    number: Mapped[int] = mapped_column(Integer)
    position: Mapped[str] = mapped_column(String(10))
    eligible: Mapped[bool] = mapped_column(Boolean)
    external_match_player_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LocationRow(Base):
    __tablename__ = "player_locations"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), primary_key=True)
    location: Mapped[str] = mapped_column(String(20))
    last_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ShiftRow(Base):
    __tablename__ = "shifts"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(150), primary_key=True)
    player_id: Mapped[str] = mapped_column(String(100), index=True)
    team_id: Mapped[str] = mapped_column(String(100))
    start_playing_ms: Mapped[int] = mapped_column(BigInteger)
    end_playing_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


class LineRow(Base):
    __tablename__ = "line_configurations"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON)


class EventRow(Base):
    __tablename__ = "match_events"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    active: Mapped[bool] = mapped_column(Boolean)
    playing_ms: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[dict] = mapped_column(JSON)


class AuditRow(Base):
    __tablename__ = "audit_events"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(100))
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    command: Mapped[dict] = mapped_column(JSON)
    previous_values: Mapped[dict] = mapped_column(JSON)
    new_values: Mapped[dict] = mapped_column(JSON)


class ReceiptRow(Base):
    __tablename__ = "action_receipts"
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    actor: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(Text)
    response: Mapped[dict] = mapped_column(JSON)
