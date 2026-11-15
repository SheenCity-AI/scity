"""用量统计：按空间、工作区、人汇总 token 和点数。用法：python -m scity.report"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .models import Space, UsageRecord, Workspace, make_session_factory
from .settings import load_settings


@dataclass(frozen=True)
class UsageRow:
    space: str      # 含上级，如“甲学校 / 一班”；主聊天和系统调用为空
    workspace: str
    user_id: int | None
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    points: int
    cost_yuan: float


def usage_by_space(sessions: sessionmaker) -> list[UsageRow]:
    keys = (UsageRecord.space_id, UsageRecord.workspace_id, UsageRecord.user_id)
    stmt = select(*keys, func.count(), func.sum(UsageRecord.input_tokens), func.sum(UsageRecord.output_tokens),
                  func.sum(UsageRecord.total_tokens), func.sum(UsageRecord.points),
                  func.sum(UsageRecord.cost_micro)).group_by(*keys)
    with sessions() as db:
        spaces = {s.id: s for s in db.scalars(select(Space))}
        workspaces = {w.id: w.name for w in db.scalars(select(Workspace))}

        def path(space_id: int | None) -> str:
            names = []
            while space_id is not None:
                names.insert(0, spaces[space_id].name)
                space_id = spaces[space_id].parent_id
            return " / ".join(names)

        rows = [UsageRow(path(space_id), workspaces.get(workspace_id, ""), user_id, calls, int(tin), int(tout),
                         int(total), int(points),
                         int(cost) / 1_000_000)
                for space_id, workspace_id, user_id, calls, tin, tout, total, points, cost in db.execute(stmt)]
    return sorted(rows, key=lambda r: (r.space, r.workspace, r.user_id or 0))


def main() -> None:
    rows = usage_by_space(make_session_factory(load_settings().database_url))
    print("空间\t工作区\t用户ID\t调用次数\t输入token\t输出token\t总token\t点数\t估算费用(元)")
    for r in rows:
        print(f"{r.space or '（主聊天或系统）'}\t{r.workspace}\t{r.user_id or ''}\t{r.calls}\t{r.input_tokens}\t"
              f"{r.output_tokens}\t{r.total_tokens}\t{r.points}\t{r.cost_yuan:.4f}")


if __name__ == "__main__":
    main()
