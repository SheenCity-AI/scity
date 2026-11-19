"""知识点点亮记录：老师确认分数后，把分数记到每个组员的知识点上。

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('knowledge_mark',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('point_code', sa.String(length=20), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('course_ref', sa.String(length=100), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('touches', sa.Integer(), nullable=False),
    sa.Column('minutes', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['game.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'point_code', 'game_id')
    )
    op.create_index(op.f('ix_knowledge_mark_game_id'), 'knowledge_mark', ['game_id'], unique=False)
    op.create_index(op.f('ix_knowledge_mark_point_code'), 'knowledge_mark', ['point_code'], unique=False)
    op.create_index(op.f('ix_knowledge_mark_user_id'), 'knowledge_mark', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_knowledge_mark_user_id'), table_name='knowledge_mark')
    op.drop_index(op.f('ix_knowledge_mark_point_code'), table_name='knowledge_mark')
    op.drop_index(op.f('ix_knowledge_mark_game_id'), table_name='knowledge_mark')
    op.drop_table('knowledge_mark')
