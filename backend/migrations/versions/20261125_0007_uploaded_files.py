"""学生上传的文件。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('uploaded_file',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token', sa.String(length=40), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=False),
    sa.Column('field', sa.String(length=80), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('path', sa.String(length=300), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['game.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_uploaded_file_game_id'), 'uploaded_file', ['game_id'], unique=False)
    op.create_index(op.f('ix_uploaded_file_team_id'), 'uploaded_file', ['team_id'], unique=False)
    op.create_index(op.f('ix_uploaded_file_token'), 'uploaded_file', ['token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_uploaded_file_token'), table_name='uploaded_file')
    op.drop_index(op.f('ix_uploaded_file_team_id'), table_name='uploaded_file')
    op.drop_index(op.f('ix_uploaded_file_game_id'), table_name='uploaded_file')
    op.drop_table('uploaded_file')
