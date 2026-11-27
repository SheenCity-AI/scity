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
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from .chat import CAP_REACHED, refusal
from .demo import DemoEnrollment
from .game import GameError, GameService
from .knowledge import KnowledgeMap
from .models import MAJOR_LEVELS
from .personas import persona_for
from .spaces import Context, NotMember, SpaceService, WorkspaceArchived
from .wallet import SpendCapReached, WalletService, WalletView

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


class MajorBody(BaseModel):
    major_id: int


MAX_TALK = 300


class TalkBody(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def not_blank_or_long(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_TALK:
            raise ValueError(f"要说的话不能为空，也不能超过 {MAX_TALK} 字")
        return value


def _wallet(view: WalletView) -> dict:
    return {"subsidy": view.subsidy, "balance": view.balance}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(settings, sessions, spaces: SpaceService, wallets: WalletService, assistant, login,
               games: GameService | None = None, roles=None) -> FastAPI:
    if not settings.web_secret:
        raise RuntimeError("没有配置 SCITY_WEB_SECRET，网页后台不能启动")
    app = FastAPI(title="SCity", docs_url=None, redoc_url=None, openapi_url=None)
    demo = DemoEnrollment(sessions, spaces, wallets)
    locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    knowledge = KnowledgeMap(sessions)
    talk_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # 一局里同一时间只让一句话在问：次数才数得准

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
                "course": games.cover(w.id) if games and w.kind == "topic" else None,
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

    @app.get("/api/knowledge/me")
    def my_knowledge(user_id: int = Depends(current_user)) -> dict:
        """自己的知识图（实线泡泡）：只能看自己的。"""
        return knowledge.mine(user_id)

    def as_teacher(user_id: int, workspace_id: int) -> None:
        if context(user_id, workspace_id).role != "teacher":
            raise HTTPException(403, "只有这个班的任课老师可以看")

    @app.get("/api/workspaces/{workspace_id}/knowledge")
    def class_knowledge(workspace_id: int, user_id: int = Depends(current_user)) -> dict:
        """任课老师看本班的全班汇总：每个知识点多少人点亮、各档各几人。看不到聊天内容。"""
        as_teacher(user_id, workspace_id)
        return knowledge.klass(workspace_id)

    @app.get("/api/workspaces/{workspace_id}/knowledge/{student_id}")
    def student_knowledge(workspace_id: int, student_id: int, user_id: int = Depends(current_user)) -> dict:
        """任课老师在上课期间看本班一个学生的图；课程结束后不能再看。"""
        as_teacher(user_id, workspace_id)
        try:
            data = knowledge.student_in(workspace_id, student_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        log.info("student map viewed teacher=%s workspace=%s student=%s", user_id, workspace_id, student_id)
        return data

    @app.get("/api/knowledge/demo")
    def demo_knowledge(user_id: int = Depends(current_user)) -> dict:
        """演示用的知识图：不是任何人的真实数据，页面上会写明。给还没有上过课的人（比如老师）看效果。"""
        return knowledge.demo()

    @app.get("/api/majors")
    def majors(level: str, q: str = "", user_id: int = Depends(current_user)) -> dict:
        """本人选专业时查专业目录（教育部本科、职业教育两套）。"""
        if level not in MAJOR_LEVELS or len(q) > 20:
            raise HTTPException(422, "层次只能是 " + "、".join(MAJOR_LEVELS))
        return {"majors": knowledge.search_majors(level, q)}

    @app.post("/api/me/major")
    def set_major(body: MajorBody, user_id: int = Depends(current_user)) -> dict:
        """登记或改自己的专业。知识图按它分圈；改了以后图会重新排，已经点亮的知识点不变。"""
        try:
            major = knowledge.set_major(user_id, body.major_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        log.info("major set user=%s major=%s", user_id, body.major_id)
        return {"ok": True, "major": major}

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
            # 记下被规则拒绝的原因，方便事后查同学卡在哪（只记原因，不记填报内容）
            log.info("game rejected user=%s workspace=%s game=%s reason=%s", user_id, workspace_id, game_id, exc)
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

    @app.post("/api/workspaces/{workspace_id}/game/upload")
    async def game_upload(workspace_id: int, field: str = Form(...), file: UploadFile = File(...), user_id: int = Depends(current_user)) -> dict:
        """学生上传一个文件（作业截图、图纸）。返回一个只有本组和任课老师能打开的地址，学生再把它作为这一项交上来。"""
        ctx, game_id = game_of(user_id, workspace_id)
        if game_id is None:
            raise HTTPException(404, "这个课题还没有开局")
        try:
            slot = games.upload_slot(game_id, ctx, field)
        except GameError as exc:
            raise HTTPException(409, str(exc)) from exc
        name = os.path.basename((file.filename or "文件").replace("\\", "/"))[:120] or "文件"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if slot["exts"] and ext not in slot["exts"]:
            raise HTTPException(415, f"这一项只收 {'、'.join(slot['exts'])} 格式的文件")
        token = uuid4().hex + uuid4().hex[:8]
        folder = settings.upload_dir / str(game_id)
        folder.mkdir(parents=True, exist_ok=True)
        path, size, limit = folder / f"{token}.{ext or 'bin'}", 0, slot["mb"] * 1024 * 1024
        try:
            with open(path, "wb") as out:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"文件超过 {slot['mb']}MB 了，压缩一下或拆开再传")
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if size == 0:
            path.unlink(missing_ok=True)
            raise HTTPException(422, "这是一个空文件")
        games.save_upload(slot, token, name, size, str(path))
        log.info("uploaded user=%s workspace=%s game=%s field=%s size=%s", user_id, workspace_id, game_id, field, size)
        return {"ok": True, "url": f"/api/files/{token}", "name": name, "size": size}

    @app.get("/api/files/{token}")
    def get_file(token: str, user_id: int = Depends(current_user)):
        """打开一个上传的文件：同组成员和任课老师能看，别人看到的是“没有这个文件”。"""
        try:
            f = games.open_file(token, user_id)
        except GameError as exc:
            raise HTTPException(404, str(exc)) from exc
        inline = f.name.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "gif", "webp", "pdf")
        return FileResponse(f.path, filename=f.name, content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600",
                                     "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"})

    @app.post("/api/workspaces/{workspace_id}/game/talk")
    async def game_talk(workspace_id: int, body: TalkBody, user_id: int = Depends(current_user)) -> dict:
        """学生和课里的 AI 角色说一句话。每组次数有限；结果仍由课程规则算，这里只记下一问一答。"""
        ctx, game_id = game_of(user_id, workspace_id)
        if game_id is None or roles is None:
            raise HTTPException(404, "这个课题现在没有可以对话的角色")
        try:
            wallets.check_spend_cap(settings.spend_cap_yuan)
        except SpendCapReached as exc:
            raise HTTPException(402, CAP_REACHED) from exc
        ref = f"talk-{uuid4().hex}"
        async with talk_locks[game_id]:
            started = time.monotonic()
            try:
                seat = games.talk_seat(game_id, ctx)
            except GameError as exc:
                log.info("game rejected user=%s workspace=%s game=%s reason=%s", user_id, workspace_id, game_id, exc)
                raise HTTPException(409, str(exc)) from exc
            try:
                answer, calls = await roles.reply(seat.prompt, seat.history, body.text)
            except Exception as exc:
                log.exception("角色回答失败 ref=%s", ref)
                raise HTTPException(502, f"{seat.name}没有听清，请再说一遍（这一次不算次数）") from exc
            # 课里的对话属于上课，点数不够也不拦（超出的部分记为缺口，事后调这门课的预算）
            result = wallets.charge_turn(ctx, calls, ref, purpose="role")
            try:
                left = games.talk_save(game_id, ctx, seat.stage, body.text, answer)
            except GameError as exc:
                raise HTTPException(409, str(exc)) from exc
        log.info("talked ref=%s user=%s workspace=%s game=%s stage=%s left=%s points=%s seconds=%.1f",
                 ref, user_id, workspace_id, game_id, seat.stage, left, result.points, time.monotonic() - started)
        return {"ok": True, "name": seat.name, "answer": answer, "left": left, "version": games.version(game_id)}

    return app


def main() -> None:
    import uvicorn

    from .assistant import Assistant
    from .course import load_courses
    from .models import make_session_factory
    from .roles import RolePlayer
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
    games = GameService(sessions, courses)
    app = create_app(settings, sessions, spaces, wallets, Assistant(settings, spaces, wallets, briefing=games.brief),
                     FeishuLogin(settings.feishu_app_id, settings.feishu_app_secret), games=games,
                     roles=RolePlayer(settings))
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SCITY_WEB_PORT", "8000")), access_log=False,
                log_level="warning")


if __name__ == "__main__":
    main()
