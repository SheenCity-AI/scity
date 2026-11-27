import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scity.personas import persona_for
from scity.wallet import ModelCall
from scity.web import FeishuUser, create_app, sign_session, verify_session

SECRET = "test-secret"


class FakeLogin:
    """假的飞书登录：授权码 code-<open_id> 换出对应的人。"""

    async def exchange(self, code: str, flow: str) -> FeishuUser:
        if not code.startswith("code-"):
            raise PermissionError("bad code")
        return FeishuUser(open_id=code[5:], name="林晓" if code.endswith("ou_s") else "")


class FakeAssistant:
    def __init__(self):
        self.asked = []
        self.fail = False

    async def start_turn(self, ctx, text):
        self.asked.append((ctx.user_id, ctx.workspace_id, text))
        calls, fail = [], self.fail

        class Turn:
            persona = persona_for(ctx)

            def __init__(self):
                self.calls = calls

            async def stream(self):
                yield "你好，"
                calls.append(ModelCall("m", 2000, 0))
                if fail:
                    raise RuntimeError("模型中断")
                yield "我在。"

        return Turn()

    async def history(self, ctx):
        return [{"role": "user", "text": "早"}, {"role": "assistant", "text": "早上好"}]

    async def reset(self, ctx):
        self.asked.append(("reset", ctx.workspace_id))


@pytest.fixture
def web(sessions, spaces, wallets):
    w = SimpleNamespace(assistant=FakeAssistant(), spaces=spaces, wallets=wallets)
    w.settings = SimpleNamespace(spend_cap_yuan=None, demo_enroll=False, web_secret=SECRET, feishu_app_id="cli_test")
    w.app = create_app(w.settings, sessions, spaces, wallets, w.assistant, FakeLogin())
    w.client = TestClient(w.app, base_url="https://testserver")  # 登录 Cookie 只在 https 下发送
    w.student = spaces.ensure_user("ou_s")
    w.school = spaces.create_space("school", "甲学校")
    w.klass = spaces.create_space("class", "一班", parent_id=w.school)
    w.topic = spaces.create_workspace(w.klass, "topic", "沙盘")
    spaces.add_member(w.student, w.klass, "student")
    wallets.grant(100, "g", user_id=w.student, space_id=w.school)
    return w


def login(web, open_id="ou_s"):
    response = web.client.post("/api/auth/feishu", json={"code": f"code-{open_id}"})
    assert response.status_code == 200, response.text
    return response


def events(response):
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_session_token_is_signed_and_expires():
    token = sign_session(7, SECRET, ttl=60)
    assert verify_session(token, SECRET) == 7
    assert verify_session(token, "other-secret") is None
    assert verify_session(token[:-2] + "00", SECRET) is None
    assert verify_session(sign_session(7, SECRET, ttl=-1), SECRET) is None
    assert verify_session("garbage", SECRET) is None


def test_everything_requires_login(web):
    assert web.client.get("/api/me").status_code == 401
    assert web.client.post("/api/chat", json={"text": "你好"}).status_code == 401
    assert web.client.get("/api/auth/config").json() == {"app_id": "cli_test"}
    assert web.client.post("/api/auth/feishu", json={"code": "nope"}).status_code == 401


def test_login_sets_cookie_and_me_lists_own_workspaces(web, spaces):
    response = login(web)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie
    me = web.client.get("/api/me").json()
    assert me["user"] == {"id": web.student, "name": "林晓"}  # 第一次登录时补上姓名
    assert me["wallet"] == {"subsidy": 100, "balance": 0}
    assert me["workspaces"] == [{"id": web.topic, "name": "沙盘", "kind": "topic", "category": "learning",
                                 "space": "甲学校 / 一班", "role": "student", "assistant": "SCity 助手", "course": None}]


