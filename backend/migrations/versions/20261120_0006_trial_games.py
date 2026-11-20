"""体验班：一局课可以只开到第几次课。

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('game', sa.Column('last_lesson', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('game', 'last_lesson')
