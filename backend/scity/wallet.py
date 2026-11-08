"""点数账本：开户、余额、按模型用量扣点。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import Klass, LedgerEntry, School, UsageRecord, User, Wallet, utcnow

DEMO_SCHOOL = "演示学校"
DEMO_KLASS = "筑年 · 2026 级 1 班"
DEMO_SUBSIDY = 3000
DEMO_BALANCE = 260
DEMO_SUBSIDY_EXPIRES = datetime(2027, 1, 31, 16, 0)  # 北京时间 2027-02-01 零点


@dataclass(frozen=True)
class ModelCall:
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class WalletView:
    subsidy: int
    balance: int

    @property
    def available(self) -> int:
        return self.subsidy + self.balance


@dataclass(frozen=True)
class ChargeResult:
    points: int
    from_subsidy: int
    from_balance: int
    shortfall: int
    wallet: WalletView


def points_for(call: ModelCall, pricing: dict) -> int:
    rate = pricing.get(call.model) or pricing["default"]
    cost = call.input_tokens / 1000 * rate["input_per_1k"] + call.output_tokens / 1000 * rate["output_per_1k"]
    return math.ceil(cost) if cost > 0 else 0


def _usable_subsidy(wallet: Wallet, now: datetime) -> int:
    expired = wallet.subsidy_expires_at is not None and wallet.subsidy_expires_at <= now
    return 0 if expired else wallet.subsidy


class WalletService:
    def __init__(self, sessions: sessionmaker, pricing: dict):
        self._sessions = sessions
        self._pricing = pricing

    def _demo_klass(self, db: Session) -> Klass:
        school = db.scalar(select(School).where(School.name == DEMO_SCHOOL))
        if school is None:
            school = School(name=DEMO_SCHOOL)
            db.add(school)
            db.flush()
        klass = db.scalar(select(Klass).where(Klass.school_id == school.id, Klass.name == DEMO_KLASS))
        if klass is None:
            klass = Klass(school_id=school.id, name=DEMO_KLASS)
            db.add(klass)
            db.flush()
        return klass

    def ensure_user(self, open_id: str) -> int:
        """第一次对话时开户，放进演示班级并发放演示点数。"""
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.feishu_open_id == open_id))
            if user is not None:
                return user.id
            user = User(feishu_open_id=open_id, klass_id=self._demo_klass(db).id)
            db.add(user)
            db.flush()
            db.add(Wallet(user_id=user.id, subsidy=DEMO_SUBSIDY, subsidy_total=DEMO_SUBSIDY,
                          subsidy_expires_at=DEMO_SUBSIDY_EXPIRES, balance=DEMO_BALANCE))
            db.add(LedgerEntry(user_id=user.id, kind="grant", subsidy_delta=DEMO_SUBSIDY,
                               balance_delta=DEMO_BALANCE, ref="demo-enroll"))
            return user.id

    def view(self, user_id: int) -> WalletView:
        with self._sessions() as db:
            wallet = db.get(Wallet, user_id)
            return WalletView(subsidy=_usable_subsidy(wallet, utcnow()), balance=wallet.balance)

    def charge_turn(self, user_id: int, calls: list[ModelCall], ref: str) -> ChargeResult:
        """一轮对话结算：先扣学校补贴、再扣个人余额。

        回答已经生成，点数不够时扣到 0，差额记在流水的 shortfall 里，不倒扣成负数。
        """
        now = utcnow()
        with self._sessions.begin() as db:
            user = db.get(User, user_id)
            wallet = db.scalar(select(Wallet).where(Wallet.user_id == user_id).with_for_update())
            total = 0
            for call in calls:
                points = points_for(call, self._pricing)
                total += points
                db.add(UsageRecord(user_id=user_id, klass_id=user.klass_id, school_id=user.klass.school_id,
                                   model=call.model, input_tokens=call.input_tokens,
                                   output_tokens=call.output_tokens,
                                   total_tokens=call.input_tokens + call.output_tokens,
                                   points=points, ref=ref))
            from_subsidy = min(_usable_subsidy(wallet, now), total)
            from_balance = min(wallet.balance, total - from_subsidy)
            shortfall = total - from_subsidy - from_balance
            wallet.subsidy -= from_subsidy
            wallet.balance -= from_balance
            db.add(LedgerEntry(user_id=user_id, kind="charge", subsidy_delta=-from_subsidy,
                               balance_delta=-from_balance, shortfall=shortfall, ref=ref))
            view = WalletView(subsidy=_usable_subsidy(wallet, now), balance=wallet.balance)
        return ChargeResult(total, from_subsidy, from_balance, shortfall, view)
