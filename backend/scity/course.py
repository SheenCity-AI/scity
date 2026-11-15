"""课程插件的接口：通用引擎只认识阶段、填报和结果，课程的规则都在插件里。

插件要提供：
  ref                                   课程包及版本，如 "sandbox@0.1"
  stages                                按顺序排好的全部阶段
  validate(stage, payload, state, team) 校验一个组的填报，返回要保存的内容；不合规就抛 ValueError（中文原因）
  default(stage, state, team)           截止时没交的组用什么；返回 None 表示视为没填
  validate_record(stage, payload, state) 校验老师录入的内容（如现场举牌结果）
  close(stage, state)                   关闭阶段时算出结果，返回 None 表示这个阶段没有结果
  view(state, team, role)               这个身份此刻能看到的课程内容；保密的东西不要放进学生的视图
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stage:
    key: str
    name: str
    lesson: int
    minutes: int | None = None  # 有时长的阶段开启后倒计时，到点不再接受填报
    ends: str = "teacher"       # teacher 老师随时结束 / timer 必须等倒计时到 / timer_or_all 到点，或全部提交后老师提前结束
    form: bool = False          # 各组是否要填报
    extend_minutes: int = 0     # 老师最多可以延长多少分钟


@dataclass(frozen=True)
class TeamInfo:
    name: str
    company: str
    npc: bool
    members: tuple[int, ...]


@dataclass
class GameState:
    """一局课此刻的全部事实。结果不另存，课程插件用它从头算。"""

    seed: str
    stage: str | None
    teams: list[TeamInfo]
    decisions: dict[str, dict[str, dict]] = field(default_factory=dict)  # 阶段 → 组 → 最后一次填报
    results: dict[str, dict] = field(default_factory=dict)               # 阶段 → 关闭时算出的结果
    records: dict[str, list[dict]] = field(default_factory=dict)         # 阶段 → 老师的录入，按时间顺序
    _history: dict[tuple[str, str], list[dict]] = field(default_factory=dict)

    def team(self, name: str) -> TeamInfo:
        return next(t for t in self.teams if t.name == name)

    def history(self, stage: str, team: str) -> list[dict]:
        return self._history.get((stage, team), [])
