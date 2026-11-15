"""课题引擎：一局课、组、组员、填报、事件。

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('game',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workspace_id', sa.Integer(), nullable=False),
    sa.Column('course_ref', sa.String(length=100), nullable=False),
    sa.Column('seed', sa.String(length=64), nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=True),
    sa.Column('stage_opened_at', sa.DateTime(), nullable=True),
    sa.Column('stage_deadline', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_game_workspace_id'), 'game', ['workspace_id'], unique=False)
    op.create_table('game_event',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['game.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_game_event_game_id'), 'game_event', ['game_id'], unique=False)
    op.create_table('team',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('company', sa.String(length=64), nullable=False),
    sa.Column('npc', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['game.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('game_id', 'name')
    )
    op.create_index(op.f('ix_team_game_id'), 'team', ['game_id'], unique=False)
    op.create_table('decision',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('stage', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['game.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_decision_game_id'), 'decision', ['game_id'], unique=False)
    op.create_index(op.f('ix_decision_team_id'), 'decision', ['team_id'], unique=False)
    op.create_table('team_member',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('game_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('post', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['game_id'], ['game.id'], ),
    sa.ForeignKeyConstraint(['team_id'], ['team.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('game_id', 'user_id')
    )
    op.create_index(op.f('ix_team_member_game_id'), 'team_member', ['game_id'], unique=False)
    op.create_index(op.f('ix_team_member_team_id'), 'team_member', ['team_id'], unique=False)
    op.create_index(op.f('ix_team_member_user_id'), 'team_member', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_team_member_user_id'), table_name='team_member')
    op.drop_index(op.f('ix_team_member_team_id'), table_name='team_member')
    op.drop_index(op.f('ix_team_member_game_id'), table_name='team_member')
    op.drop_table('team_member')
    op.drop_index(op.f('ix_decision_team_id'), table_name='decision')
    op.drop_index(op.f('ix_decision_game_id'), table_name='decision')
    op.drop_table('decision')
    op.drop_index(op.f('ix_team_game_id'), table_name='team')
    op.drop_table('team')
    op.drop_index(op.f('ix_game_event_game_id'), table_name='game_event')
    op.drop_table('game_event')
    op.drop_index(op.f('ix_game_workspace_id'), table_name='game')
    op.drop_table('game')
