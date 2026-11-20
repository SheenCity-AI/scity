"""官方后台（命令行简易版）：按名单开课，查看空间和人。

用法：
  python -m scity.admin open-course 名单.json   建班级和课题工作区，设定老师和学生，发放补贴；可重复执行来加人
  python -m scity.admin open-game 开局.json     在一个课题工作区里开一局：分组，加系统对手；原来进行中的局归档
  python -m scity.admin spaces                  空间、工作区和人数
  python -m scity.admin users                   人和所在空间

名单格式：
  {"school": "学校名", "class": "班级名", "course": {"name": "课题名", "ref": "课程包@版本"},
   "teachers": [{"open_id": "ou_…", "name": "姓名"}], "students": [{"user_id": 3}],
   "subsidy": {"points": 3000, "expires": "2027-01-31"}}
人用飞书 open_id 或平台用户编号指定；subsidy 可以不写。补贴由学校出，到期日当天（北京时间）结束后作废。

开局格式：
  {"workspace": 1, "seed": "试玩-第1场",
   "teams": [{"name": "第1组", "members": [3]}, {"name": "稳基置业", "npc": true}]}
课程包取工作区登记的版本；members 是平台用户编号，必须是这个班的学生。
"""
from __future__ import annotations

import argparse
from pathlib import Path
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .course import load_courses
from .game import GameService
from .knowledge import KnowledgeMap
from .models import Major, Membership, Space, User, Workspace, make_session_factory
from .settings import load_settings
from .spaces import SpaceService
from .wallet import WalletService


@dataclass(frozen=True)
class CourseOpened:
    school_id: int
    class_id: int
    workspace_id: int
    teachers: int
    students: int
    granted: int  # 这次新发补贴的人数


