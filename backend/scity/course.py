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
  knowledge(state, team)                这个组到现在凭老师的评分点亮了哪些知识点：{"scores": {知识点编号: 分}, "touched": [知识点编号]}。
                                        scores 是“展开”的知识点和所在板块的分（10 分制），touched 是只“点到”的。老师每次保存评分后，
                                        平台把它记到这个组每个成员的知识图上。还可以给 "touches": {编号: 接触次数} 和
                                        "minutes": {编号: 课上学习的分钟数}，决定泡泡颜色的深浅；不给就按接触 1 次算
  role(stage, state, team)              这个阶段里学生可以对话的 AI 角色（如居民代表）；没有就返回 None。返回
                                        {"name": 称呼, "prompt": 给模型的设定, "turns": 每组最多问几次}。
                                        对话只是让学生听到角色的反应，结果仍由 close 按规则算；设定里不要放学生不该知道的内容
"""
from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path


log = logging.getLogger("scity.web")


def load_courses(private_dir: Path) -> dict:
    """装载私有目录 courses/ 下的课程包。两种：

    courses/<名称>/plugin.py   课程插件（写了程序的，文件里给出一个 COURSE）
    courses/<名称>/课程.md      内容型课程包（不写程序，见 content_course.py）；同一个文件夹里有 plugin.py 时以插件为准
    内容型课程包写得不对时只跳过这一个并记日志，不影响别的课。
    """
    from .content_course import COURSE_FILE, ContentCourse, CourseFormatError

    courses = {}
    for path in sorted(Path(private_dir).glob("courses/*/plugin.py")):
        spec = importlib.util.spec_from_file_location(f"scity_course_{path.parent.name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        courses[module.COURSE.ref] = module.COURSE
    for path in sorted(Path(private_dir).glob(f"courses/*/{COURSE_FILE}")):
        if (path.parent / "plugin.py").exists():
            continue
        try:
            course = ContentCourse(path.parent)
        except CourseFormatError as exc:
            log.error("课程包 %s 没有装上：%s", path.parent.name, exc)
            continue
        if course.ref in courses:
            log.error("课程包 %s 没有装上：编号和版本 %s 已经被别的课程包用了", path.parent.name, course.ref)
            continue
        courses[course.ref] = course
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
    spent: dict[str, float] = field(default_factory=dict)                # 阶段 → 实际上了多少分钟（开过的阶段才有；当前阶段算到此刻）
    talks: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # 阶段 → 组 → 和 AI 角色的问答 [{"q", "a"}]，按时间顺序
    _history: dict[tuple[str, str], list[dict]] = field(default_factory=dict)

    def team(self, name: str) -> TeamInfo:
        return next(t for t in self.teams if t.name == name)

    def history(self, stage: str, team: str) -> list[dict]:
        return self._history.get((stage, team), [])
