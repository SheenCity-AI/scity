from datetime import timedelta

import pytest
from sqlalchemy import func, select

from scity.models import LedgerEntry, UsageRecord, Wallet, utcnow
from scity.report import usage_by_space
from scity.spaces import NotMember
from scity.wallet import ModelCall, SpendCapReached, points_for

SOON = utcnow() + timedelta(days=30)
LATER = utcnow() + timedelta(days=90)
PAST = utcnow() - timedelta(days=1)


def call(points: int) -> ModelCall:
    """演示换算下，输入 1000 token 等于 1 点。"""
    return ModelCall("m", points * 1000, 0)


@pytest.fixture
def world(spaces, wallets):
    """一个学生：在甲学校一班上沙盘课，同时在孵化器下的工作室做一个项目。"""
    class World:
        pass
    w = World()
    w.uid = spaces.ensure_user("ou_a")
    w.school = spaces.create_space("school", "甲学校")
    w.klass = spaces.create_space("class", "一班", parent_id=w.school)
    w.topic = spaces.create_workspace(w.klass, "topic", "沙盘")
    w.incubator = spaces.create_space("incubator", "某市孵化器")
    w.opc = spaces.create_space("opc", "晓光工作室", parent_id=w.incubator)
    w.project = spaces.create_workspace(w.opc, "project", "产业园数字沙盘")
    spaces.add_member(w.uid, w.klass, "student")
    spaces.add_member(w.uid, w.opc, "member")
    w.ctx = lambda workspace_id=None: spaces.context(w.uid, workspace_id)
    return w


def balances(sessions) -> dict:
    with sessions() as db:
        return {(w.user_id, w.space_id): w.balance for w in db.scalars(select(Wallet))}


def test_points_round_up_and_use_model_rate():
    pricing = {"default": {"input_per_1k": 1, "output_per_1k": 3}, "m2": {"input_per_1k": 10, "output_per_1k": 10}}
    assert points_for(ModelCall("m1", 1000, 1000), pricing) == 4
    assert points_for(ModelCall("m1", 1, 0), pricing) == 1
    assert points_for(ModelCall("m1", 0, 0), pricing) == 0
    assert points_for(ModelCall("m2", 1000, 0), pricing) == 10


def test_school_subsidy_is_spent_before_personal_balance_in_topic_and_main_chat(world, wallets, sessions):
    wallets.grant(space_id=world.school, user_id=world.uid, points=3, expires_at=SOON, ref="g")
    wallets.grant(user_id=world.uid, points=10, ref="topup")
    result = wallets.charge_turn(world.ctx(world.topic), [call(2)], "r1")
    assert (result.points, result.from_subsidy, result.from_balance, result.shortfall) == (2, 2, 0, 0)
    result = wallets.charge_turn(world.ctx(), [call(4)], "r2")
    assert (result.from_subsidy, result.from_balance, result.shortfall) == (1, 3, 0)
    assert (result.wallet.subsidy, result.wallet.balance) == (0, 7)


def test_school_subsidy_cannot_pay_for_project_work(world, wallets, sessions):
    wallets.grant(space_id=world.school, user_id=world.uid, points=100, expires_at=SOON, ref="g")
    wallets.grant(user_id=world.uid, points=50, ref="topup")
    assert wallets.view(world.ctx(world.project)).available == 0
    result = wallets.charge_turn(world.ctx(world.project), [call(5)], "r1")
    assert (result.from_subsidy, result.from_balance, result.shortfall) == (0, 0, 5)
    assert balances(sessions) == {(world.uid, world.school): 100, (world.uid, None): 50}


def test_project_work_uses_incubator_allocation_then_project_budget(world, wallets, sessions):
    wallets.grant(space_id=world.incubator, user_id=world.uid, points=3, ref="alloc")
    wallets.grant(space_id=world.opc, points=10, ref="budget")
    result = wallets.charge_turn(world.ctx(world.project), [call(5)], "r1")
    assert (result.from_subsidy, result.from_balance, result.shortfall) == (5, 0, 0)
    assert balances(sessions) == {(world.uid, world.incubator): 0, (None, world.opc): 8}
    # 项目的钱不能拿去上课或闲聊
    assert wallets.view(world.ctx(world.topic)).available == 0
    assert wallets.view(world.ctx()).available == 0


def test_another_projects_budget_is_out_of_reach(world, wallets, spaces, sessions):
    other = spaces.create_space("opc", "别人的工作室", parent_id=world.incubator)
    wallets.grant(space_id=other, points=10, ref="budget")
    result = wallets.charge_turn(world.ctx(world.project), [call(2)], "r1")
    assert result.shortfall == 2
    assert balances(sessions) == {(None, other): 10}


