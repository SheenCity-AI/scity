"""用量统计：按学校、班级、学生汇总 token 和点数。用法：python -m scity.report"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .models import Klass, School, UsageRecord, User, make_session_factory
from .settings import load_settings


@dataclass(frozen=True)
class UsageRow:
    school: str
    klass: str
    user_id: int
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    points: int


def usage_by_student(sessions: sessionmaker) -> list[UsageRow]:
    stmt = (
        select(School.name, Klass.name, UsageRecord.user_id, func.count(), func.sum(UsageRecord.input_tokens),
               func.sum(UsageRecord.output_tokens), func.sum(UsageRecord.total_tokens), func.sum(UsageRecord.points))
        .join(School, School.id == UsageRecord.school_id)
        .join(Klass, Klass.id == UsageRecord.klass_id)
        .join(User, User.id == UsageRecord.user_id)
        .group_by(School.name, Klass.name, UsageRecord.user_id)
        .order_by(School.name, Klass.name, UsageRecord.user_id)
    )
    with sessions() as db:
        return [UsageRow(*row) for row in db.execute(stmt)]


def main() -> None:
    rows = usage_by_student(make_session_factory(load_settings().database_url))
    print("学校\t班级\t学生ID\t调用次数\t输入token\t输出token\t总token\t点数")
    for r in rows:
        print(f"{r.school}\t{r.klass}\t{r.user_id}\t{r.calls}\t{r.input_tokens}\t{r.output_tokens}\t{r.total_tokens}\t{r.points}")


if __name__ == "__main__":
    main()
