"""学生的知识图（实线泡泡）：老师确认分数后，分数记到每个组员的知识点上。"""
from fastapi.testclient import TestClient
from sqlalchemy import select
from types import SimpleNamespace

from scity.knowledge import KnowledgeMap, tier
from scity.models import KnowledgeDomain, KnowledgeLink, KnowledgeMark, KnowledgePoint
from scity.web import create_app, sign_session
from tests.test_content_course import SURVEY, room  # noqa: F401
from tests.test_web import SECRET, FakeAssistant, FakeLogin


def test_tiers_follow_the_agreed_thresholds():
    assert [tier(s, 1, 0) for s in (None, 3, 5.5, 6, 8, 8.5, 10)] == [0, 0, 0, 2, 2, 3, 3]
    assert tier(None, 3, 0) == 1 and tier(None, 1, 10) == 1 and tier(4, 5, 0) == 1  # 没到 6 分的，接触够多也只到“了解”
    assert tier(9, 1, 0) == 3  # 理解、会用只看老师确认的分


def to_first_gate(room):
    room.next(), room.next()
    room.submit(room.a, SURVEY)
    room.clock.forward(20)
    room.next()
    return room.ctx(room.teacher)


def marks(sessions, user):
    with sessions() as db:
        return {m.point_code: (m.score, m.touches, m.minutes) for m in db.scalars(select(KnowledgeMark).where(KnowledgeMark.user_id == user))}


def test_saving_scores_lights_up_every_member_of_the_team(room, sessions):
    teacher = to_first_gate(room)
    assert marks(sessions, room.a) == {}  # 老师没确认之前，什么都不亮
    room.games.record(room.game, teacher, {"scores": {"第1组": {"B1": 8.5}}})
    # 现场调研（N02）上了 20 分钟：“展开”的 K-SJ-001 记 1 次、20 分钟；“点到”的 K-SJ-002 出现在 N02、N03，记 2 次，不算时间
    expect = {"K-SJ-001": (8.5, 1, 20.0), "K-SJ-002": (None, 2, 0.0)}
    assert marks(sessions, room.a) == expect and marks(sessions, room.b) == expect  # 同组两个人都记上
    assert marks(sessions, room.c) == {} and marks(sessions, room.teacher) == {}    # 没打分的组、老师自己：没有
    room.games.record(room.game, teacher, {"scores": {"第1组": {"B1": 5}, "第2组": {"B1": 9}}})  # 老师改分：跟着改，不重复记
    assert marks(sessions, room.a)["K-SJ-001"] == (5.0, 1, 20.0) and len(marks(sessions, room.a)) == 2
    assert marks(sessions, room.c)["K-SJ-001"][0] == 9.0
    with sessions() as db:
        mark = db.scalar(select(KnowledgeMark).where(KnowledgeMark.user_id == room.c, KnowledgeMark.point_code == "K-SJ-001"))
        assert (mark.game_id, mark.course_ref) == (room.game, "campus-corner@0.1")
    assert room.games.state(room.game).spent == {"n01": 0.0, "n02": 20.0, "n03": 0.0}


