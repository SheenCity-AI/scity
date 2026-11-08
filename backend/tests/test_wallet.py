from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from scity.models import LedgerEntry, UsageRecord, Wallet, make_session_factory
from scity.report import usage_by_student
from scity.settings import DEMO_PRICING
from scity.wallet import DEMO_BALANCE, DEMO_SUBSIDY, ModelCall, WalletService, points_for


@pytest.fixture
def sessions(tmp_path):
    return make_session_factory(f"sqlite:///{tmp_path}/t.db")


@pytest.fixture
def service(sessions):
    return WalletService(sessions, DEMO_PRICING)


def set_wallet(sessions, user_id, **fields):
    with sessions.begin() as db:
        wallet = db.get(Wallet, user_id)
        for key, value in fields.items():
            setattr(wallet, key, value)


def test_points_round_up_and_use_model_rate():
    pricing = {"default": {"input_per_1k": 1, "output_per_1k": 3}, "m2": {"input_per_1k": 10, "output_per_1k": 10}}
    assert points_for(ModelCall("m1", 1000, 1000), pricing) == 4
    assert points_for(ModelCall("m1", 1, 0), pricing) == 1
    assert points_for(ModelCall("m1", 0, 0), pricing) == 0
    assert points_for(ModelCall("m2", 1000, 0), pricing) == 10


def test_enroll_only_once(service):
    first = service.ensure_user("ou_a")
    assert service.ensure_user("ou_a") == first
    assert service.view(first).available == DEMO_SUBSIDY + DEMO_BALANCE


def test_subsidy_is_spent_before_balance(service, sessions):
    uid = service.ensure_user("ou_a")
    set_wallet(sessions, uid, subsidy=3, balance=10)
    result = service.charge_turn(uid, [ModelCall("m", 5000, 0)], "r1")
    assert (result.points, result.from_subsidy, result.from_balance, result.shortfall) == (5, 3, 2, 0)
    assert (result.wallet.subsidy, result.wallet.balance) == (0, 8)


def test_never_goes_negative_and_records_shortfall(service, sessions):
    uid = service.ensure_user("ou_a")
    set_wallet(sessions, uid, subsidy=1, balance=1)
    result = service.charge_turn(uid, [ModelCall("m", 9000, 0)], "r1")
    assert (result.from_subsidy, result.from_balance, result.shortfall) == (1, 1, 7)
    assert result.wallet.available == 0
    with sessions() as db:
        assert db.scalar(select(LedgerEntry).where(LedgerEntry.kind == "charge")).shortfall == 7


def test_expired_subsidy_cannot_be_spent(service, sessions):
    uid = service.ensure_user("ou_a")
    set_wallet(sessions, uid, subsidy=100, balance=10, subsidy_expires_at=datetime.utcnow() - timedelta(days=1))
    assert service.view(uid).available == 10
    result = service.charge_turn(uid, [ModelCall("m", 4000, 0)], "r1")
    assert (result.from_subsidy, result.from_balance) == (0, 4)


def test_each_model_call_is_recorded_and_reported(service, sessions):
    uid = service.ensure_user("ou_a")
    service.charge_turn(uid, [ModelCall("m", 1000, 100), ModelCall("m", 2000, 200)], "r1")
    with sessions() as db:
        assert len(db.scalars(select(UsageRecord)).all()) == 2
    row = usage_by_student(sessions)[0]
    assert (row.calls, row.input_tokens, row.output_tokens, row.total_tokens) == (2, 3000, 300, 3300)
    assert row.school == "演示学校"
