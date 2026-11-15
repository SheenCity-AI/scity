"""人、空间、成员关系和工作区：谁在哪个空间里是什么角色，由这里说了算。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import (BUSINESS_SPACES, LEARNING_SPACES, ROLES, WORKSPACE_KINDS, Membership, Space, User, Workspace,
                     space_category, utcnow)


class NotMember(PermissionError):
    """这个人不是该空间的在册成员。"""


class WorkspaceArchived(PermissionError):
    """工作区已归档，只能回看。"""


@dataclass(frozen=True)
class Context:
    """一次对话或操作发生在哪：谁、哪个空间、哪个工作区、什么角色。主聊天时后三项为空。"""

    user_id: int
    space_id: int | None = None
    workspace_id: int | None = None
    role: str | None = None
    category: str = "main"  # main / learning / business


@dataclass(frozen=True)
class MembershipView:
    space_id: int
    space_name: str
    kind: str
    role: str


@dataclass(frozen=True)
class WorkspaceView:
    id: int
    name: str
    kind: str
    category: str
    space_id: int
    archived: bool


def resolve_context(db: Session, user_id: int, workspace_id: int | None) -> Context:
    if workspace_id is None:
        if db.get(User, user_id) is None:
            raise NotMember(f"没有这个人：{user_id}")
        return Context(user_id=user_id)
    workspace = db.get(Workspace, workspace_id)
    member = workspace and db.scalar(select(Membership).where(
        Membership.user_id == user_id, Membership.space_id == workspace.space_id, Membership.active))
    if member is None:
        raise NotMember(f"用户 {user_id} 不在工作区 {workspace_id} 所属的空间里")
    if workspace.archived_at is not None:
        raise WorkspaceArchived(f"工作区 {workspace_id} 已归档")
    return Context(user_id, workspace.space_id, workspace.id, member.role, workspace.space.category)


class SpaceService:
    def __init__(self, sessions: sessionmaker):
        self._sessions = sessions

    def ensure_user(self, open_id: str, display_name: str = "") -> int:
        with self._sessions.begin() as db:
            user = db.scalar(select(User).where(User.feishu_open_id == open_id))
            if user is None:
                user = User(feishu_open_id=open_id, display_name=display_name)
                db.add(user)
                db.flush()
            return user.id

    def display_name(self, user_id: int) -> str:
        with self._sessions() as db:
            return db.get(User, user_id).display_name

    def space_path(self, space_id: int) -> str:
        """含上级的空间名，如“甲学校 / 一班”。"""
        names: list[str] = []
        with self._sessions() as db:
            while space_id is not None:
                space = db.get(Space, space_id)
                names.insert(0, space.name)
                space_id = space.parent_id
        return " / ".join(names)

    def set_display_name(self, user_id: int, name: str) -> None:
        with self._sessions.begin() as db:
            db.get(User, user_id).display_name = name

    def create_space(self, kind: str, name: str, parent_id: int | None = None) -> int:
        if kind not in LEARNING_SPACES + BUSINESS_SPACES:
            raise ValueError(f"不认识的空间类型：{kind}")
        with self._sessions.begin() as db:
            space = Space(kind=kind, name=name, parent_id=parent_id)
            db.add(space)
            db.flush()
            return space.id

    def find_space(self, kind: str, name: str, parent_id: int | None = None) -> int | None:
        with self._sessions() as db:
            return db.scalar(select(Space.id).where(Space.kind == kind, Space.name == name,
                                                    Space.parent_id.is_(None) if parent_id is None
                                                    else Space.parent_id == parent_id))

    def add_member(self, user_id: int, space_id: int, role: str) -> None:
        """加入空间；已在册就改角色，离开过的重新生效。一个人在一个空间里只有一个角色。"""
        with self._sessions.begin() as db:
            space = db.get(Space, space_id)
            if role not in ROLES[space.category]:
                raise ValueError(f"{space.kind} 空间里没有 {role} 这个角色")
            member = db.scalar(select(Membership).where(Membership.user_id == user_id, Membership.space_id == space_id))
            if member is None:
                db.add(Membership(user_id=user_id, space_id=space_id, role=role))
            else:
                member.role, member.active, member.left_at = role, True, None

    def leave(self, user_id: int, space_id: int) -> None:
        with self._sessions.begin() as db:
            member = db.scalar(select(Membership).where(Membership.user_id == user_id, Membership.space_id == space_id))
            if member is not None:
                member.active, member.left_at = False, utcnow()

    def memberships(self, user_id: int) -> list[MembershipView]:
        with self._sessions() as db:
            rows = db.execute(select(Membership, Space).join(Space, Space.id == Membership.space_id)
                              .where(Membership.user_id == user_id, Membership.active).order_by(Membership.id))
            return [MembershipView(s.id, s.name, s.kind, m.role) for m, s in rows]

    def create_workspace(self, space_id: int, kind: str, name: str, course_ref: str = "") -> int:
        with self._sessions.begin() as db:
            space = db.get(Space, space_id)
            if kind != WORKSPACE_KINDS[space.category]:
                raise ValueError(f"{space.kind} 空间里不能建 {kind} 工作区")
            workspace = Workspace(space_id=space_id, kind=kind, name=name, course_ref=course_ref)
            db.add(workspace)
            db.flush()
            return workspace.id

    def find_workspace(self, space_id: int, name: str) -> int | None:
        with self._sessions() as db:
            return db.scalar(select(Workspace.id).where(Workspace.space_id == space_id, Workspace.name == name))

    def archive_workspace(self, workspace_id: int) -> None:
        with self._sessions.begin() as db:
            db.get(Workspace, workspace_id).archived_at = utcnow()

    def workspaces(self, user_id: int, include_archived: bool = False) -> list[WorkspaceView]:
        """这个人的任务清单：所在空间里的全部工作区，只有名称和类别，没有内容。"""
        stmt = (select(Workspace, Space.kind).join(Space, Space.id == Workspace.space_id)
                .join(Membership, Membership.space_id == Workspace.space_id)
                .where(Membership.user_id == user_id, Membership.active).order_by(Workspace.id))
        if not include_archived:
            stmt = stmt.where(Workspace.archived_at.is_(None))
        with self._sessions() as db:
            return [WorkspaceView(w.id, w.name, w.kind, space_category(kind), w.space_id, w.archived_at is not None)
                    for w, kind in db.execute(stmt)]

    def context(self, user_id: int, workspace_id: int | None) -> Context:
        with self._sessions() as db:
            return resolve_context(db, user_id, workspace_id)
