from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select

from scity.game import GameService
from scity.models import UsageRecord
from scity.wallet import ModelCall
from scity.web import create_app, sign_session
from tests.test_game import Clock, ToyCourse
from tests.test_web import SECRET, FakeAssistant, FakeLogin


class FakeRoles:
    def __init__(self):
        self.asked, self.fail = [], False

    async def reply(self, prompt, history, text):
        if self.fail:
            raise RuntimeError("模型中断")
        self.asked.append((prompt, history, text))
        return f"收到：{text}", [ModelCall("m", 3000, 0)]


@pytest.fixture
def site(sessions, spaces, wallets):
    s = SimpleNamespace(clock=Clock())
    s.games = GameService(sessions, {"toy@1": ToyCourse()}, now=s.clock)
    settings = SimpleNamespace(spend_cap_yuan=None, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    s.roles = FakeRoles()
    app = create_app(settings, sessions, spaces, wallets, FakeAssistant(), FakeLogin(), games=s.games, roles=s.roles)
    s.teacher, s.a, s.c = (spaces.ensure_user(x) for x in ("ou_t", "ou_a", "ou_c"))
    klass = spaces.create_space("class", "一班")
    s.ws = spaces.create_workspace(klass, "topic", "玩具课", "toy@1")
    s.empty = spaces.create_workspace(klass, "topic", "没开局的课题", "toy@1")
    spaces.add_member(s.teacher, klass, "teacher")
    spaces.add_member(s.a, klass, "student")
    spaces.add_member(s.c, klass, "student")
    s.game = s.games.create(s.ws, "toy@1", "s", [{"name": "第1组", "members": [s.a]}, {"name": "第2组", "members": [s.c]}])

    def client(user):
        c = TestClient(app, base_url="https://testserver")
        c.cookies.set("scity_session", sign_session(user, SECRET))
        return c

    s.as_teacher, s.as_a, s.as_c = client(s.teacher), client(s.a), client(s.c)
    s.url = f"/api/workspaces/{s.ws}/game"
    return s


def test_state_is_role_aware_and_cheap_to_poll(site):
    assert site.as_a.get(f"/api/workspaces/{site.empty}/game").json() == {"game": None}
    student = site.as_a.get(site.url).json()
    assert (student["role"], student["team"], student["stage"]) == ("student", "第1组", None)
    version = student["version"]
    assert site.as_a.get(f"{site.url}?since={version}").json() == {"game": site.game, "version": version, "changed": False}
    assert site.as_teacher.post(f"{site.url}/advance").status_code == 200
    fresh = site.as_a.get(f"{site.url}?since={version}").json()
    assert fresh["version"] > version and fresh["stage"]["key"] == "intro"


def test_student_submits_teacher_drives_and_errors_are_readable(site):
    assert site.as_a.post(f"{site.url}/advance").json() == {"detail": "只有老师可以做这个操作"}
    site.as_teacher.post(f"{site.url}/advance")
    site.as_teacher.post(f"{site.url}/advance")
    bad = site.as_a.post(f"{site.url}/submit", json={"payload": {"price": 1}})
    assert bad.status_code == 409 and bad.json()["detail"] == "报价不能低于 100"
    assert site.as_a.post(f"{site.url}/submit", json={"payload": {"price": 300}}).status_code == 200
    assert site.as_c.get(site.url).json()["course"] == {"mine": None, "winner": None}
    assert site.as_teacher.get(site.url).json()["submitted"] == {"第1组": True, "第2组": False}
    assert site.as_teacher.post(f"{site.url}/extend", json={"minutes": 5}).status_code == 200
    assert site.as_teacher.post(f"{site.url}/extend", json={"minutes": 5}).status_code == 409
    site.clock.forward(15)
    site.as_teacher.post(f"{site.url}/advance")
    assert site.as_teacher.post(f"{site.url}/record", json={"payload": {"lot": "A"}}).status_code == 200
    assert site.as_c.get(site.url).json()["course"]["winner"] == "第1组"


def test_outsiders_and_anonymous_are_kept_out(site, spaces):
    outsider = spaces.ensure_user("ou_x")
    stranger = TestClient(site.as_a.app, base_url="https://testserver")
    assert stranger.get(site.url).status_code == 401
    stranger.cookies.set("scity_session", sign_session(outsider, SECRET))
    assert stranger.get(site.url).status_code == 403
    assert stranger.post(f"{site.url}/submit", json={"payload": {"price": 300}}).status_code == 403


def test_rejections_are_logged_with_their_reason_but_not_the_payload(site, caplog):
    site.as_teacher.post(f"{site.url}/advance")
    site.as_teacher.post(f"{site.url}/advance")
    with caplog.at_level("INFO", logger="scity.web"):
        site.as_a.post(f"{site.url}/submit", json={"payload": {"price": 1}})
    line = next(r.getMessage() for r in caplog.records if "game rejected" in r.getMessage())
    assert "reason=报价不能低于 100" in line and "price" not in line



def test_students_talk_to_a_course_role_a_limited_number_of_times(site, sessions):
    talk = lambda who, text: who.post(site.url + "/talk", json={"text": text})
    site.as_teacher.post(site.url + "/advance")
    assert talk(site.as_a, "你好").status_code == 409  # 开场阶段没有角色
    site.as_teacher.post(site.url + "/advance")
    assert talk(site.as_teacher, "我是老师").status_code == 409 and talk(site.as_a, "  ").status_code == 422
    first = talk(site.as_a, "最低报多少？").json()
    assert (first["name"], first["answer"], first["left"]) == ("拍卖师", "收到：最低报多少？", 1)
    prompt, history, text = site.roles.asked[-1]
    assert "第1组" in prompt and history == () and text == "最低报多少？"
    site.roles.fail = True
    failed = talk(site.as_a, "能改吗？")
    assert failed.status_code == 502 and "不算次数" in failed.json()["detail"]
    site.roles.fail = False
    assert talk(site.as_a, "能改吗？").json()["left"] == 0
    assert site.roles.asked[-1][1] == (("最低报多少？", "收到：最低报多少？"),)  # 接着上一句谈
    done = talk(site.as_a, "再问一句")
    assert done.status_code == 409 and "次数已经用完" in done.json()["detail"]
    assert talk(site.as_c, "我们组呢？").json()["left"] == 1  # 各组各算各的
    with sessions() as db:  # 用量记在“课里的角色”名下，和助手聊天分开
        usage = db.scalars(select(UsageRecord).where(UsageRecord.purpose == "role")).all()
    assert len(usage) == 3 and {u.user_id for u in usage} == {site.a, site.c}
    view = site.as_a.get(site.url).json()
    assert view["version"] == site.as_a.get(site.url, params={"since": 0}).json()["version"]


def test_talk_needs_a_role_player_and_respects_the_platform_spend_cap(sessions, spaces, wallets):
    games = GameService(sessions, {"toy@1": ToyCourse()}, now=Clock())
    settings = SimpleNamespace(spend_cap_yuan=0.0, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    user = spaces.ensure_user("ou_a")
    klass = spaces.create_space("class", "一班")
    ws = spaces.create_workspace(klass, "topic", "玩具课", "toy@1")
    spaces.add_member(user, klass, "student")
    games.create(ws, "toy@1", "s", [{"name": "第1组", "members": [user]}])
    for roles, code in ((None, 404), (FakeRoles(), 402)):
        app = create_app(settings, sessions, spaces, wallets, FakeAssistant(), FakeLogin(), games=games, roles=roles)
        client = TestClient(app, base_url="https://testserver")
        client.cookies.set("scity_session", sign_session(user, SECRET))
        assert client.post(f"/api/workspaces/{ws}/game/talk", json={"text": "你好"}).status_code == code
