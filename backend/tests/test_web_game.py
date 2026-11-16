from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scity.game import GameService
from scity.web import create_app, sign_session
from tests.test_game import Clock, ToyCourse
from tests.test_web import SECRET, FakeAssistant, FakeLogin


@pytest.fixture
def site(sessions, spaces, wallets):
    s = SimpleNamespace(clock=Clock())
    s.games = GameService(sessions, {"toy@1": ToyCourse()}, now=s.clock)
    settings = SimpleNamespace(spend_cap_yuan=None, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    app = create_app(settings, sessions, spaces, wallets, FakeAssistant(), FakeLogin(), games=s.games)
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
