"""record which long-lived token added a shopping list item

Revision ID: c8f3d5b71e42
Revises: b7e21a4c9f30
Create Date: 2026-07-31 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8f3d5b71e42'
down_revision = 'b7e21a4c9f30'
branch_labels = None
depends_on = None


def upgrade():
    # Name rather than a token id, so attribution outlives revocation.
    with op.batch_alter_table('shoppinglist_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('created_by_token_name', sa.String(length=255), nullable=True)
        )


def downgrade():
    with op.batch_alter_table('shoppinglist_items', schema=None) as batch_op:
        batch_op.drop_column('created_by_token_name')
