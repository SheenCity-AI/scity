"""网页后台：飞书里打开网页应用后，识别是谁、列出他的工作区、在页面里和助手对话。

登录：前端用飞书 JSSDK 拿授权码，POST /api/auth/feishu 换成登录状态（签名 Cookie）。
对话：POST /api/chat，以 text/event-stream 返回，事件 delta（一段文字）和 done（扣点结果）。
日志只记元数据，不记对话内容。用法：python -m scity.web
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from .chat import refusal
from .demo import DemoEnrollment
from .game import GameError, GameService
from .personas import persona_for
from .spaces import Context, NotMember, SpaceService, WorkspaceArchived
from .wallet import WalletService, WalletView

log = logging.getLogger("scity.web")

COOKIE = "scity_session"
SESSION_TTL = 7 * 24 * 3600
MAX_MESSAGE = 8000
FEISHU = "https://open.feishu.cn/open-apis"


def sign_session(user_id: int, secret: str, ttl: int = SESSION_TTL) -> str:
    body = f"{user_id}.{int(time.time()) + ttl}"
    return f"{body}.{hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()}"


def verify_session(token: str, secret: str) -> int | None:
    try:
        user_id, expires, signature = token.split(".")
        expected = hmac.new(secret.encode(), f"{user_id}.{expires}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(signature, expected) and int(expires) > time.time():
            return int(user_id)
    except ValueError:
        pass
    return None


@dataclass(frozen=True)
class FeishuUser:
    open_id: str
    name: str = ""


class FeishuLogin:
    """用网页应用的授权码换出当前飞书用户。

    flow=v2：JSSDK requestAccess 拿到的授权码；flow=v1：旧版 requestAuthCode 拿到的授权码。
    """

    def __init__(self, app_id: str, app_secret: str, transport: httpx.AsyncBaseTransport | None = None):
        self._app_id = app_id
        self._app_secret = app_secret
        self._transport = transport

    async def exchange(self, code: str, flow: str) -> FeishuUser:
        async with httpx.AsyncClient(transport=self._transport, timeout=15, trust_env=False) as http:
            if flow == "v1":
                app = await self._call(http.post(f"{FEISHU}/auth/v3/app_access_token/internal",
                                                 json={"app_id": self._app_id, "app_secret": self._app_secret}))
                token = (await self._call(http.post(
                    f"{FEISHU}/authen/v1/oidc/access_token", json={"grant_type": "authorization_code", "code": code},
                    headers={"Authorization": f"Bearer {app['app_access_token']}"})))["data"]["access_token"]
            else:
                token = (await self._call(http.post(f"{FEISHU}/authen/v2/oauth/token", json={
                    "grant_type": "authorization_code", "client_id": self._app_id,
                    "client_secret": self._app_secret, "code": code})))["access_token"]
            info = (await self._call(http.get(f"{FEISHU}/authen/v1/user_info",
                                              headers={"Authorization": f"Bearer {token}"})))["data"]
        return FeishuUser(open_id=info["open_id"], name=info.get("name", ""))

    @staticmethod
    async def _call(request) -> dict:
        data = (await request).json()
        if data.get("code") != 0:
            # 只报错误码，不带回飞书返回的正文
            raise PermissionError(f"飞书登录失败 code={data.get('code')}")
        return data


class LoginBody(BaseModel):
    code: str
    flow: str = "v2"


class ChatBody(BaseModel):
    text: str
    workspace_id: int | None = None

    @field_validator("text")
    @classmethod
    def not_blank_or_huge(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_MESSAGE:
            raise ValueError(f"消息不能为空，也不能超过 {MAX_MESSAGE} 字")
        return value


class ResetBody(BaseModel):
    workspace_id: int | None = None


class PayloadBody(BaseModel):
    payload: dict


class ExtendBody(BaseModel):
    minutes: int


def _wallet(view: WalletView) -> dict:
    return {"subsidy": view.subsidy, "balance": view.balance}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(settings, sessions, spaces: SpaceService, wallets: WalletService, assistant, login,
               games: GameService | None = None) -> FastAPI:
    if not settings.web_secret:
        raise RuntimeError("没有配置 SCITY_WEB_SECRET，网页后台不能启动")
    app = FastAPI(title="SCity", docs_url=None, redoc_url=None, openapi_url=None)
    demo = DemoEnrollment(sessions, spaces, wallets)
    locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def current_user(request: Request) -> int:
        user_id = verify_session(request.cookies.get(COOKIE, ""), settings.web_secret)
        if user_id is None:
            raise HTTPException(401, "请从飞书重新打开")
        return user_id

    def context(user_id: int, workspace_id: int | None) -> Context:
        try:
            return spaces.context(user_id, workspace_id)
        except (NotMember, WorkspaceArchived) as exc:
            raise HTTPException(403, "你不能进入这个工作区，或者它已经归档") from exc

    @app.get("/api/auth/config")
    def auth_config() -> dict:
        return {"app_id": settings.feishu_app_id}

    @app.post("/api/auth/feishu")
    async def auth_feishu(body: LoginBody, response: Response) -> dict:
        try:
            who = await login.exchange(body.code, body.flow)
        except (PermissionError, KeyError, httpx.HTTPError) as exc:
            log.warning("login_failed flow=%s error=%s", body.flow, type(exc).__name__)
            raise HTTPException(401, "飞书登录失败，请重新打开") from exc
        user_id = demo.enroll(who.open_id) if settings.demo_enroll else spaces.ensure_user(who.open_id)
        if who.name and not spaces.display_name(user_id):
            spaces.set_display_name(user_id, who.name)
        response.set_cookie(COOKIE, sign_session(user_id, settings.web_secret), max_age=SESSION_TTL,
                            httponly=True, secure=True, samesite="lax", path="/")
        log.info("login user=%s flow=%s", user_id, body.flow)
        return {"ok": True}

    @app.get("/api/me")
    def me(user_id: int = Depends(current_user)) -> dict:
        roles = {m.space_id: m.role for m in spaces.memberships(user_id)}
        return {
            "user": {"id": user_id, "name": spaces.display_name(user_id)},
            "wallet": _wallet(wallets.view(Context(user_id))),
            "workspaces": [{
                "id": w.id, "name": w.name, "kind": w.kind, "category": w.category,
                "space": spaces.space_path(w.space_id), "role": roles[w.space_id],
                "assistant": persona_for(Context(user_id, w.space_id, w.id, roles[w.space_id], w.category)).title,
            } for w in spaces.workspaces(user_id)],
        }

    @app.get("/api/chat/history")
    async def history(workspace_id: int | None = None, user_id: int = Depends(current_user)) -> dict:
        ctx = context(user_id, workspace_id)
        return {"assistant": persona_for(ctx).title, "wallet": _wallet(wallets.view(ctx)),
                "messages": await assistant.history(ctx)}

    @app.post("/api/chat/reset")
    async def reset(body: ResetBody, user_id: int = Depends(current_user)) -> dict:
        ctx = context(user_id, body.workspace_id)
        async with locks[user_id]:
            await assistant.reset(ctx)
        return {"ok": True}

    @app.post("/api/chat")
    async def chat(body: ChatBody, user_id: int = Depends(current_user)) -> StreamingResponse:
        ctx = context(user_id, body.workspace_id)
        reason = refusal(spaces, wallets, ctx, settings.spend_cap_yuan)
        if reason:
            raise HTTPException(402, reason)
        ref = f"web-{uuid4().hex}"

        async def stream():
            started = time.monotonic()
            calls, failed, persona = [], False, persona_for(ctx)
            async with locks[user_id]:
                try:
                    turn = await assistant.start_turn(ctx, body.text)
                    calls, persona = turn.calls, turn.persona
                    async for chunk in turn.stream():
                        yield _sse("delta", {"text": chunk})
                except Exception:
                    failed = True
                    log.exception("生成回答失败 ref=%s", ref)
                # 中途出错也按已发生的模型调用结算
                result = wallets.charge_turn(ctx, calls, ref)
            log.info("answered ref=%s user=%s workspace=%s calls=%s points=%s shortfall=%s failed=%s seconds=%.1f",
                     ref, user_id, ctx.workspace_id, len(calls), result.points, result.shortfall, failed,
                     time.monotonic() - started)
            yield _sse("done", {"points": result.points, "wallet": _wallet(result.wallet),
                                "assistant": persona.title, "failed": failed})

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---------- 课题引擎：一局课的状态、填报和老师操作 ----------

    def game_of(user_id: int, workspace_id: int) -> tuple[Context, int | None]:
        ctx = context(user_id, workspace_id)
        return ctx, games.active(workspace_id) if games else None

    def act(user_id: int, workspace_id: int, action) -> dict:
        ctx, game_id = game_of(user_id, workspace_id)
        if game_id is None:
            raise HTTPException(404, "这个课题还没有开局")
        try:
            action(game_id, ctx)
        except GameError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "version": games.version(game_id)}

    @app.get("/api/workspaces/{workspace_id}/game")
    def game_state(workspace_id: int, since: int | None = None, user_id: int = Depends(current_user)) -> dict:
        """各台电脑每隔几秒问一次；带上 since 时，没变化只回一个很小的应答。"""
        ctx, game_id = game_of(user_id, workspace_id)
        if game_id is None:
            return {"game": None}
        version = games.version(game_id)
        if since is not None and since == version:
            return {"game": game_id, "version": version, "changed": False}
        return games.view(game_id, ctx)

    @app.post("/api/workspaces/{workspace_id}/game/submit")
    def game_submit(workspace_id: int, body: PayloadBody, user_id: int = Depends(current_user)) -> dict:
        return act(user_id, workspace_id, lambda game_id, ctx: games.submit(game_id, ctx, body.payload))

    @app.post("/api/workspaces/{workspace_id}/game/advance")
    def game_advance(workspace_id: int, user_id: int = Depends(current_user)) -> dict:
        return act(user_id, workspace_id, lambda game_id, ctx: games.advance(game_id, ctx))

    @app.post("/api/workspaces/{workspace_id}/game/extend")
    def game_extend(workspace_id: int, body: ExtendBody, user_id: int = Depends(current_user)) -> dict:
        return act(user_id, workspace_id, lambda game_id, ctx: games.extend(game_id, ctx, body.minutes))

    @app.post("/api/workspaces/{workspace_id}/game/record")
    def game_record(workspace_id: int, body: PayloadBody, user_id: int = Depends(current_user)) -> dict:
        return act(user_id, workspace_id, lambda game_id, ctx: games.record(game_id, ctx, body.payload))

    return app


def main() -> None:
    import uvicorn

    from .assistant import Assistant
    from .course import load_courses
    from .models import make_session_factory
    from .settings import load_settings

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    settings = load_settings()
    sessions = make_session_factory(settings.database_url)
    spaces = SpaceService(sessions)
    wallets = WalletService(sessions, settings.pricing, settings.model_costs)
    courses = load_courses(settings.private_dir)
    log.info("courses=%s", sorted(courses))
    app = create_app(settings, sessions, spaces, wallets, Assistant(settings, spaces, wallets),
                     FeishuLogin(settings.feishu_app_id, settings.feishu_app_secret),
                     games=GameService(sessions, courses))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SCITY_WEB_PORT", "8000")), access_log=False,
                log_level="warning")


if __name__ == "__main__":
    main()
