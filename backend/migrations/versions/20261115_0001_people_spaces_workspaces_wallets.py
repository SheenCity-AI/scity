"""人、空间、成员关系、工作区、钱包、点数流水、模型用量（11-15 起的数据结构）。

Revision ID: 0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('space',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['space.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_space_parent_id'), 'space', ['parent_id'], unique=False)
    op.create_table('user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('feishu_open_id', sa.String(length=64), nullable=False),
    sa.Column('display_name', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_feishu_open_id'), 'user', ['feishu_open_id'], unique=True)
    op.create_table('membership',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('space_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('joined_at', sa.DateTime(), nullable=False),
    sa.Column('left_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['space_id'], ['space.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'space_id')
    )
    op.create_index(op.f('ix_membership_space_id'), 'membership', ['space_id'], unique=False)
    op.create_index(op.f('ix_membership_user_id'), 'membership', ['user_id'], unique=False)
    op.create_table('wallet',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('space_id', sa.Integer(), nullable=True),
    sa.Column('balance', sa.Integer(), nullable=False),
    sa.Column('granted_total', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint('user_id IS NOT NULL OR space_id IS NOT NULL', name='wallet_has_owner'),
    sa.ForeignKeyConstraint(['space_id'], ['space.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('wallet_allocation', 'wallet', ['user_id', 'space_id'], unique=True, sqlite_where=sa.text('user_id IS NOT NULL AND space_id IS NOT NULL'), postgresql_where=sa.text('user_id IS NOT NULL AND space_id IS NOT NULL'))
    op.create_index('wallet_personal', 'wallet', ['user_id'], unique=True, sqlite_where=sa.text('space_id IS NULL'), postgresql_where=sa.text('space_id IS NULL'))
    op.create_index('wallet_pool', 'wallet', ['space_id'], unique=True, sqlite_where=sa.text('user_id IS NULL'), postgresql_where=sa.text('user_id IS NULL'))
    op.create_table('workspace',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('space_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('course_ref', sa.String(length=100), nullable=False),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['space_id'], ['space.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_workspace_space_id'), 'workspace', ['space_id'], unique=False)
    op.create_table('ledger_entry',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('wallet_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('delta', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('space_id', sa.Integer(), nullable=True),
    sa.Column('workspace_id', sa.Integer(), nullable=True),
    sa.Column('ref', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['space_id'], ['space.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallet.id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ledger_entry_space_id'), 'ledger_entry', ['space_id'], unique=False)
    op.create_index(op.f('ix_ledger_entry_user_id'), 'ledger_entry', ['user_id'], unique=False)
    op.create_index(op.f('ix_ledger_entry_wallet_id'), 'ledger_entry', ['wallet_id'], unique=False)
    op.create_index(op.f('ix_ledger_entry_workspace_id'), 'ledger_entry', ['workspace_id'], unique=False)
    op.create_table('usage_record',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('space_id', sa.Integer(), nullable=True),
    sa.Column('workspace_id', sa.Integer(), nullable=True),
    sa.Column('purpose', sa.String(length=20), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('points', sa.Integer(), nullable=False),
    sa.Column('cost_micro', sa.BigInteger(), nullable=False),
    sa.Column('ref', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['space_id'], ['space.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_usage_record_created_at'), 'usage_record', ['created_at'], unique=False)
    op.create_index(op.f('ix_usage_record_space_id'), 'usage_record', ['space_id'], unique=False)
    op.create_index(op.f('ix_usage_record_user_id'), 'usage_record', ['user_id'], unique=False)
    op.create_index(op.f('ix_usage_record_workspace_id'), 'usage_record', ['workspace_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_usage_record_workspace_id'), table_name='usage_record')
    op.drop_index(op.f('ix_usage_record_user_id'), table_name='usage_record')
    op.drop_index(op.f('ix_usage_record_space_id'), table_name='usage_record')
    op.drop_index(op.f('ix_usage_record_created_at'), table_name='usage_record')
    op.drop_table('usage_record')
    op.drop_index(op.f('ix_ledger_entry_workspace_id'), table_name='ledger_entry')
    op.drop_index(op.f('ix_ledger_entry_wallet_id'), table_name='ledger_entry')
    op.drop_index(op.f('ix_ledger_entry_user_id'), table_name='ledger_entry')
    op.drop_index(op.f('ix_ledger_entry_space_id'), table_name='ledger_entry')
    op.drop_table('ledger_entry')
    op.drop_index(op.f('ix_workspace_space_id'), table_name='workspace')
    op.drop_table('workspace')
    op.drop_index('wallet_pool', table_name='wallet', sqlite_where=sa.text('user_id IS NULL'), postgresql_where=sa.text('user_id IS NULL'))
    op.drop_index('wallet_personal', table_name='wallet', sqlite_where=sa.text('space_id IS NULL'), postgresql_where=sa.text('space_id IS NULL'))
    op.drop_index('wallet_allocation', table_name='wallet', sqlite_where=sa.text('user_id IS NOT NULL AND space_id IS NOT NULL'), postgresql_where=sa.text('user_id IS NOT NULL AND space_id IS NOT NULL'))
    op.drop_table('wallet')
    op.drop_index(op.f('ix_membership_user_id'), table_name='membership')
    op.drop_index(op.f('ix_membership_space_id'), table_name='membership')
    op.drop_table('membership')
    op.drop_index(op.f('ix_user_feishu_open_id'), table_name='user')
    op.drop_table('user')
    op.drop_index(op.f('ix_space_parent_id'), table_name='space')
    op.drop_table('space')
