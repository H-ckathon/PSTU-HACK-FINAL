"""Session families, so refresh-token reuse revokes the whole chain.

Rotation creates a new session each time. Blocking only the replayed session
leaves its descendants alive — so an attacker who steals a refresh token, uses
it once and lets the victim's replay trip the alarm would keep working access.

Tagging every rotated session with the family it descends from lets one
detection revoke the entire chain, which is the behaviour "reuse detection"
is actually supposed to mean.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ADD COLUMN family_id UUID;")
    # Existing rows are their own family root.
    op.execute("UPDATE sessions SET family_id = id WHERE family_id IS NULL;")
    op.execute("ALTER TABLE sessions ALTER COLUMN family_id SET NOT NULL;")
    op.execute("CREATE INDEX idx_sessions_family ON sessions (family_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sessions_family;")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS family_id;")
