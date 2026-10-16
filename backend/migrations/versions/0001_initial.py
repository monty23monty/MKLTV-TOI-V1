"""Initial relational match aggregate, projections, immutable audits and receipts.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("games", sa.Column("id", sa.String(80), primary_key=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("external_id", sa.String(200)), sa.Column("status", sa.String(30), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("state", sa.JSON(), nullable=False))
    op.create_index("ix_games_external_id", "games", ["external_id"])
    op.create_table("teams", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("id", sa.String(100), primary_key=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("external_id", sa.String(200)), sa.Column("data", sa.JSON(), nullable=False))
    op.create_index("ix_teams_external_id", "teams", ["external_id"])
    op.create_table("players", sa.Column("id", sa.String(100), primary_key=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("external_id", sa.String(200)), sa.Column("default_position", sa.String(10), nullable=False))
    op.create_index("ix_players_external_id", "players", ["external_id"])
    op.create_table("game_rosters", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("player_id", sa.String(100), sa.ForeignKey("players.id"), primary_key=True), sa.Column("team_id", sa.String(100), nullable=False), sa.Column("number", sa.Integer(), nullable=False), sa.Column("position", sa.String(10), nullable=False), sa.Column("eligible", sa.Boolean(), nullable=False))
    op.create_table("player_locations", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("player_id", sa.String(100), sa.ForeignKey("players.id"), primary_key=True), sa.Column("location", sa.String(20), nullable=False), sa.Column("last_event_id", sa.String(100)))
    op.create_table("shifts", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("id", sa.String(150), primary_key=True), sa.Column("player_id", sa.String(100), nullable=False), sa.Column("team_id", sa.String(100), nullable=False), sa.Column("start_playing_ms", sa.BigInteger(), nullable=False), sa.Column("end_playing_ms", sa.BigInteger()), sa.Column("data", sa.JSON(), nullable=False))
    op.create_index("ix_shifts_player_id", "shifts", ["player_id"])
    op.create_table("line_configurations", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("id", sa.String(100), primary_key=True), sa.Column("team_id", sa.String(100), nullable=False), sa.Column("data", sa.JSON(), nullable=False))
    op.create_table("match_events", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("id", sa.String(100), primary_key=True), sa.Column("type", sa.String(40), nullable=False), sa.Column("active", sa.Boolean(), nullable=False), sa.Column("playing_ms", sa.BigInteger(), nullable=False), sa.Column("data", sa.JSON(), nullable=False))
    op.create_index("ix_match_events_type", "match_events", ["type"])
    op.create_table("audit_events", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("version", sa.Integer(), primary_key=True), sa.Column("actor", sa.String(100), nullable=False), sa.Column("request_id", sa.String(100), nullable=False), sa.Column("created_at_ms", sa.BigInteger(), nullable=False), sa.Column("command", sa.JSON(), nullable=False), sa.Column("previous_values", sa.JSON(), nullable=False), sa.Column("new_values", sa.JSON(), nullable=False))
    op.create_table("action_receipts", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("request_id", sa.String(100), primary_key=True), sa.Column("actor", sa.String(100), nullable=False), sa.Column("fingerprint", sa.Text(), nullable=False), sa.Column("response", sa.JSON(), nullable=False))


def downgrade():
    for table in ("action_receipts", "audit_events", "match_events", "line_configurations", "shifts", "player_locations", "game_rosters", "players", "teams", "games"):
        op.drop_table(table)
