"""Link a reversal to the transaction it reverses.

A refund is not an edit and not a deletion — it is a new transaction with its
own pair of signed entries, pointing back at the original. Without this column
the two are related only by amount and timing, which is a guess; with it, the
provenance of every correction is explicit and queryable.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE transactions
            ADD COLUMN reverses_transaction_id UUID REFERENCES transactions(id);
        """
    )
    # A transaction can be reversed at most once. This is a database guarantee,
    # not a check in application code — the same posture as `uq_idem`.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_one_reversal_per_transaction
            ON transactions (reverses_transaction_id)
            WHERE reverses_transaction_id IS NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE transactions
            ADD CONSTRAINT reversal_has_target CHECK (
                (type = 'REVERSAL' AND reverses_transaction_id IS NOT NULL) OR
                (type <> 'REVERSAL' AND reverses_transaction_id IS NULL)
            );
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE transactions DROP CONSTRAINT IF EXISTS reversal_has_target;")
    op.execute("DROP INDEX IF EXISTS uq_one_reversal_per_transaction;")
    op.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS reverses_transaction_id;")