def test_chat_streams_then_charges_the_right_place(web, wallets, spaces):
    login(web)
    got = events(web.client.post("/api/chat", json={"text": "你好", "workspace_id": web.topic}))
    assert [e for e, _ in got] == ["delta", "delta", "done"]
    assert "".join(d["text"] for e, d in got if e == "delta") == "你好，我在。"
    done = got[-1][1]
    assert (done["points"], done["wallet"], done["assistant"], done["failed"]) == (2, {"subsidy": 98, "balance": 0}, "SCity 助手", False)
    assert web.assistant.asked == [(web.student, web.topic, "你好")]
    got = events(web.client.post("/api/chat", json={"text": "主聊天"}))
    assert got[-1][1]["wallet"] == {"subsidy": 96, "balance": 0}
    assert web.assistant.asked[-1] == (web.student, None, "主聊天")


def test_interrupted_answer_still_charges_and_says_so(web):
    login(web)
    web.assistant.fail = True
    got = events(web.client.post("/api/chat", json={"text": "你好"}))
    assert [e for e, _ in got] == ["delta", "done"]
    assert got[-1][1]["failed"] is True and got[-1][1]["points"] == 2


def test_cannot_enter_someone_elses_workspace_or_archived_one(web, spaces):
    other = spaces.create_workspace(spaces.create_space("class", "二班"), "topic", "别班")
    login(web)
    assert web.client.post("/api/chat", json={"text": "x", "workspace_id": other}).status_code == 403
    assert web.client.get(f"/api/chat/history?workspace_id={other}").status_code == 403
    spaces.archive_workspace(web.topic)
    assert web.client.post("/api/chat", json={"text": "x", "workspace_id": web.topic}).status_code == 403
    assert web.assistant.asked == []


def test_refusals_are_explained_without_calling_the_model(web, wallets, spaces):
    login(web, "ou_stranger")
    response = web.client.post("/api/chat", json={"text": "你好"})
    assert response.status_code == 402 and "还没有加入任何课程" in response.json()["detail"]
    login(web)
    web.settings.spend_cap_yuan = 0.001
    wallets.record_system_usage([ModelCall("m", 2000, 0)], "warmup", "boot")
    response = web.client.post("/api/chat", json={"text": "你好"})
    assert response.status_code == 402 and "额度已经用完" in response.json()["detail"]
    assert web.assistant.asked == []


def test_history_and_reset_are_scoped_to_the_workspace(web):
    login(web)
    assert web.client.get(f"/api/chat/history?workspace_id={web.topic}").json()["messages"][1]["text"] == "早上好"
    assert web.client.post("/api/chat/reset", json={"workspace_id": web.topic}).status_code == 200
    assert web.assistant.asked == [("reset", web.topic)]


def test_empty_or_oversized_message_is_rejected(web):
    login(web)
    assert web.client.post("/api/chat", json={"text": "   "}).status_code == 422
    assert web.client.post("/api/chat", json={"text": "字" * 8001}).status_code == 422


async def test_feishu_login_exchanges_code_for_person_in_both_flows():
    import httpx

    from scity.web import FeishuLogin
    seen = []

    def feishu(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.url.path, body.get("code"), request.headers.get("authorization")))
        if request.url.path.endswith("/authen/v2/oauth/token"):
            ok = body == {"grant_type": "authorization_code", "client_id": "cli_x", "client_secret": "s3", "code": "c2"}
            return httpx.Response(200, json={"code": 0, "access_token": "u-v2"} if ok else {"code": 20003})
        if request.url.path.endswith("/app_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "app_access_token": "a-1"})
        if request.url.path.endswith("/oidc/access_token"):
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "u-v1"}})
        return httpx.Response(200, json={"code": 0, "data": {"open_id": "ou_real", "name": "宋晓宇"}})

    login = FeishuLogin("cli_x", "s3", transport=httpx.MockTransport(feishu))
    assert await login.exchange("c2", "v2") == FeishuUser("ou_real", "宋晓宇")
    assert seen[-1] == ("/open-apis/authen/v1/user_info", None, "Bearer u-v2")
    assert await login.exchange("c1", "v1") == FeishuUser("ou_real", "宋晓宇")
    assert seen[-1][2] == "Bearer u-v1" and seen[-2][2] == "Bearer a-1"
    with pytest.raises(PermissionError) as error:
        await login.exchange("wrong", "v2")
    assert "s3" not in str(error.value)
