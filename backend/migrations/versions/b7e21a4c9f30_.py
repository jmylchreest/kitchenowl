"""add scope and household restriction to token

Revision ID: b7e21a4c9f30
Revises: 0b10d67750be
Create Date: 2026-07-31 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e21a4c9f30'
down_revision = '0b10d67750be'
branch_labels = None
depends_on = None


def upgrade():
    # Nullable: none means unrestricted, so existing tokens are unaffected.
    with op.batch_alter_table('token', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scope', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('household_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_token_household_id'), ['household_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_token_household_id', 'household', ['household_id'], ['id'], ondelete='CASCADE'
        )


def downgrade():
    with op.batch_alter_table('token', schema=None) as batch_op:
        batch_op.drop_constraint('fk_token_household_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_token_household_id'))
        batch_op.drop_column('household_id')
        batch_op.drop_column('scope')