def _check(sessions: sessionmaker, spec: dict) -> None:
    """先把名单查一遍，有问题就整份拒绝，不留下建了一半的班级。"""
    for key in ("school", "class"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError(f"名单缺少 {key}")
    if not isinstance(spec.get("course"), dict) or not spec["course"].get("name"):
        raise ValueError("名单缺少 course.name")
    people = spec.get("teachers", []) + spec.get("students", [])
    if not spec.get("students"):
        raise ValueError("名单里没有学生")
    with sessions() as db:
        for person in people:
            if "user_id" in person:
                if db.get(User, person["user_id"]) is None:
                    raise ValueError(f"没有编号为 {person['user_id']} 的用户")
            elif not person.get("open_id"):
                raise ValueError(f"名单里有人既没有 open_id 也没有 user_id：{person}")
            if person.get("major_code") and db.scalar(select(Major.id).where(
                    Major.level == person.get("level", ""), Major.code == person["major_code"])) is None:
                raise ValueError(f"专业目录里没有“{person.get('level', '')} {person['major_code']}”（{person.get('name', '')}）。"
                                 "层次写 本科、高职专科 或 职业本科，代码按教育部专业目录")
    subsidy = spec.get("subsidy")
    if subsidy:
        if not isinstance(subsidy.get("points"), int) or subsidy["points"] <= 0:
            raise ValueError("subsidy.points 必须是大于 0 的整数")
        datetime.strptime(subsidy["expires"], "%Y-%m-%d")


def open_course(sessions: sessionmaker, spaces: SpaceService, wallets: WalletService, spec: dict) -> CourseOpened:
    _check(sessions, spec)
    school = spaces.find_space("school", spec["school"]) or spaces.create_space("school", spec["school"])
    klass = spaces.find_space("class", spec["class"], school) or spaces.create_space("class", spec["class"], school)
    course = spec["course"]
    workspace = (spaces.find_workspace(klass, course["name"])
                 or spaces.create_workspace(klass, "topic", course["name"], course.get("ref", "")))

    knowledge = KnowledgeMap(sessions)

    def enroll(people: list[dict], role: str) -> list[int]:
        ids = []
        for person in people:
            user_id = person.get("user_id") or spaces.ensure_user(person["open_id"])
            if person.get("name"):
                spaces.set_display_name(user_id, person["name"])
            spaces.add_member(user_id, klass, role)
            if person.get("major_code"):  # 名单里可以带上层次和专业代码，如 "level": "本科", "major_code": "082801"（_check 已经核对过）
                knowledge.set_major(user_id, knowledge.find_major(person["level"], person["major_code"]))
            ids.append(user_id)
        return ids

    teachers = enroll(spec.get("teachers", []), "teacher")
    students = enroll(spec["students"], "student")
    granted = 0
    subsidy = spec.get("subsidy")
    if subsidy:
        # 到期日按北京时间当天结束，换成库里用的 UTC 是当天 16:00
        expires = datetime.strptime(subsidy["expires"], "%Y-%m-%d").replace(hour=16)
        ref = f"course-{workspace}"
        for user_id in students:
            if not wallets.has_grant(user_id, ref):
                wallets.grant(subsidy["points"], ref, user_id=user_id, space_id=school, expires_at=expires)
                granted += 1
    return CourseOpened(school, klass, workspace, len(teachers), len(students), granted)


def open_game(sessions: sessionmaker, games: GameService, spec: dict) -> int:
    with sessions() as db:
        workspace = db.get(Workspace, spec.get("workspace"))
        if workspace is None:
            raise ValueError("没有这个工作区")
        students = set(db.scalars(select(Membership.user_id).where(
            Membership.space_id == workspace.space_id, Membership.active, Membership.role == "student")))
    teams = spec.get("teams") or []
    if not any(not t.get("npc") for t in teams):
        raise ValueError("至少要有一个学生组")
    for team in teams:
        strangers = [u for u in team.get("members", []) if u not in students]
        if strangers:
            raise ValueError(f"{team.get('name')} 里有人不是这个班的学生：{strangers}")
    return games.create(workspace.id, workspace.course_ref, spec.get("seed", ""), teams)


def _print_spaces(sessions: sessionmaker) -> None:
    with sessions() as db:
        spaces = db.scalars(select(Space).order_by(Space.id)).all()
        members = dict(db.execute(select(Membership.space_id, func.count()).where(Membership.active)
                                  .group_by(Membership.space_id)).all())
        workspaces = db.scalars(select(Workspace).order_by(Workspace.id)).all()

    def show(parent_id: int | None, depth: int) -> None:
        for space in (s for s in spaces if s.parent_id == parent_id):
            print(f"{'  ' * depth}[{space.id}] {space.kind} {space.name}（{members.get(space.id, 0)} 人）")
            for w in (w for w in workspaces if w.space_id == space.id):
                state = "已归档" if w.archived_at else "进行中"
                print(f"{'  ' * (depth + 1)}· 工作区 [{w.id}] {w.name} {w.course_ref} {state}")
            show(space.id, depth + 1)

    show(None, 0)


def _print_users(sessions: sessionmaker) -> None:
    with sessions() as db:
        users = db.scalars(select(User).order_by(User.id)).all()
        rows = db.execute(select(Membership.user_id, Space.name, Membership.role)
                          .join(Space, Space.id == Membership.space_id).where(Membership.active)).all()
    for user in users:
        places = "；".join(f"{name}({role})" for uid, name, role in rows if uid == user.id) or "未加入任何空间"
        # 飞书账号只显示末 4 位，够核对，不外露完整标识
        print(f"[{user.id}] {user.display_name or '（未登记姓名）'} …{user.feishu_open_id[-4:]} {places}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scity.admin", description="SCity 官方后台（命令行简易版）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("open-course", help="按名单开课").add_argument("roster", help="名单 JSON 文件")
    commands.add_parser("open-game", help="在课题工作区里开一局").add_argument("spec", help="开局 JSON 文件")
    commands.add_parser("spaces", help="空间、工作区和人数")
    commands.add_parser("users", help="人和所在空间")
    commands.add_parser("import-knowledge", help="导入知识图谱总图（专业目录、知识领域、知识点、相近专业）").add_argument("folder", help="放表格的目录")
    commands.add_parser("knowledge", help="总图里现在有多少专业、知识领域和知识点")
    commands.add_parser("check-course", help="检查一个内容型课程包的文件夹能不能上平台").add_argument("folder", help="课程包文件夹")
    args = parser.parse_args(argv)
    if args.command == "check-course":  # 只读文件，不连数据库
        from .content_course import ContentCourse, CourseFormatError, problems
        found = problems(Path(args.folder))
        try:
            course = ContentCourse(Path(args.folder))
            tasks = sum(1 for n in course.nodes.values() if n.fields)
            print(f"{course.name}（{course.ref}）：{len({s.lesson for s in course.stages})} 次课，{len(course.stages)} 个阶段，"
                  f"{tasks} 个阶段要交东西，{len(course.blocks)} 个评价板块，{len(course.points)} 个知识点")
        except CourseFormatError:
            pass
        for line in found:
            print("  要改：" + line)
        print("平台这一关通过。内容对不对、合不合规，还要人看。" if not found else f"还有 {len(found)} 处要改。")
        raise SystemExit(1 if found else 0)
    settings = load_settings()
    sessions = make_session_factory(settings.database_url)
    if args.command == "open-course":
        with open(args.roster, encoding="utf-8") as fh:
            spec = json.load(fh)
        result = open_course(sessions, SpaceService(sessions),
                             WalletService(sessions, settings.pricing, settings.model_costs), spec)
        print(f"已开课：班级 [{result.class_id}]，工作区 [{result.workspace_id}]；"
              f"老师 {result.teachers} 人，学生 {result.students} 人，新发补贴 {result.granted} 人")
    elif args.command == "open-game":
        with open(args.spec, encoding="utf-8") as fh:
            spec = json.load(fh)
        game_id = open_game(sessions, GameService(sessions, load_courses(settings.private_dir)), spec)
        print(f"已开局 [{game_id}]：{len(spec['teams'])} 家公司，等老师点“开始”")
    elif args.command == "import-knowledge":
        report = KnowledgeMap(sessions).import_dir(Path(args.folder))
        print("已导入：专业 {majors}，知识领域 {domains}，知识点 {points}，关键词 {keywords}，知识点关联 {links}，"
              "知识点挂专业 {point_majors}，相近专业 {neighbors} 对".format(**report))
        for line in report["warnings"][:50]:
            print("  注意：" + line)
        if len(report["warnings"]) > 50:
            print(f"  …另有 {len(report['warnings']) - 50} 条")
    elif args.command == "knowledge":
        print(KnowledgeMap(sessions).stats())
    elif args.command == "spaces":
        _print_spaces(sessions)
    else:
        _print_users(sessions)


if __name__ == "__main__":
    main()