def test_never_goes_negative_and_records_shortfall(world, wallets, sessions):
    wallets.grant(space_id=world.school, user_id=world.uid, points=1, expires_at=SOON, ref="g")
    wallets.grant(user_id=world.uid, points=1, ref="topup")
    result = wallets.charge_turn(world.ctx(world.topic), [call(9)], "r1")
    assert (result.from_subsidy, result.from_balance, result.shortfall) == (1, 1, 7)
    assert result.wallet.available == 0
    with sessions() as db:
        short = db.scalar(select(LedgerEntry).where(LedgerEntry.kind == "shortfall"))
        assert (short.wallet_id, short.delta, short.workspace_id) == (None, -7, world.topic)


def test_expired_subsidy_cannot_be_spent_and_earliest_expiry_goes_first(world, wallets, spaces, sessions):
    wallets.grant(space_id=world.school, user_id=world.uid, points=100, expires_at=PAST, ref="old")
    wallets.grant(space_id=world.klass, user_id=world.uid, points=5, expires_at=LATER, ref="late")
    other_school = spaces.create_space("school", "乙学校")
    wallets.grant(space_id=other_school, user_id=world.uid, points=5, expires_at=SOON, ref="soon")
    assert wallets.view(world.ctx()).available == 10
    wallets.charge_turn(world.ctx(), [call(6)], "r1")
    assert balances(sessions) == {(world.uid, world.school): 100, (world.uid, world.klass): 4, (world.uid, other_school): 0}


def test_repeated_grants_top_up_the_same_wallet(world, wallets, sessions):
    wallets.grant(space_id=world.school, user_id=world.uid, points=5, expires_at=SOON, ref="g1")
    wallets.grant(space_id=world.school, user_id=world.uid, points=7, expires_at=LATER, ref="g2")
    with sessions() as db:
        wallet = db.scalar(select(Wallet))
        assert (wallet.balance, wallet.granted_total, wallet.expires_at) == (12, 12, LATER)
        assert db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "grant")) == 2


def test_usage_is_tagged_with_person_space_task_and_purpose(world, wallets, sessions):
    wallets.grant(space_id=world.school, user_id=world.uid, points=100, expires_at=SOON, ref="g")
    wallets.charge_turn(world.ctx(world.topic), [ModelCall("m", 1000, 100), ModelCall("m", 2000, 200)], "r1", purpose="grade")
    wallets.charge_turn(world.ctx(), [call(1)], "r2")
    with sessions() as db:
        rows = db.scalars(select(UsageRecord).order_by(UsageRecord.id)).all()
        assert [(r.user_id, r.space_id, r.workspace_id, r.purpose) for r in rows] == [
            (world.uid, world.klass, world.topic, "grade"), (world.uid, world.klass, world.topic, "grade"),
            (world.uid, None, None, "chat")]
        assert rows[0].total_tokens == 1100
        charge = db.scalars(select(LedgerEntry).where(LedgerEntry.kind == "charge")).first()
        assert (charge.user_id, charge.space_id, charge.workspace_id, charge.ref) == (world.uid, world.klass, world.topic, "r1")


def test_report_groups_by_space_task_and_person(world, wallets, sessions):
    wallets.charge_turn(world.ctx(world.topic), [ModelCall("m", 1000, 100), ModelCall("m", 2000, 200)], "r1")
    wallets.charge_turn(world.ctx(world.project), [call(1)], "r2")
    wallets.charge_turn(world.ctx(), [call(1)], "r3")
    rows = {(r.space, r.workspace): r for r in usage_by_space(sessions)}
    assert set(rows) == {("甲学校 / 一班", "沙盘"), ("某市孵化器 / 晓光工作室", "产业园数字沙盘"), ("", "")}
    row = rows[("甲学校 / 一班", "沙盘")]
    assert (row.user_id, row.calls, row.input_tokens, row.output_tokens, row.total_tokens) == (world.uid, 2, 3000, 300, 3300)
    assert isinstance(row.cost_yuan, float) and row.cost_yuan > 0


def test_stale_context_is_checked_again_when_charging(world, wallets, spaces):
    ctx = world.ctx(world.topic)
    spaces.leave(world.uid, world.klass)
    with pytest.raises(NotMember):
        wallets.charge_turn(ctx, [call(1)], "r1")


def test_spend_cap_counts_every_call_including_system_ones(world, wallets):
    # 测试用成本：输入每千 token 0.001 元
    assert wallets.spent_yuan() == 0
    wallets.charge_turn(world.ctx(), [ModelCall("m", 40_000, 0)], "r1")
    wallets.record_system_usage([ModelCall("m", 20_000, 0)], "warmup", "boot")
    assert wallets.spent_yuan() == pytest.approx(0.06)
    wallets.check_spend_cap(0.07)
    wallets.check_spend_cap(None)
    with pytest.raises(SpendCapReached):
        wallets.check_spend_cap(0.06)
