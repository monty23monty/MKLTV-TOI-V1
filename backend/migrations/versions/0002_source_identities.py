"""Separate persistent external people from match-specific tracking entries.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("person_identities", sa.Column("id", sa.String(100), primary_key=True), sa.Column("provider", sa.String(50), nullable=False), sa.Column("external_player_id", sa.String(100), nullable=False), sa.Column("name", sa.String(200), nullable=False))
    op.create_index("ix_person_identities_external_player_id", "person_identities", ["external_player_id"])
    if op.get_bind().dialect.name == "sqlite":
        # SQLite supports a nullable REFERENCES column directly; avoid rebuilding
        # a parent table while live rosters/locations reference its existing rows.
        op.execute("ALTER TABLE players ADD COLUMN person_id VARCHAR(100) REFERENCES person_identities(id)")
    else:
        op.add_column("players", sa.Column("person_id", sa.String(100), sa.ForeignKey("person_identities.id", name="fk_players_person_id"), nullable=True))
    op.create_index("ix_players_person_id", "players", ["person_id"])
    op.add_column("game_rosters", sa.Column("external_match_player_id", sa.String(100), nullable=True))
    op.add_column("game_rosters", sa.Column("source_data", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("game_rosters", "source_data")
    op.drop_column("game_rosters", "external_match_player_id")
    op.drop_index("ix_players_person_id", table_name="players")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_players_person_id", "players", type_="foreignkey")
    op.drop_column("players", "person_id")
    op.drop_table("person_identities")
