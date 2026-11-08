"""账号、班级、点数账本和模型用量的数据表。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class School(Base):
    __tablename__ = "school"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Klass(Base):
    __tablename__ = "klass"
    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("school.id"))
    name: Mapped[str] = mapped_column(String(100))
    school: Mapped[School] = relationship()


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    feishu_open_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    klass_id: Mapped[int] = mapped_column(ForeignKey("klass.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    klass: Mapped[Klass] = relationship()
    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)


class Wallet(Base):
    """点数钱包：学校补贴优先扣除、到期作废；个人余额长期有效。"""

    __tablename__ = "wallet"
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), primary_key=True)
    subsidy: Mapped[int] = mapped_column(Integer, default=0)
    subsidy_total: Mapped[int] = mapped_column(Integer, default=0)
    subsidy_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    user: Mapped[User] = relationship(back_populates="wallet")


class LedgerEntry(Base):
    """点数流水，每一次变动一条，只增不改。"""

    __tablename__ = "ledger_entry"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # grant / charge
    subsidy_delta: Mapped[int] = mapped_column(Integer, default=0)
    balance_delta: Mapped[int] = mapped_column(Integer, default=0)
    shortfall: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UsageRecord(Base):
    """每次模型调用一条，学校和班级在写入时固化，学生转班不影响历史统计。"""

    __tablename__ = "usage_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    klass_id: Mapped[int] = mapped_column(ForeignKey("klass.id"), index=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("school.id"), index=True)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


def make_session_factory(database_url: str) -> sessionmaker:
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