def test_my_map_merges_courses_and_only_shows_my_own(room, sessions, spaces, wallets):
    teacher = to_first_gate(room)
    room.games.record(room.game, teacher, {"scores": {"第1组": {"B1": 8.5}, "第2组": {"B1": 4}}})
    with sessions.begin() as db:  # 总图里只登记了其中一个知识点；再加一条别的课里的记录
        d = KnowledgeDomain(code="SJ.1", name="设计调研", group_code="SJ", group_name="设计基础")
        db.add(d)
        db.flush()
        p1 = KnowledgePoint(code="K-SJ-001", name="现场调研方法", domain_id=d.id, summary="去现场看、问、记")
        p2 = KnowledgePoint(code="K-SJ-009", name="访谈提纲", domain_id=d.id)
        db.add_all([p1, p2])
        db.flush()
        db.add(KnowledgeLink(point_id=p1.id, related_id=p2.id))
        db.add(KnowledgeMark(user_id=room.a, point_code="K-SJ-009", game_id=room.game, course_ref="other@1.0", score=None, touches=3))
        db.add(KnowledgeMark(user_id=room.b, point_code="K-SJ-009", game_id=room.game, course_ref="other@1.0", score=None, touches=1))
    km = KnowledgeMap(sessions)
    mine = km.mine(room.a)
    by = {x["code"]: x for x in mine["points"]}
    assert (by["K-SJ-001"]["tier"], by["K-SJ-001"]["name"], by["K-SJ-001"]["domain"], by["K-SJ-001"]["group"]) == ("会用", "现场调研方法", "设计调研", "设计基础")
    assert (by["K-SJ-002"]["tier"], by["K-SJ-002"]["in_map"], by["K-SJ-002"]["name"]) == ("点到", False, "K-SJ-002")  # 总图里还没有的：先显示编号
    assert (by["K-SJ-001"]["touches"], by["K-SJ-001"]["minutes"], by["K-SJ-002"]["touches"]) == (1, 20.0, 2)
    assert (by["K-SJ-009"]["tier"], by["K-SJ-009"]["touches"], by["K-SJ-009"]["courses"]) == ("了解", 3, ["other@1.0"])
    assert mine["links"] == [["K-SJ-001", "K-SJ-009"]] and mine["counts"] == {"点到": 1, "了解": 1, "理解": 0, "会用": 1}
    # 4 分：没到“理解”；但这一步在课上学了 20 分钟，算“了解”。只是点到的那个仍是小点
    assert {x["code"]: x["tier"] for x in km.mine(room.c)["points"]} == {"K-SJ-001": "了解", "K-SJ-002": "点到"}
    empty = km.mine(room.teacher)
    assert (empty["points"], empty["links"], empty["major"], empty["counts"]) == ([], [], None, {"点到": 0, "了解": 0, "理解": 0, "会用": 0})
    settings = SimpleNamespace(spend_cap_yuan=None, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    app = create_app(settings, sessions, spaces, wallets, FakeAssistant(), FakeLogin(), games=room.games)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/api/knowledge/me").status_code == 401
    client.cookies.set("scity_session", sign_session(room.c, SECRET))
    got = client.get("/api/knowledge/me").json()
    assert [x["code"] for x in got["points"]] == ["K-SJ-001", "K-SJ-002"] and "8.5" not in str(got)  # 只有自己的，看不到别的组的分


def test_teacher_sees_the_class_summary_and_single_students_only_while_the_course_runs(room, sessions, spaces, wallets):
    from scity.models import Game, utcnow
    teacher = to_first_gate(room)
    room.games.record(room.game, teacher, {"scores": {"第1组": {"B1": 8.5}, "第2组": {"B1": 4}}})
    ws = teacher.workspace_id
    elsewhere = spaces.create_workspace(teacher.space_id, "topic", "另一门课", "other@1.0")
    with sessions.begin() as db:  # 甲在别的课上学到的：不进这个班这门课的汇总
        other = Game(workspace_id=elsewhere, course_ref="other@1.0")
        db.add(other)
        db.flush()
        db.add(KnowledgeMark(user_id=room.a, point_code="K-SJ-009", game_id=other.id, course_ref="other@1.0", score=9, touches=5))
    spaces.set_display_name(room.a, "甲同学")
    km = KnowledgeMap(sessions)
    data = km.klass(ws)
    assert data["klass"]["size"] == 3 and data["klass"]["open"] is True and data["klass"]["name"] == "校园一角"
    by = {x["code"]: x for x in data["points"]}
    assert set(by) == {"K-SJ-001", "K-SJ-002"}
    one = by["K-SJ-001"]  # 甲、乙 8.5 分“会用”，丙 4 分但课上学了 20 分钟“了解”；全班平均 7 分
    assert (one["lit"], one["dist"], one["score"], one["tier"], one["touches"], one["minutes"]) == (3, [0, 1, 0, 2], 7.0, "理解", 1.0, 20.0)
    assert (by["K-SJ-002"]["dist"], by["K-SJ-002"]["score"], by["K-SJ-002"]["tier"]) == ([3, 0, 0, 0], None, "点到")
    roster = {s["name"]: s for s in data["klass"]["students"]}
    assert roster["甲同学"]["lit"] == 2 and roster["甲同学"]["counts"] == [1, 0, 0, 1] and len(roster) == 3
    assert data["major"] is None and all(x["ring"] is None for x in data["points"])  # 班里没人登记专业：分不出冷暖

    settings = SimpleNamespace(spend_cap_yuan=None, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    app = create_app(settings, sessions, spaces, wallets, FakeAssistant(), FakeLogin(), games=room.games)

    def client(user):
        c = TestClient(app, base_url="https://testserver")
        c.cookies.set("scity_session", sign_session(user, SECRET))
        return c
    t, a = client(room.teacher), client(room.a)
    assert a.get(f"/api/workspaces/{ws}/knowledge").status_code == 403           # 学生看不了全班的
    assert a.get(f"/api/workspaces/{ws}/knowledge/{room.b}").status_code == 403  # 也看不了同学的
    got = t.get(f"/api/workspaces/{ws}/knowledge").json()
    assert got["klass"]["size"] == 3 and len(got["points"]) == 2
    single = t.get(f"/api/workspaces/{ws}/knowledge/{room.a}").json()
    assert single["student"]["name"] == "甲同学" and {x["code"] for x in single["points"]} == {"K-SJ-001", "K-SJ-002", "K-SJ-009"}  # 单个学生是整张图
    assert t.get(f"/api/workspaces/{ws}/knowledge/{room.teacher}").status_code == 404  # 不是这个班的学生
    with sessions.begin() as db:
        db.get(Game, room.game).finished_at = utcnow()
    ended = t.get(f"/api/workspaces/{ws}/knowledge/{room.a}")
    assert ended.status_code == 403 and "只保留全班汇总" in ended.json()["detail"]  # 课程结束后不能再看单个学生
    assert t.get(f"/api/workspaces/{ws}/knowledge").json()["klass"]["open"] is False   # 全班汇总一直能看
