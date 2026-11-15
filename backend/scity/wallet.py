"""点数账本：发放、余额、按模型用量扣点，以及累计费用上限。

扣点顺序由对话发生在哪决定：
  主聊天和课题工作区：学校一类空间发给本人的额度（先到期的先用）→ 个人钱包；
  项目工作区：本空间和上级空间发给本人的额度 → 这些空间的公用额度。学校的钱和个人钱包都不动。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import LEARNING_SPACES, LedgerEntry, Space, UsageRecord, Wallet, utcnow
from .spaces import Context, resolve_context


class SpendCapReached(RuntimeError):
    """累计费用到了上限，不再调用模型。"""


@dataclass(frozen=True)
class ModelCall:
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class WalletView:
    """当前场合能用的点数：subsidy 是空间出的钱，balance 是个人钱包。"""

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


def _rate(table: dict, model: str) -> dict:
    return table.get(model) or table["default"]


def points_for(call: ModelCall, pricing: dict) -> int:
    rate = _rate(pricing, call.model)
    cost = call.input_tokens / 1000 * rate["input_per_1k"] + call.output_tokens / 1000 * rate["output_per_1k"]
    return math.ceil(cost) if cost > 0 else 0


def cost_micro_for(call: ModelCall, costs: dict) -> int:
    rate = _rate(costs, call.model)
    yuan = call.input_tokens / 1000 * rate["input_per_1k"] + call.output_tokens / 1000 * rate["output_per_1k"]
    return math.ceil(yuan * 1_000_000)


def _ancestors(db: Session, space_id: int) -> list[int]:
    """本空间和各级上级，近的在前。"""
    chain: list[int] = []
    while space_id is not None and space_id not in chain:
        chain.append(space_id)
        space_id = db.get(Space, space_id).parent_id
    return chain


def _sources(db: Session, ctx: Context, now: datetime, lock: bool = False) -> list[Wallet]:
    """这个场合可以扣的钱包，按扣点顺序排好。"""
    usable = (Wallet.expires_at.is_(None)) | (Wallet.expires_at > now)
    if ctx.category == "business":
        chain = _ancestors(db, ctx.space_id)
        stmt = select(Wallet).where(Wallet.space_id.in_(chain), usable,
                                    (Wallet.user_id == ctx.user_id) | Wallet.user_id.is_(None))
        rank = lambda w: (w.user_id is None, chain.index(w.space_id))
    else:
        stmt = (select(Wallet).outerjoin(Space, Space.id == Wallet.space_id)
                .where(Wallet.user_id == ctx.user_id, usable,
                       Wallet.space_id.is_(None) | Space.kind.in_(LEARNING_SPACES)))
        rank = lambda w: (w.space_id is None, w.expires_at is None, w.expires_at or now, w.id)
    if lock:
        # 固定按编号加锁，避免两笔扣点互相等待
        stmt = stmt.order_by(Wallet.id).with_for_update(of=Wallet)
    return sorted(db.scalars(stmt), key=rank)


def _view(wallets: list[Wallet]) -> WalletView:
    return WalletView(subsidy=sum(w.balance for w in wallets if w.space_id is not None),
                      balance=sum(w.balance for w in wallets if w.space_id is None))


class WalletService:
    def __init__(self, sessions: sessionmaker, pricing: dict, costs: dict):
        self._sessions = sessions
        self._pricing = pricing
        self._costs = costs

    def grant(self, points: int, ref: str, user_id: int | None = None, space_id: int | None = None,
              expires_at: datetime | None = None) -> None:
        """发放点数。同一个人在同一个空间只有一个额度钱包，再发就累加，到期时间以最新一次为准。"""
        if points <= 0:
            raise ValueError("发放的点数必须大于 0")
        owner = (Wallet.user_id == user_id if user_id is not None else Wallet.user_id.is_(None),
                 Wallet.space_id == space_id if space_id is not None else Wallet.space_id.is_(None))
        with self._sessions.begin() as db:
            wallet = db.scalar(select(Wallet).where(*owner).with_for_update())
            if wallet is None:
                wallet = Wallet(user_id=user_id, space_id=space_id, balance=0, granted_total=0)
                db.add(wallet)
                db.flush()
            wallet.balance += points
            wallet.granted_total += points
            if expires_at is not None:
                wallet.expires_at = expires_at
            db.add(LedgerEntry(wallet_id=wallet.id, kind="grant", delta=points, user_id=user_id,
                               space_id=space_id, ref=ref))

    def view(self, ctx: Context) -> WalletView:
        with self._sessions() as db:
            return _view(_sources(db, ctx, utcnow()))

    def _record(self, db: Session, ctx: Context | None, calls: list[ModelCall], purpose: str, ref: str) -> int:
        total = 0
        for call in calls:
            points = points_for(call, self._pricing) if ctx else 0
            total += points
            db.add(UsageRecord(user_id=ctx and ctx.user_id, space_id=ctx and ctx.space_id,
                               workspace_id=ctx and ctx.workspace_id, purpose=purpose, model=call.model,
                               input_tokens=call.input_tokens, output_tokens=call.output_tokens,
                               total_tokens=call.input_tokens + call.output_tokens, points=points,
                               cost_micro=cost_micro_for(call, self._costs), ref=ref))
        return total

    def charge_turn(self, ctx: Context, calls: list[ModelCall], ref: str, purpose: str = "chat") -> ChargeResult:
        """一轮对话结算。

        回答已经生成，点数不够时扣到 0，差额记一条 shortfall 流水，不倒扣成负数。
        扣点前重新核对成员身份，已经离开空间的人不能再花这个空间的钱。
        """
        now = utcnow()
        with self._sessions.begin() as db:
            ctx = resolve_context(db, ctx.user_id, ctx.workspace_id)
            where = {"user_id": ctx.user_id, "space_id": ctx.space_id, "workspace_id": ctx.workspace_id, "ref": ref}
            remaining = total = self._record(db, ctx, calls, purpose, ref)
            sources = _sources(db, ctx, now, lock=True)
            from_subsidy = from_balance = 0
            for wallet in sources:
                take = min(wallet.balance, remaining)
                if take == 0:
                    continue
                wallet.balance -= take
                remaining -= take
                if wallet.space_id is None:
                    from_balance += take
                else:
                    from_subsidy += take
                db.add(LedgerEntry(wallet_id=wallet.id, kind="charge", delta=-take, **where))
            if remaining:
                db.add(LedgerEntry(wallet_id=None, kind="shortfall", delta=-remaining, **where))
            view = _view(sources)
        return ChargeResult(total, from_subsidy, from_balance, remaining, view)

    def record_system_usage(self, calls: list[ModelCall], purpose: str, ref: str) -> None:
        """系统自己的模型调用（如启动热身）：不扣任何人的点数，但计入累计费用。"""
        with self._sessions.begin() as db:
            self._record(db, None, calls, purpose, ref)

    def spent_yuan(self) -> float:
        with self._sessions() as db:
            return int(db.scalar(select(func.sum(UsageRecord.cost_micro))) or 0) / 1_000_000

    def check_spend_cap(self, cap_yuan: float | None) -> None:
        if cap_yuan is not None and self.spent_yuan() >= cap_yuan:
            raise SpendCapReached(f"累计费用已到上限 {cap_yuan} 元")
