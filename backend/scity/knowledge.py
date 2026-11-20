"""知识图谱的总图：把专业目录、知识领域、知识点、相近专业从表格导入数据库，并提供查询。

导入用的表是带表头的 CSV（UTF-8，可带 BOM），放在一个目录里，按文件名认：
  专业目录/本科专业目录*.csv        列：门类代码、学科门类、专业类代码、专业类、专业代码、专业名称
  专业目录/职业教育专业目录*.csv    列：层次、专业大类代码、专业大类、专业类代码、专业类、专业代码、专业名称、来源（只取高职专科和职业本科）
  知识领域*.csv                    列：大领域代码、大领域、知识领域编号、知识领域、适用范围
  知识点清单*.csv                  列：编号、名称、知识领域编号、关键词、相关知识点、本科专业代码、高职专业代码、来源课程包、核对状态、一句话
  相近专业对照表*.csv              列：专业代码、目录、相近专业代码、相近专业目录、依据、专业群、相近专业所在专业群
多个值写在一格里时用顿号分隔。可以反复导入：已有的更新，表里没有的专业不会被删（学生可能已经登记在它名下）。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .models import (MAJOR_LEVELS, KnowledgeDomain, KnowledgeKeyword, KnowledgeLink, KnowledgeMark, KnowledgePoint,
                     KnowledgePointMajor, Major, MajorNeighbor, User)

RINGS = {1: "本专业和相近专业", 2: "其他专业", 3: "专业以外的兴趣"}  # 学科主次，从近到远；第三圈来自聊天，真实数据里还没有
# 演示用的“个人衍生的知识点”（虚线泡泡）：名字、第几圈、聊到几次、多少分钟。真实的要从聊天里识别，还没有做
DEMO_DERIVED = [("AI 出图提示词", 1, 27, 95), ("参数化建模", 1, 6, 22), ("建筑摄影", 1, 4, 12), ("客户沟通", 2, 12, 40), ("报价常识", 2, 3, 8),
                ("短视频剪辑", 2, 8, 31), ("辩论技巧", 3, 5, 18), ("吉他入门", 3, 9, 46), ("健身计划", 3, 2, 5)]

TIERS = ("点到", "了解", "理解", "会用")  # 实线泡泡的四档，从小到大
UNDERSTAND, MASTER = 6.0, 8.5              # 10 分制：6 分以上“理解”，8.5 分以上“会用”（暂定，以后按真实成绩调）
KNOW_TOUCHES, KNOW_MINUTES = 3, 10.0       # 接触 3 次以上或累计 10 分钟以上，从“点到”长到“了解”（暂定）


def tier(score: float | None, touches: int, minutes: float) -> int:
    """一个知识点现在是第几档（TIERS 的下标）。理解、会用只凭老师确认过的分；其余看接触多少。"""
    if score is not None and score >= MASTER:
        return 3
    if score is not None and score >= UNDERSTAND:
        return 2
    return 1 if touches >= KNOW_TOUCHES or minutes >= KNOW_MINUTES else 0


def _rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(fh)]


def _many(cell: str) -> list[str]:
    """一格里的多个值：顿号、逗号、分号都认。"""
    return [x.strip() for x in re.split(r"[、，,；;]", cell or "") if x.strip()]


def _latest(folder: Path, pattern: str) -> Path | None:
    found = sorted(folder.glob(pattern))
    return found[-1] if found else None


class KnowledgeMap:
    def __init__(self, sessions: sessionmaker):
        self._sessions = sessions

    # ---------- 导入 ----------

    def import_dir(self, folder: Path) -> dict:
        """导入一个目录里的总图表格。返回各类数量和要人看一眼的问题（对不上的代码等）。"""
        folder = Path(folder)
        report = {"majors": 0, "domains": 0, "points": 0, "keywords": 0, "links": 0, "point_majors": 0, "neighbors": 0, "warnings": []}
        warn = report["warnings"].append
        with self._sessions.begin() as db:
            majors = {(m.level, m.code): m for m in db.scalars(select(Major))}
            touched: set = set()

            def major(level: str, row: dict, category_code: str, category: str, source: str) -> None:
                key = (level, row["专业代码"])
                m = majors.get(key)
                if m is None:
                    m = majors[key] = Major(level=level, code=row["专业代码"])
                    db.add(m)
                m.name, m.category_code, m.category = row["专业名称"], category_code, category
                m.class_code, m.class_name, m.source = row.get("专业类代码", ""), row.get("专业类", ""), source
                touched.add(key)
                report["majors"] += 1

            path = _latest(folder, "专业目录/本科专业目录*.csv")
            if path:
                for row in _rows(path):
                    major("本科", row, row.get("门类代码", ""), row.get("学科门类", ""), path.stem)
            else:
                warn("没有找到本科专业目录")
            path = _latest(folder, "专业目录/职业教育专业目录*.csv")
            if path:
                for row in _rows(path):
                    if row.get("层次") in MAJOR_LEVELS:
                        major(row["层次"], row, row.get("专业大类代码", ""), row.get("专业大类", ""), row.get("来源") or path.stem)
            else:
                warn("没有找到职业教育专业目录")
            db.flush()
            stale = [f"{m.level} {m.code} {m.name}" for key, m in majors.items() if key not in touched]
            if stale and touched:
                warn(f"库里有 {len(stale)} 个专业不在这次的目录里（没有删，学生可能登记在它名下）：" + "、".join(stale[:5]) + ("…" if len(stale) > 5 else ""))

            domains = {d.code: d for d in db.scalars(select(KnowledgeDomain))}
            path = _latest(folder, "知识领域*.csv")
            for row in _rows(path) if path else []:
                d = domains.get(row["知识领域编号"])
                if d is None:
                    d = domains[row["知识领域编号"]] = KnowledgeDomain(code=row["知识领域编号"])
                    db.add(d)
                d.name, d.group_code, d.group_name, d.scope = row["知识领域"], row["大领域代码"], row["大领域"], row.get("适用范围", "")
                report["domains"] += 1
            db.flush()

            points = {p.code: p for p in db.scalars(select(KnowledgePoint))}
            path = _latest(folder, "知识点清单*.csv")
            listed = _rows(path) if path else []
            for row in listed:
                if row["知识领域编号"] not in domains:
                    warn(f"知识点 {row['编号']} 的知识领域 {row['知识领域编号']} 不在知识领域表里，没有导入")
                    continue
                p = points.get(row["编号"])
                if p is None:
                    p = points[row["编号"]] = KnowledgePoint(code=row["编号"])
                    db.add(p)
                p.name, p.domain_id, p.summary = row["名称"], domains[row["知识领域编号"]].id, row.get("一句话", "")[:300]
                p.source, p.status = row.get("来源课程包", ""), row.get("核对状态", "")
                report["points"] += 1
            db.flush()

            by_code: dict[str, list[Major]] = {}
            for (level, code), m in majors.items():
                by_code.setdefault(code, []).append(m)
            for row in listed:
                p = points.get(row["编号"])
                if p is None or p.id is None:
                    continue
                for table in (KnowledgeKeyword, KnowledgeLink, KnowledgePointMajor):
                    db.execute(delete(table).where(table.point_id == p.id))
                for word in dict.fromkeys(_many(row.get("关键词", ""))):
                    db.add(KnowledgeKeyword(point_id=p.id, word=word[:40]))
                    report["keywords"] += 1
                for code in dict.fromkeys(_many(row.get("相关知识点", ""))):
                    if code in points and code != p.code:
                        db.add(KnowledgeLink(point_id=p.id, related_id=points[code].id))
                        report["links"] += 1
                    else:
                        warn(f"知识点 {p.code} 的相关知识点 {code} 不在清单里")
                linked = set()
                for level_group, cell in ((("本科",), row.get("本科专业代码", "")), (("高职专科", "职业本科"), row.get("高职专业代码", ""))):
                    for code in _many(cell):
                        hits = [m for m in by_code.get(code, []) if m.level in level_group]
                        if not hits:
                            warn(f"知识点 {p.code} 挂的专业代码 {code} 不在专业目录里")
                        for m in hits:
                            if m.id not in linked:
                                linked.add(m.id)
                                db.add(KnowledgePointMajor(point_id=p.id, major_id=m.id))
                                report["point_majors"] += 1

            path = _latest(folder, "相近专业对照表*.csv")
            if path:
                db.execute(delete(MajorNeighbor))
                seen = set()
                for row in _rows(path):
                    a, b = majors.get((row["目录"], row["专业代码"])), majors.get((row["相近专业目录"], row["相近专业代码"]))
                    if a is None or b is None:
                        warn(f"相近专业对照表里 {row['目录']} {row['专业代码']} → {row['相近专业目录']} {row['相近专业代码']} 有一边不在专业目录里")
                        continue
                    if row.get("专业群"):
                        a.cluster = row["专业群"][:40]
                    if row.get("相近专业所在专业群"):
                        b.cluster = row["相近专业所在专业群"][:40]
                    if a.id != b.id and (a.id, b.id) not in seen:
                        seen.add((a.id, b.id))
                        db.add(MajorNeighbor(major_id=a.id, neighbor_id=b.id, basis=row.get("依据", "")[:40]))
                        report["neighbors"] += 1
        report["warnings"] = list(dict.fromkeys(report["warnings"]))
        return report

    # ---------- 查询 ----------

    def stats(self) -> dict:
        with self._sessions() as db:
            count = lambda table: db.scalar(select(func.count()).select_from(table))
            levels = dict(db.execute(select(Major.level, func.count()).group_by(Major.level)).all())
            return {"majors": levels, "domains": count(KnowledgeDomain), "groups": db.scalar(select(func.count(func.distinct(KnowledgeDomain.group_code)))),
                    "points": count(KnowledgePoint), "keywords": count(KnowledgeKeyword), "links": count(KnowledgeLink),
                    "point_majors": count(KnowledgePointMajor), "neighbors": count(MajorNeighbor)}

    def find_major(self, level: str, code: str) -> int | None:
        with self._sessions() as db:
            return db.scalar(select(Major.id).where(Major.level == level, Major.code == code))

    def near(self, major_id: int) -> set[int]:
        """相近专业（不含本专业）。"""
        with self._sessions() as db:
            return set(db.scalars(select(MajorNeighbor.neighbor_id).where(MajorNeighbor.major_id == major_id)))

    def ring(self, major_id: int | None, point_code: str) -> int:
        """以这个专业为中心，一个知识点落在第几圈：1 本专业和相近专业，2 其他专业。没有登记专业的一律算 2。"""
        if major_id is None:
            return 2
        with self._sessions() as db:
            owners = set(db.scalars(select(KnowledgePointMajor.major_id).join(KnowledgePoint, KnowledgePoint.id == KnowledgePointMajor.point_id)
                                    .where(KnowledgePoint.code == point_code)))
        return 1 if owners & ({major_id} | self.near(major_id)) else 2

    def point(self, code: str) -> dict | None:
        with self._sessions() as db:
            p = db.scalar(select(KnowledgePoint).where(KnowledgePoint.code == code))
            if p is None:
                return None
            d = db.get(KnowledgeDomain, p.domain_id)
            related = db.scalars(select(KnowledgePoint.code).join(KnowledgeLink, KnowledgeLink.related_id == KnowledgePoint.id)
                                 .where(KnowledgeLink.point_id == p.id).order_by(KnowledgePoint.code)).all()
            words = db.scalars(select(KnowledgeKeyword.word).where(KnowledgeKeyword.point_id == p.id).order_by(KnowledgeKeyword.id)).all()
            return {"code": p.code, "name": p.name, "summary": p.summary, "domain": d.name, "domain_code": d.code, "group": d.group_name,
                    "group_code": d.group_code, "keywords": list(words), "related": list(related), "source": p.source}

    @staticmethod
    def _major(m: Major | None) -> dict | None:
        return m and {"id": m.id, "level": m.level, "code": m.code, "name": m.name, "class_name": m.class_name or m.category}

    def search_majors(self, level: str, q: str, limit: int = 30) -> list[dict]:
        """给本人选专业用：按层次，在专业名称、专业类和代码里找。"""
        q = q.strip()
        with self._sessions() as db:
            query = select(Major).where(Major.level == level)
            if q:
                query = query.where(Major.name.contains(q) | Major.class_name.contains(q) | Major.code.startswith(q))
            return [self._major(m) for m in db.scalars(query.order_by(Major.code).limit(limit))]

    def major_of(self, user_id: int) -> dict | None:
        with self._sessions() as db:
            user = db.get(User, user_id)
            return self._major(db.get(Major, user.major_id)) if user and user.major_id else None

    def set_major(self, user_id: int, major_id: int) -> dict:
        with self._sessions.begin() as db:
            major = db.get(Major, major_id)
            if major is None:
                raise ValueError("专业目录里没有这个专业")
            db.get(User, user_id).major_id = major.id
            return self._major(major)

    def mine(self, user_id: int) -> dict:
        """一个学生的实线泡泡：学过的课程包里、老师评过分的知识点。同一个知识点在几门课里都学过时，
        分数取最高的，次数和时长相加。返回的 links 只含两头都已点亮的关联。"""
        with self._sessions() as db:
            merged: dict[str, dict] = {}
            for m in db.scalars(select(KnowledgeMark).where(KnowledgeMark.user_id == user_id).order_by(KnowledgeMark.id)):
                one = merged.setdefault(m.point_code, {"score": None, "touches": 0, "minutes": 0.0, "courses": [], "updated": m.updated_at})
                if m.score is not None and (one["score"] is None or m.score > one["score"]):
                    one["score"] = m.score
                one["touches"] += m.touches
                one["minutes"] += m.minutes
                one["updated"] = max(one["updated"], m.updated_at)
                if m.course_ref not in one["courses"]:
                    one["courses"].append(m.course_ref)
            known = {p.code: (p, d) for p, d in db.execute(select(KnowledgePoint, KnowledgeDomain).join(
                KnowledgeDomain, KnowledgeDomain.id == KnowledgePoint.domain_id).where(KnowledgePoint.code.in_(merged or [""])))}
            ids = {p.id: code for code, (p, _) in known.items()}
            links = sorted({tuple(sorted((ids[a], ids[b]))) for a, b in db.execute(select(KnowledgeLink.point_id, KnowledgeLink.related_id).where(
                KnowledgeLink.point_id.in_(ids or [0]), KnowledgeLink.related_id.in_(ids or [0])))})
            user = db.get(User, user_id)
            major = db.get(Major, user.major_id) if user and user.major_id else None
            circle = set()  # 落在第一圈的知识点：挂在本专业或相近专业下面的
            if major is not None:
                mine_and_near = {major.id} | set(db.scalars(select(MajorNeighbor.neighbor_id).where(MajorNeighbor.major_id == major.id)))
                circle = {ids[pid] for pid in db.scalars(select(KnowledgePointMajor.point_id).where(
                    KnowledgePointMajor.point_id.in_(ids or [0]), KnowledgePointMajor.major_id.in_(mine_and_near)))}
            major = self._major(major)
        points = []
        for code, one in merged.items():
            p, d = known.get(code, (None, None))
            level = tier(one["score"], one["touches"], one["minutes"])
            points.append({"code": code, "name": p.name if p else code, "summary": p.summary if p else "", "in_map": p is not None,
                           "domain": d.name if d else "", "domain_code": d.code if d else "", "group": d.group_name if d else "",
                           "group_code": d.group_code if d else "", "tier": TIERS[level], "level": level, "score": one["score"],
                           "touches": one["touches"], "minutes": round(one["minutes"], 1), "courses": one["courses"],
                           "updated": one["updated"].strftime("%Y-%m-%d"), "ring": None if major is None else 1 if code in circle else 2,
                           "derived": False})  # 课程里的知识点是实线；聊天里衍生出来的（虚线）还没有做
        points.sort(key=lambda x: (x["group_code"] or "~", x["domain_code"], x["code"]))
        return {"points": points, "links": [list(pair) for pair in links], "major": major, "rings": RINGS,
                "counts": {name: sum(1 for x in points if x["level"] == i) for i, name in enumerate(TIERS)}}

    def demo(self) -> dict:
        """演示用的一张图（不是任何人的真实数据）：一个建筑学本科生学完总图里现有的知识点，再加几个聊天里衍生的。
        知识点、知识领域、关联和分圈都取自真实的总图，分数和次数是按固定规则编的，每次都一样。"""
        with self._sessions() as db:
            major = db.scalar(select(Major).where(Major.level == "本科", Major.name == "建筑学")) or db.scalar(select(Major).order_by(Major.id))
            rows = db.execute(select(KnowledgePoint, KnowledgeDomain).join(KnowledgeDomain, KnowledgeDomain.id == KnowledgePoint.domain_id)
                              .order_by(KnowledgePoint.code).limit(90)).all()
            ids = {p.id: p.code for p, _ in rows}
            links = sorted({tuple(sorted((ids[a], ids[b]))) for a, b in db.execute(select(KnowledgeLink.point_id, KnowledgeLink.related_id).where(
                KnowledgeLink.point_id.in_(ids or [0]), KnowledgeLink.related_id.in_(ids or [0])))})
            near = ({major.id} | set(db.scalars(select(MajorNeighbor.neighbor_id).where(MajorNeighbor.major_id == major.id)))) if major else set()
            circle = {ids[pid] for pid in db.scalars(select(KnowledgePointMajor.point_id).where(
                KnowledgePointMajor.point_id.in_(ids or [0]), KnowledgePointMajor.major_id.in_(near or [0])))}
            major = self._major(major)
        points = []
        for i, (p, d) in enumerate(rows):
            score = None if i % 10 in (3, 6, 9) else [9.5, 7, 8.5, 6, 5, 7.5, 9, 6.5, 8, 4][i % 10]
            touches, minutes = [1, 1, 2, 4, 1, 9, 1, 3, 1, 12][(i * 7) % 10], [0, 8, 14, 20, 0, 63, 6, 25, 10, 35][(i * 7) % 10]
            level = tier(score, touches, minutes)
            points.append({"code": p.code, "name": p.name, "summary": p.summary, "in_map": True, "domain": d.name, "domain_code": d.code,
                           "group": d.group_name, "group_code": d.group_code, "tier": TIERS[level], "level": level, "score": score,
                           "touches": touches, "minutes": float(0 if score is None else minutes), "courses": ["演示课程"], "updated": "2026-11-20",
                           "ring": 1 if p.code in circle else 2, "derived": False})
        for i, (name, ring, touches, minutes) in enumerate(DEMO_DERIVED, 1):
            level = tier(None, touches, minutes)
            points.append({"code": f"DEMO-{i:02d}", "name": name, "summary": "", "in_map": False, "domain": "聊天里聊到的", "domain_code": f"chat.{ring}",
                           "group": "", "group_code": "", "tier": TIERS[level], "level": level, "score": None, "touches": touches,
                           "minutes": float(minutes), "courses": [], "updated": "2026-11-20", "ring": ring, "derived": True})
        solid = [x for x in points if not x["derived"]]
        return {"points": points, "links": [list(pair) for pair in links], "major": major, "rings": RINGS, "demo": True,
                "counts": {name: sum(1 for x in solid if x["level"] == i) for i, name in enumerate(TIERS)}}

    def match(self, word: str) -> list[str]:
        """一个词对应哪些知识点（按关键词或知识点名称）。"""
        with self._sessions() as db:
            by_word = db.scalars(select(KnowledgePoint.code).join(KnowledgeKeyword, KnowledgeKeyword.point_id == KnowledgePoint.id)
                                 .where(KnowledgeKeyword.word == word)).all()
            by_name = db.scalars(select(KnowledgePoint.code).where(KnowledgePoint.name == word)).all()
            return sorted(set(by_word) | set(by_name))
