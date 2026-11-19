"""课程插件的接口：通用引擎只认识阶段、填报和结果，课程的规则都在插件里。

插件要提供：
  ref                                   课程包及版本，如 "sandbox@0.1"
  stages                                按顺序排好的全部阶段
  validate(stage, payload, state, team) 校验一个组的填报，返回要保存的内容；不合规就抛 ValueError（中文原因）
  default(stage, state, team)           截止时没交的组用什么；返回 None 表示视为没填
  validate_record(stage, payload, state) 校验老师录入的内容（如现场举牌结果）
  close(stage, state)                   关闭阶段时算出结果，返回 None 表示这个阶段没有结果
  view(state, team, role)               这个身份此刻能看到的课程内容；保密的东西不要放进学生的视图
可选：
  required_teams(stage, state)          这个阶段要等哪些组提交（默认全部学生组）
  brief(state, team, role)              给工作区里的助手看的现场摘要（文字）；同样只放这个身份能看到的内容
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path


def load_courses(private_dir: Path) -> dict:
    """装载私有目录 courses/<名称>/plugin.py 里的课程插件（每个文件给出一个 COURSE）。"""
    courses = {}
    for path in sorted(Path(private_dir).glob("courses/*/plugin.py")):
        spec = importlib.util.spec_from_file_location(f"scity_course_{path.parent.name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        courses[module.COURSE.ref] = module.COURSE
    return courses


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
    stage_minutes: float = 0.0  # 当前阶段已经开了多少分钟（按服务器时间），课程可据此按时推送快讯
    decisions: dict[str, dict[str, dict]] = field(default_factory=dict)  # 阶段 → 组 → 最后一次填报
    results: dict[str, dict] = field(default_factory=dict)               # 阶段 → 关闭时算出的结果
    records: dict[str, list[dict]] = field(default_factory=dict)         # 阶段 → 老师的录入，按时间顺序
    defaulted: set[tuple[str, str]] = field(default_factory=set)         # (阶段, 组)：这一步学生没交，现在生效的是截止时系统代填的
    _history: dict[tuple[str, str], list[dict]] = field(default_factory=dict)

    def team(self, name: str) -> TeamInfo:
        return next(t for t in self.teams if t.name == name)

    def history(self, stage: str, team: str) -> list[dict]:
        return self._history.get((stage, team), [])
