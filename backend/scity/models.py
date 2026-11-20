"""数据表：人、空间、成员关系、工作区、钱包、点数流水、模型用量，课题引擎的一局课、组、填报和事件，
以及知识图谱的总图（专业目录、知识领域、知识点）。

人不属于任何空间；班级、学校、孵化器、OPC、项目组都是空间，可以有上下级。
一个课题或一个项目是空间下的一个工作区；不在任何工作区里就是主聊天。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint,
                        create_engine, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

LEARNING_SPACES = ("school", "class")
BUSINESS_SPACES = ("incubator", "opc", "project_team")
ROLES = {"learning": ("student", "teacher"), "business": ("member", "manager")}
WORKSPACE_KINDS = {"learning": "topic", "business": "project"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def space_category(kind: str) -> str:
    return "learning" if kind in LEARNING_SPACES else "business"


class Base(DeclarativeBase):
    pass


class User(Base):
    """人。档案跟人走，不随班级或公司变化。"""

    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    feishu_open_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    major_id: Mapped[int | None] = mapped_column(ForeignKey("major.id"), nullable=True)  # 本人的专业（含层次）；知识图以它为中心分圈
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Space(Base):
    __tablename__ = "space"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("space.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    parent: Mapped["Space | None"] = relationship(remote_side=[id])

    @property
    def category(self) -> str:
        return space_category(self.kind)


class Membership(Base):
    """一个人在一个空间里的角色。离开后保留记录，active 置否。"""

    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("user_id", "space_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("space.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(default=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Workspace(Base):
    """工作区：空间里的一个课题或一个项目。归档后只读。"""

    __tablename__ = "workspace"
    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("space.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # topic / project
    name: Mapped[str] = mapped_column(String(100))
    course_ref: Mapped[str] = mapped_column(String(100), default="")  # 引用的课程包及版本
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    space: Mapped[Space] = relationship()


class Wallet(Base):
    """点数钱包，三种：

    只有 user_id：个人钱包，长期有效；
    user_id 和 space_id 都有：空间发给个人的额度（学校补贴、孵化器分配），可以有到期时间；
    只有 space_id：空间公用的额度（项目预算）。
    """

    __tablename__ = "wallet"
    __table_args__ = (
        CheckConstraint("user_id IS NOT NULL OR space_id IS NOT NULL", name="wallet_has_owner"),
        Index("wallet_personal", "user_id", unique=True, sqlite_where=text("space_id IS NULL"),
              postgresql_where=text("space_id IS NULL")),
        Index("wallet_allocation", "user_id", "space_id", unique=True,
              sqlite_where=text("user_id IS NOT NULL AND space_id IS NOT NULL"),
              postgresql_where=text("user_id IS NOT NULL AND space_id IS NOT NULL")),
        Index("wallet_pool", "space_id", unique=True, sqlite_where=text("user_id IS NULL"),
              postgresql_where=text("user_id IS NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("space.id"), nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    granted_total: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    space: Mapped[Space | None] = relationship()


class LedgerEntry(Base):
    """点数流水，每个钱包每次变动一条，只增不改。

    kind：grant 发放 / charge 扣点 / shortfall 点数不够时没扣到的差额（wallet_id 为空）。
    user_id、space_id、workspace_id 记的是这笔变动发生在谁、哪个空间、哪个工作区。
    """

    __tablename__ = "ledger_entry"
    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallet.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    delta: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True, index=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("space.id"), nullable=True, index=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspace.id"), nullable=True, index=True)
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UsageRecord(Base):
    """每次模型调用一条：谁、在哪个空间、哪个工作区、做什么用。

    主聊天的 space_id、workspace_id 为空；系统自己的调用（如启动热身）user_id 也为空。
    cost_micro 是按模型成本价估算的费用，单位百万分之一元，用于累计费用上限。
    """

    __tablename__ = "usage_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True, index=True)
    space_id: Mapped[int | None] = mapped_column(ForeignKey("space.id"), nullable=True, index=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspace.id"), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(20), default="chat")
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Game(Base):
    """一局课：属于一个课题工作区。stage 为空表示还没开始或已经走完（看 finished_at）。"""

    __tablename__ = "game"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id"), index=True)
    course_ref: Mapped[str] = mapped_column(String(100))
    seed: Mapped[str] = mapped_column(String(64), default="")
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stage_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)  # 每次有变化加 1，各台电脑靠它知道要不要刷新
    last_lesson: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 体验班：只开到第几次课；空着表示上完整门课
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Team(Base):
    __tablename__ = "team"
    __table_args__ = (UniqueConstraint("game_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("game.id"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    company: Mapped[str] = mapped_column(String(64), default="")
    npc: Mapped[bool] = mapped_column(default=False)  # 系统控制的对手


class TeamMember(Base):
    __tablename__ = "team_member"
    __table_args__ = (UniqueConstraint("game_id", "user_id"),)  # 一个人在一局里只在一个组
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("game.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    post: Mapped[str] = mapped_column(String(20), default="")


class Decision(Base):
    """一个组在一个阶段的填报。只增不改，同一阶段以最后一次为准。user_id 为空表示截止时系统代填的默认选项。"""

    __tablename__ = "decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("game.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GameEvent(Base):
    """老师的操作和系统算出的结果，只增不改。

    kind：open 开启阶段 / extend 延长 / record 老师录入 / result 关闭阶段时课程插件算出的结果 /
    talk 一个组和课里的 AI 角色的一问一答（payload 里有 team、q、a）。
    """

    __tablename__ = "game_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("game.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    stage: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 知识图谱的总图：专业目录、知识领域、知识点 ----------
# 每个学生的知识图是这张总图上被点亮的部分。专业目录用教育部的本科和职业教育两套；知识点只存一份，可以同时挂多个专业。

MAJOR_LEVELS = ("本科", "高职专科", "职业本科")


class Major(Base):
    """教育部专业目录里的一个专业。本科按“门类 → 专业类 → 专业”，高职按“专业大类 → 专业类 → 专业”，这里都记成三层。"""

    __tablename__ = "major"
    __table_args__ = (UniqueConstraint("level", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(10))            # 本科 / 高职专科 / 职业本科
    code: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(60))
    category_code: Mapped[str] = mapped_column(String(8), default="")
    category: Mapped[str] = mapped_column(String(40), default="")   # 学科门类 / 专业大类
    class_code: Mapped[str] = mapped_column(String(8), default="")
    class_name: Mapped[str] = mapped_column(String(60), default="")  # 专业类；个别门类下没有
    cluster: Mapped[str] = mapped_column(String(40), default="")     # 我们划的专业群，相近专业按它来；没划的为空
    source: Mapped[str] = mapped_column(String(60), default="")      # 目录版本


class MajorNeighbor(Base):
    """相近专业：一行一对。以学生本人的专业为中心分三圈时，本专业和相近专业算第一圈。"""

    __tablename__ = "major_neighbor"
    __table_args__ = (UniqueConstraint("major_id", "neighbor_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    major_id: Mapped[int] = mapped_column(ForeignKey("major.id"), index=True)
    neighbor_id: Mapped[int] = mapped_column(ForeignKey("major.id"))
    basis: Mapped[str] = mapped_column(String(40), default="")


class KnowledgeDomain(Base):
    """知识领域（如“融资拿地”），上面还有一层大领域（如“城市开发与更新”，代码 KF）。"""

    __tablename__ = "knowledge_domain"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True)       # KF.2
    name: Mapped[str] = mapped_column(String(40))
    group_code: Mapped[str] = mapped_column(String(6), index=True)   # KF
    group_name: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(40), default="")


class KnowledgePoint(Base):
    """知识点：图上的一个泡泡，粗细约等于一张知识卡。名词不单独成泡泡，记在关键词里。"""

    __tablename__ = "knowledge_point"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)       # K-KF-004
    name: Mapped[str] = mapped_column(String(60))
    domain_id: Mapped[int] = mapped_column(ForeignKey("knowledge_domain.id"), index=True)
    summary: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(60), default="")      # 来自哪个课程包或通识包
    status: Mapped[str] = mapped_column(String(40), default="")      # 核对状态


class KnowledgeKeyword(Base):
    """知识点的关键词：聊到“容积率”，认作在聊“规划条件指标”。"""

    __tablename__ = "knowledge_keyword"
    __table_args__ = (UniqueConstraint("point_id", "word"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("knowledge_point.id"), index=True)
    word: Mapped[str] = mapped_column(String(40), index=True)


class KnowledgeLink(Base):
    """相关的知识点：聚焦一个知识点时，显示和它相关联的知识点。"""

    __tablename__ = "knowledge_link"
    __table_args__ = (UniqueConstraint("point_id", "related_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("knowledge_point.id"), index=True)
    related_id: Mapped[int] = mapped_column(ForeignKey("knowledge_point.id"))


class KnowledgePointMajor(Base):
    """知识点属于哪些专业（本科和高职都可以，一个知识点可以挂多个）。"""

    __tablename__ = "knowledge_point_major"
    __table_args__ = (UniqueConstraint("point_id", "major_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    point_id: Mapped[int] = mapped_column(ForeignKey("knowledge_point.id"), index=True)
    major_id: Mapped[int] = mapped_column(ForeignKey("major.id"), index=True)


class KnowledgeMark(Base):
    """一个人在一局课里点亮的一个知识点（实线泡泡的依据）。老师确认分数时写入，按当时的小组成员记到每个人名下。

    只存知识点编号、分数、次数、时长和日期，不存对话原文。score 是老师给所在板块打的分（10 分制），
    只有课程包里标为“展开”的知识点有；“点到”的为空。
    """

    __tablename__ = "knowledge_mark"
    __table_args__ = (UniqueConstraint("user_id", "point_code", "game_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    point_code: Mapped[str] = mapped_column(String(20), index=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("game.id"), index=True)
    course_ref: Mapped[str] = mapped_column(String(100))  # 课程包及版本
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    touches: Mapped[int] = mapped_column(Integer, default=1)   # 接触次数
    minutes: Mapped[float] = mapped_column(Float, default=0.0)  # 学习时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def make_session_factory(database_url: str, create: bool = False) -> sessionmaker:
    """create=True 时直接按当前定义建表，只用于测试和本地试用；正式环境用 alembic 升级。"""
    engine = create_engine(database_url)
    if create:
        Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
