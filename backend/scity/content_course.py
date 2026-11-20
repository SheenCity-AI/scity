"""内容型课程包：不用写程序，一个文件夹里放几份文件就能上课。

文件夹里的文件（只有第一份必须有）：
  课程.md       名称、编号、版本，每次课有哪些阶段，每个阶段的资料、讲稿、学生要交什么、可以对话的角色
  评价板块.md   老师课后按组、按板块打分用：每个板块属于哪次课、含哪些知识点、占总分多少、评分要点
  知识点.csv    表头含“编号、名称、深度”，可以再加一列“出现在”（阶段编号，如 N02、N04）；深度写“展开”或“点到”，
                板块的分只落到“展开”的知识点上；“出现在”用来算接触次数和课上学习时间
  名词解释.md   “## 名词”下面一段解释，页面上出现这些词时可以点开看

课程.md 的写法见 tests/fixtures/sample_course/课程.md。平台能做的就这几样：按阶段推进、倒计时、发资料、
收各组的填报（单选、多选、数字、文字）、和 AI 角色限次数对话、老师课后打分。要别的玩法，得写课程插件。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .course import Stage

COURSE_FILE = "课程.md"
ENDS = {"老师": "teacher", "到点": "timer", "到点或全交": "timer_or_all"}
KINDS = ("单选", "多选", "数字", "文字", "长文字")
DEFAULT_TURNS = 5
SHOW = 120  # 老师的表里，一格最多显示多少字


class CourseFormatError(ValueError):
    """课程包文件写得不对；message 里有文件名、行号和原因，给做课的人看。"""


@dataclass
class Node:
    stage: Stage
    hint: str = ""
    script: str = ""
    lecture: str = ""
    materials: list[tuple[str, str]] = field(default_factory=list)
    fields: list[dict] = field(default_factory=list)
    role: dict | None = None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _field(cells: list[str], where: str) -> dict:
    label, kind, spec, hint = (cells + ["", "", "", ""])[:4]
    if kind not in KINDS:
        raise CourseFormatError(f"{where}：“{label}”的类型要写 {'、'.join(KINDS)} 之一，现在是“{kind}”")
    if not label or re.search(r"[\"'<>&|]", label):
        raise CourseFormatError(f"{where}：要交的内容得有名字，名字里不要有引号和尖括号")
    out = {"key": re.sub(r"\s+", "", label), "label": label, "hint": hint.replace("选填", "").strip("，,；; "),
           "required": "选填" not in spec + hint}
    spec = spec.replace("选填", "").strip("，,；; ")
    if kind in ("单选", "多选"):
        most = re.search(r"最多\s*(\d+)\s*项", spec + hint)
        options = [o.strip() for o in re.split(r"[、，,]", re.sub(r"[（(]?最多\s*\d+\s*项[）)]?", "", spec)) if o.strip()]
        if len(options) < 2:
            raise CourseFormatError(f"{where}：“{label}”至少要有两个选项，用顿号隔开")
        if kind == "单选":
            out.update(type="choice", options=[{"v": o, "label": o} for o in options])
        else:
            out.update(type="multi", options=options, max=int(most.group(1)) if most else 0)
            out["hint"] = re.sub(r"[（(]?最多\s*\d+\s*项[）)]?", "", out["hint"]).strip("，,；; ")
    elif kind == "数字":
        span = re.match(r"(-?[\d.]+)\s*[～~\-—至到]\s*(-?[\d.]+)\s*(.*)", spec)
        if span:
            out.update(type="number", min=float(span.group(1)), max=float(span.group(2)), unit=span.group(3).strip())
        else:
            out.update(type="number", unit=spec)
    else:
        most = re.search(r"(\d+)\s*字", spec)
        out.update(type="text", max=int(most.group(1)) if most else (600 if kind == "长文字" else 100))
        if kind == "长文字":
            out["rows"] = 5
    return out


def parse(folder: Path) -> dict:
    """读课程.md，返回 {name, code, version, about, nodes}。写得不对就抛 CourseFormatError。"""
    path = Path(folder) / COURSE_FILE
    meta = {"name": "", "编号": "", "版本": "", "说明": ""}
    nodes: list[Node] = []
    lesson, node, part, buf = 0, None, None, []

    def flush() -> None:
        nonlocal buf, part
        text = "\n".join(buf).strip()
        if node is not None and part is not None:
            kind, title = part
            if kind == "资料":
                node.materials.append((title or "资料", text))
            elif kind == "讲稿":
                node.lecture = text
            elif kind == "角色":
                turns = re.search(r"^- 次数[：:]\s*(\d+)", text, re.M)
                node.role = {"name": title or "角色", "turns": int(turns.group(1)) if turns else DEFAULT_TURNS,
                             "prompt": re.sub(r"^- 次数[：:].*\n?", "", text, flags=re.M).strip()}
                if not node.role["prompt"]:
                    raise CourseFormatError(f"{path.name}：阶段“{node.stage.name}”的角色“{title}”没有写设定")
            elif kind == "任务":
                rows = [_cells(line) for line in buf if line.strip().startswith("|") and not re.match(r"^\|\s*:?-", line.strip())]
                node.fields = [_field(r, f"{path.name} 阶段“{node.stage.name}”的任务") for r in rows[1:]]
                keys = [f["key"] for f in node.fields]
                if not keys or len(keys) != len(set(keys)):
                    raise CourseFormatError(f"{path.name}：阶段“{node.stage.name}”的任务表是空的，或者有两行同名")
        buf, part = [], None

    attrs: dict = {}

    def open_stage(no: int, key: str, name: str) -> None:
        nonlocal node, attrs
        if any(n.stage.key == key for n in nodes):
            raise CourseFormatError(f"{path.name} 第 {no} 行：阶段编号 {key} 重复了")
        attrs = {}
        node = Node(Stage(key, name, lesson))
        nodes.append(node)

    def close_stage() -> None:
        """把“- 时长：”这些写进阶段。"""
        if node is None:
            return
        minutes = int(re.search(r"\d+", attrs["时长"]).group()) if re.search(r"\d+", attrs.get("时长", "")) else None
        ends = attrs.get("结束", "到点或全交" if minutes else "老师")
        if ends not in ENDS:
            raise CourseFormatError(f"{path.name}：阶段“{node.stage.name}”的“结束”要写 {'、'.join(ENDS)} 之一")
        if ENDS[ends] != "teacher" and not minutes:
            raise CourseFormatError(f"{path.name}：阶段“{node.stage.name}”写了“{ends}”结束，就要写时长")
        extend = int(re.search(r"\d+", attrs["延长"]).group()) if re.search(r"\d+", attrs.get("延长", "")) else 0
        node.hint, node.script = attrs.get("提示", ""), attrs.get("老师", "")
        node.stage = Stage(node.stage.key, node.stage.name, node.stage.lesson, minutes=minutes, ends=ENDS[ends],
                           form=bool(node.fields), extend_minutes=extend)

    if not path.is_file():
        raise CourseFormatError(f"{folder} 里没有 {COURSE_FILE}")
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("# ") and not meta["name"]:
            meta["name"] = line[2:].strip()
        elif line.startswith("## "):
            flush(), close_stage()
            node = None
            found = re.match(r"## 第\s*(\d+)\s*次课", line)
            if not found:
                raise CourseFormatError(f"{path.name} 第 {no} 行：二级标题要写成“## 第 1 次课 课名”")
            if int(found.group(1)) < lesson:
                raise CourseFormatError(f"{path.name} 第 {no} 行：课次要从小到大排")
            lesson = int(found.group(1))
        elif line.startswith("### "):
            flush(), close_stage()
            found = re.match(r"### (N\d{2,3})\s+(.+)", line)
            if not found or not lesson:
                raise CourseFormatError(f"{path.name} 第 {no} 行：阶段标题要写成“### N01 阶段名”，并且放在某次课下面")
            open_stage(no, found.group(1).lower(), found.group(2).strip())
        elif line.startswith("#### "):
            flush()
            title = line[5:].strip()
            kind, _, rest = title.partition("：")
            if node is None or kind not in ("资料", "讲稿", "任务", "角色"):
                raise CourseFormatError(f"{path.name} 第 {no} 行：阶段下面只能有“资料：标题”“讲稿”“任务”“角色：称呼”四种小节")
            part = (kind, rest.strip())
        elif part is not None:
            buf.append(line)
        elif line.startswith("- ") and re.match(r"- (\S+?)[：:]\s*(.*)", line):
            key, value = re.match(r"- (\S+?)[：:]\s*(.*)", line).groups()
            if node is None and key in meta:
                meta[key] = value.strip()
            elif node is not None and key in ("时长", "结束", "延长", "提示", "老师"):
                attrs[key] = value.strip()
            elif node is not None:
                raise CourseFormatError(f"{path.name} 第 {no} 行：阶段下面不认识“{key}”，能写的是 时长、结束、延长、提示、老师")
    flush(), close_stage()
    if not meta["name"] or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", meta["编号"]) or not re.fullmatch(r"\d+\.\d+", meta["版本"]):
        raise CourseFormatError(f"{path.name}：开头要有“# 课程名称”“- 编号：小写英文字母、数字和短横线”“- 版本：0.1 这样的两段数字”")
    if not nodes:
        raise CourseFormatError(f"{path.name}：一个阶段都没有")
    return {"name": meta["name"], "code": meta["编号"], "version": meta["版本"], "about": meta["说明"], "nodes": nodes}


def read_blocks(folder: Path, lessons: set[int]) -> tuple[list[dict], dict]:
    """评价板块和知识点。返回 (板块, {知识点编号: {name, deep}})。没有知识点.csv 时，板块里的知识点都算“展开”。"""
    points: dict = {}
    table = Path(folder) / "知识点.csv"
    if table.is_file():
        with table.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                code = (row.get("编号") or "").strip()
                if code:
                    points[code] = {"name": (row.get("名称") or "").strip(), "deep": (row.get("深度") or "展开").strip() != "点到",
                                    "stages": [x.lower() for x in re.findall(r"N\d{2,3}", row.get("出现在") or "", flags=re.I)]}
    blocks, cur = [], None
    path = Path(folder) / "评价板块.md"
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        head = re.match(r"## ([A-Z]+\d+) (.+)", line)
        if head:
            cur = {"id": head.group(1), "name": head.group(2).strip(), "lesson": 0, "cards": [], "weight": 0.0, "look": "", "points": []}
            blocks.append(cur)
        elif cur is None:
            continue
        elif line.startswith("- 课次"):
            cur["lesson"] = int(re.search(r"\d+", line).group())
        elif line.startswith(("- 知识点", "- 知识卡")):
            cur["cards"] = re.findall(r"K-[A-Z]+-\d+", line)
        elif line.startswith("- 占总分"):
            cur["weight"] = float(re.search(r"[\d.]+", line).group()) / 100
        elif line.startswith("- 老师看什么"):
            cur["look"] = re.split(r"[：:]", line, maxsplit=1)[1].strip()
        elif line.startswith("  - "):
            cur["points"].append(line[4:].strip())
    for b in blocks:
        if b["lesson"] not in lessons:
            raise CourseFormatError(f"评价板块.md：板块 {b['id']} 写的课次，课程.md 里没有")
        for code in b["cards"]:
            points.setdefault(code, {"name": "", "deep": True, "stages": []})
    return blocks, points


def problems(folder: Path) -> list[str]:
    """验收时用：课程包还差什么。空列表表示平台这一关过了（内容对不对、合不合规要人看）。"""
    try:
        course = ContentCourse(folder)
    except CourseFormatError as exc:
        return [str(exc)]
    out = []
    if not course.blocks:
        out.append("没有评价板块.md：老师打不了分，学生的知识图谱长不出来")
    else:
        total = round(sum(b["weight"] for b in course.blocks) * 100, 2)
        if total != 100:
            out.append(f"评价板块占总分合计 {total:g}%，要正好 100%")
        seen: dict = {}
        for b in course.blocks:
            if not b["cards"]:
                out.append(f"板块 {b['id']} 没有写知识点")
            if not b["points"]:
                out.append(f"板块 {b['id']} 没有写评分要点")
            for code in b["cards"]:
                if code in seen:
                    out.append(f"知识点 {code} 同时在板块 {seen[code]} 和 {b['id']} 里，一个知识点只进一个板块")
                seen[code] = b["id"]
        for n in sorted({s.lesson for s in course.stages} - {b["lesson"] for b in course.blocks}):
            out.append(f"第 {n} 次课没有评价板块（可以没有，但这次课就不打分）")
    if not any(n.fields for n in course.nodes.values()):
        out.append("没有任何阶段要学生交东西")
    for n in course.nodes.values():
        if not (n.materials or n.fields or n.role or n.hint):
            out.append(f"阶段“{n.stage.name}”是空的：没有资料、任务、角色，也没有给学生的提示")
    return out


def _text(title: str, body: str, tone: str = "") -> dict:
    return {"t": "text", "title": title, "body": body, "tone": tone}


class ContentCourse:
    def __init__(self, folder: Path):
        folder = Path(folder)
        data = parse(folder)
        self.name, self.about = data["name"], data["about"]
        self.ref = f"{data['code']}@{data['version']}"
        self.nodes: dict[str, Node] = {n.stage.key: n for n in data["nodes"]}
        self.stages = [n.stage for n in data["nodes"]]
        self.blocks, self.points = read_blocks(folder, {s.lesson for s in self.stages})
        self.glossary: dict[str, str] = {}
        key = None
        words = folder / "名词解释.md"
        for line in words.read_text(encoding="utf-8").splitlines() if words.is_file() else []:
            if line.startswith("## "):
                key = line[3:].strip()
                self.glossary[key] = ""
            elif key and line.strip():
                self.glossary[key] += line.strip()

    # ---------- 填报 ----------

    def validate(self, stage, payload, state, team):
        out = {}
        for f in self.nodes[stage].fields:
            raw = payload.get(f["key"])
            if raw in (None, "", []):
                if f["required"]:
                    raise ValueError(f"请填“{f['label']}”")
                continue
            if f["type"] == "choice":
                if raw not in [o["v"] for o in f["options"]]:
                    raise ValueError(f"“{f['label']}”要从给出的选项里选")
            elif f["type"] == "multi":
                if not isinstance(raw, list) or any(o not in f["options"] for o in raw) or len(set(raw)) != len(raw):
                    raise ValueError(f"“{f['label']}”要从给出的选项里选")
                if f["max"] and len(raw) > f["max"]:
                    raise ValueError(f"“{f['label']}”最多选 {f['max']} 项")
            elif f["type"] == "number":
                try:
                    raw = float(raw)
                except (TypeError, ValueError):
                    raise ValueError(f"“{f['label']}”要填数字") from None
                if ("min" in f and raw < f["min"]) or ("max" in f and raw > f["max"]):
                    raise ValueError(f"“{f['label']}”要在 {f['min']:g} 到 {f['max']:g} 之间")
                raw = int(raw) if raw == int(raw) else raw
            else:
                raw = str(raw).strip()
                if len(raw) > f["max"]:
                    raise ValueError(f"“{f['label']}”最多 {f['max']} 字，现在 {len(raw)} 字")
            out[f["key"]] = raw
        return out

    def default(self, stage, state, team):
        return None  # 没交就是没交，不替学生填

    # ---------- 课后评分 ----------

    def gate(self, key: str | None) -> int | None:
        """这个阶段是不是某次课的最后一个阶段（老师在这里打这次课的分）；是就返回第几次课。"""
        for i, s in enumerate(self.stages):
            if s.key == key:
                last = i == len(self.stages) - 1 or self.stages[i + 1].lesson != s.lesson
                return s.lesson if last and any(b["lesson"] == s.lesson for b in self.blocks) else None
        return None

    @staticmethod
    def students(state) -> list[str]:
        return [t.name for t in state.teams if not t.npc]

    @staticmethod
    def grades(state) -> dict:
        """老师确认过的分：{板块: {组: 分}}。同一格以最后一次录入为准。"""
        out: dict = {}
        for records in state.records.values():
            for r in records:
                for team, row in (r.get("scores") or {}).items():
                    for block, value in row.items():
                        out.setdefault(block, {})[team] = value
        return out

    def ungraded(self, state, lesson: int) -> list[tuple[str, str]]:
        done = self.grades(state)
        return [(t, b["name"]) for b in self.blocks if b["lesson"] == lesson for t in self.students(state)
                if done.get(b["id"], {}).get(t) is None]

    def total(self, state, team: str) -> tuple[float, float]:
        """课程总分（百分制）和已经评过的板块占多少权重。"""
        done = self.grades(state)
        got = [(b["weight"], done[b["id"]][team]) for b in self.blocks if done.get(b["id"], {}).get(team) is not None]
        return sum(w * v * 10 for w, v in got), sum(w for w, _ in got)

    def card_scores(self, state, team: str) -> dict:
        """板块的分只落到“展开”的知识点上：{知识点编号: 分}，给知识图用。"""
        done = self.grades(state)
        return {c: done[b["id"]][team] for b in self.blocks if done.get(b["id"], {}).get(team) is not None
                for c in b["cards"] if self.points[c]["deep"]}

    def touched_cards(self, state, team: str) -> list[str]:
        done = self.grades(state)
        return [c for b in self.blocks if done.get(b["id"], {}).get(team) is not None for c in b["cards"] if not self.points[c]["deep"]]

    QUIET_CAP = 10  # 没设时长的阶段，学习时间最多算 10 分钟（课后阶段会开着好几天）

    def exposure(self, state, codes) -> tuple[dict, dict]:
        """接触次数和课上学习时间：知识点“出现在”的阶段里，已经上过几个、一共上了多少分钟。"""
        cap = {s.key: s.minutes or self.QUIET_CAP for s in self.stages}
        seen = {c: [k for k in self.points[c]["stages"] if k in state.spent] for c in codes}
        return ({c: len(ks) for c, ks in seen.items() if ks},  # “点到”的知识点只数次数，不算时间：它只是在资料里被提到
                {c: sum(min(state.spent[k], cap.get(k, self.QUIET_CAP)) for k in ks) for c, ks in seen.items() if ks and self.points[c]["deep"]})

    def knowledge(self, state, team: str) -> dict:
        scores, touched = self.card_scores(state, team), self.touched_cards(state, team)
        touches, minutes = self.exposure(state, list(scores) + touched)
        return {"scores": scores, "touched": touched, "touches": touches, "minutes": minutes}

    def validate_record(self, stage, payload, state):
        lesson = self.gate(stage)
        if lesson is None or "scores" not in (payload or {}):
            raise ValueError("现在不是打分的时候：每次课的分，在这次课最后一个阶段打")
        allowed, teams, out = {b["id"] for b in self.blocks if b["lesson"] == lesson}, set(self.students(state)), {}
        for team, row in (payload.get("scores") or {}).items():
            if team not in teams or not isinstance(row, dict):
                raise ValueError(f"没有“{team}”这个组")
            for block, value in row.items():
                if block not in allowed:
                    raise ValueError("这个板块不属于这次课")
                if value in (None, ""):
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ValueError("分数要填 0 到 10 之间的数") from None
                if not 0 <= value <= 10 or round(value * 2) != value * 2:
                    raise ValueError("分数在 0 到 10 之间，最小到 0.5 分")
                out.setdefault(team, {})[block] = value
        if not out:
            raise ValueError("还没有填分数")
        return {"scores": out}

    def close(self, stage, state):
        from .game import GameError  # 放在这里引入，避免两个模块互相引用
        lesson = self.gate(stage)
        missing = self.ungraded(state, lesson) if lesson else []
        if missing:
            raise GameError(f"第 {lesson} 次课的评分还没打完（还差 {len(missing)} 格）。老师课后打完分，才能进入下一步。")
        return {"finished": True} if stage == self.stages[-1].key else None

    # ---------- 页面 ----------

    @staticmethod
    def _show(f: dict, value) -> str:
        if value in (None, "", []):
            return "—"
        text = "、".join(value) if isinstance(value, list) else f"{value:g}{f.get('unit', '')}" if f["type"] == "number" else str(value)
        return text if len(text) <= SHOW else text[:SHOW] + "…"

    def _lesson_rows(self, state, lesson: int, team: str) -> list:
        rows = []
        for n in self.nodes.values():
            if n.stage.lesson != lesson or not n.fields:
                continue
            got = state.decisions.get(n.stage.key, {}).get(team)
            rows += [[f"{n.stage.name} · {f['label']}", self._show(f, got.get(f["key"]))] for f in n.fields] if got else [[n.stage.name, "没有提交"]]
        return rows

    def _score_card(self, state, lesson: int, live: bool) -> dict:
        done, blocks, teams = self.grades(state), [b for b in self.blocks if b["lesson"] == lesson], self.students(state)
        left = self.ungraded(state, lesson)
        return {"t": "score", "title": f"第 {lesson} 次课评分（课后打，10 分制）", "live": live, "auto": {},
                "teams": [{"team": t, "label": t, "rows": self._lesson_rows(state, lesson, t)} for t in teams],
                "blocks": [{"id": b["id"], "name": b["name"], "weight": round(b["weight"] * 100), "cards": len(b["cards"]),
                            "deep": sum(1 for c in b["cards"] if self.points[c]["deep"]), "points": b["points"], "look": b["look"], "auto": ""}
                           for b in blocks],
                "values": {t: {b["id"]: done.get(b["id"], {}).get(t) for b in blocks} for t in teams},
                "note": "每个组、每个板块都打了分，才能进入下一步。" + (f"还差 {len(left)} 格。" if left else "这次课的分已经打完。")}

    def _my_scores(self, state, lesson: int, team: str) -> dict:
        done = self.grades(state)
        rows = [[f"{b['name']}（占总分 {b['weight'] * 100:.0f}%）", f"{done[b['id']][team]:g} 分"] for b in self.blocks
                if b["lesson"] == lesson and done.get(b["id"], {}).get(team) is not None]
        if not rows:
            return _text("老师的评分", "这次课的分，老师课后登录来打。打完以后在这里能看到。")
        total, weight = self.total(state, team)
        return {"t": "kv", "title": "老师给本组的评分", "rows": rows,
                "note": f"到目前为止，已评的板块占总分 {weight * 100:.0f}%，本组得 {total:.1f} 分（满分 {weight * 100:.0f}）。"}

    def _page(self, state, team, role, node: Node, live: bool) -> dict:
        key, lesson, cards = node.stage.key, self.gate(node.stage.key), []
        talks = state.talks.get(key, {})
        if role == "teacher":
            if lesson:
                cards.append(self._score_card(state, lesson, live))
            if node.fields:
                got = state.decisions.get(key, {})
                rows = [[t] + ([self._show(f, got[t].get(f["key"])) for f in node.fields] if t in got else ["没有提交"] + [""] * (len(node.fields) - 1))
                        for t in self.students(state)]
                cards.append({"t": "table", "title": "各组提交的内容", "head": ["组"] + [f["label"] for f in node.fields], "rows": rows,
                              "note": "长的内容只显示开头。"})
            if node.role:
                cards.append({"t": "kv", "title": f"各组和{node.role['name']}（AI 扮演）谈了几次", "rows": [[t, f"{len(talks.get(t, []))} 次"] for t in self.students(state)],
                              "note": f"每组最多 {node.role['turns']} 次。谈的内容只有本组看得到。"})
            return {"cards": cards or [_text("", "这一步没有要收的内容。")]}
        if team is None:
            return {"cards": [_text("", "你不在这一局的任何组里，只能旁观。")]}
        if lesson:
            cards.append(self._my_scores(state, lesson, team))
        asked = talks.get(team, [])
        if node.role and (live or asked):
            left = max(0, node.role["turns"] - len(asked)) if live else 0
            cards.append({"t": "talk", "title": f"和{node.role['name']}谈一谈", "name": node.role["name"], "history": asked, "left": left, "live": live,
                          "hint": "", "note": f"全组一共可以问 {node.role['turns']} 次，组里谁问都算。" + ("" if left or not live else "次数已经用完。")})
        page = {"cards": cards}
        if node.fields:
            mine = state.decisions.get(key, {}).get(team)
            page["form"] = {"fields": node.fields, "values": mine or {}, "done": bool(mine), "button": "提交", "note": ""}
        elif not cards:
            page["cards"] = [_text("", node.hint or "这一步听老师讲。")]
        return page

    def view(self, state, team, role):
        keys = [s.key for s in self.stages]
        over = self.stages[-1].key in state.results
        reached = keys if over else keys[:keys.index(state.stage) + 1] if state.stage in keys else []
        node = self.nodes.get(state.stage)
        view = {"title": self.name, "companies": {t.name: t.company or t.name for t in state.teams},
                "hint": node.hint if node else "课程已经全部结束。" if over else "等老师开始上课。",
                "extend": node.stage.extend_minutes if node and node.stage.extend_minutes else None,
                "glossary": self.glossary,
                "materials": {k: [{"code": str(i), "title": title, "after": 0, "text": text, "open": i == 1}
                                  for i, (title, text) in enumerate(self.nodes[k].materials, 1)] for k in reached},
                "pages": {k: self._page(state, team, role, self.nodes[k], False) for k in reached if k != state.stage}}
        if node:
            view["page"] = self._page(state, team, role, node, True)
        if role == "teacher":
            view["teacher"] = {"script": node.script if node else "课程已经全部结束。" if over else "点“开始上课”，进入第一个阶段。",
                               "lectures": {k: self.nodes[k].lecture for k in reached if self.nodes[k].lecture}}
        return view

    # ---------- 助手和 AI 角色 ----------

    def _handed_in(self, state, team: str, upto: str) -> list[str]:
        lines = []
        for s in self.stages:
            got = state.decisions.get(s.key, {}).get(team)
            if got:
                lines += [f"- {s.name} · {f['label']}：{self._show(f, got.get(f['key']))}" for f in self.nodes[s.key].fields]
            if s.key == upto:
                break
        return lines

    def role(self, stage, state, team):
        node = self.nodes.get(stage)
        if not node or not node.role or team not in self.students(state):
            return None
        done = self._handed_in(state, team, stage)
        facts = "\n\n【这一组到现在提交的内容】（由平台提供，只按这些事实说话，没有的不要编）\n" + ("\n".join(done) if done else "还没有提交任何内容。")
        return {"name": node.role["name"], "turns": node.role["turns"], "prompt": node.role["prompt"] + facts}

    def brief(self, state, team, role) -> str:
        node = self.nodes.get(state.stage)
        if node is None:
            return f"课程：{self.name}。" + ("这一局已经全部结束。" if self.stages[-1].key in state.results else "老师还没有点开始。")
        lines = [f"课程：{self.name}。现在是第 {node.stage.lesson} 次课的阶段“{node.stage.name}”。"]
        if role == "teacher":
            lines.append(f"这一步老师要做的：{node.script}" if node.script else "")
            lines.append("各组提交情况：" + "、".join(f"{t}{'已交' if t in state.decisions.get(node.stage.key, {}) else '没交'}" for t in self.students(state)) if node.fields else "")
            return "\n".join(x for x in lines if x)
        lines.append(f"这一步的提示：{node.hint}" if node.hint else "")
        if node.materials:
            lines.append("这一步发给学生的资料：" + "、".join(title for title, _ in node.materials))
        if node.fields:
            lines.append("这一步本组要交：" + "；".join(f["label"] + (f"（{'、'.join(o['v'] for o in f['options'])}）" if f["type"] == "choice" else "") for f in node.fields))
        done = self._handed_in(state, team, node.stage.key) if team else []
        lines.append("本组到现在提交的内容：\n" + "\n".join(done) if done else "本组还没有提交过内容。")
        return "\n".join(x for x in lines if x)
