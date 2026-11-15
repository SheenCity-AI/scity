"""演示阶段的自动开户：第一次私聊机器人时，放进演示班级并发放演示点数。

正式开课由官方后台组建课程空间、设定老师和学生，不走这里。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .models import LedgerEntry, User
from .spaces import SpaceService
from .wallet import WalletService

DEMO_SCHOOL = "演示学校"
DEMO_KLASS = "筑年 · 2026 级 1 班"
DEMO_SUBSIDY = 3000
DEMO_BALANCE = 260
DEMO_SUBSIDY_EXPIRES = datetime(2027, 1, 31, 16, 0)  # 北京时间 2027-02-01 零点
ENROLL_REF = "demo-enroll"
SUBSIDY_REF = "demo-enroll-subsidy"


class DemoEnrollment:
    def __init__(self, sessions: sessionmaker, spaces: SpaceService, wallet: WalletService):
        self._sessions = sessions
        self._spaces = spaces
        self._wallet = wallet

    def _demo_spaces(self) -> tuple[int, int]:
        school = self._spaces.find_space("school", DEMO_SCHOOL) or self._spaces.create_space("school", DEMO_SCHOOL)
        klass = (self._spaces.find_space("class", DEMO_KLASS, school)
                 or self._spaces.create_space("class", DEMO_KLASS, school))
        return school, klass

    def _granted(self, open_id: str, ref: str) -> int | None:
        with self._sessions() as db:
            return db.scalar(select(User.id).join(LedgerEntry, LedgerEntry.user_id == User.id)
                             .where(User.feishu_open_id == open_id, LedgerEntry.ref == ref).limit(1))

    def enroll(self, open_id: str) -> int:
        # 个人余额最后发，它的流水就是“已开户”的标记；中途失败的，下次从断的地方接着做
        user_id = self._granted(open_id, ENROLL_REF)
        if user_id is not None:
            return user_id
        user_id = self._spaces.ensure_user(open_id)
        school, klass = self._demo_spaces()
        self._spaces.add_member(user_id, klass, "student")
        if self._granted(open_id, SUBSIDY_REF) is None:
            self._wallet.grant(DEMO_SUBSIDY, SUBSIDY_REF, user_id=user_id, space_id=school,
                               expires_at=DEMO_SUBSIDY_EXPIRES)
        self._wallet.grant(DEMO_BALANCE, ENROLL_REF, user_id=user_id)
        return user_id
