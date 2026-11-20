"""学生本人的专业：知识图以它为中心，按离本专业的远近分圈。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('major_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_user_major_id', 'major', ['major_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('user') as batch:
        batch.drop_constraint('fk_user_major_id', type_='foreignkey')
        batch.drop_column('major_id')
