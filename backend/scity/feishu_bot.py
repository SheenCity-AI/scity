"""飞书机器人：长连接收消息，用流式卡片回答，结束后结算点数。

日志只记元数据（时间、消息编号、用量、结果），不记消息内容。
用法：python -m scity.feishu_bot
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict, defaultdict
from uuid import uuid4

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

from .assistant import Assistant
from .models import make_session_factory
from .settings import Settings, load_settings
from .wallet import ChargeResult, ModelCall, WalletService, WalletView

log = logging.getLogger("scity.bot")

ANSWER_ELEMENT = "answer"
META_ELEMENT = "meta"
UPDATE_INTERVAL = 0.3
MENTION = re.compile(r"@_user_\d+")
SEEN_LIMIT = 2000


def build_card() -> str:
    return json.dumps({
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "summary": {"content": "SCity 助手正在回答…"},
            "streaming_config": {"print_frequency_ms": {"default": 30}, "print_step": {"default": 2},
                                 "print_strategy": "fast"},
        },
        "body": {"elements": [
            {"tag": "markdown", "content": "正在想…", "element_id": ANSWER_ELEMENT},
            {"tag": "markdown", "content": " ", "element_id": META_ELEMENT, "text_size": "notation"},
        ]},
    }, ensure_ascii=False)


def format_wallet(view: WalletView) -> str:
    return f"学校补贴 {view.subsidy:,} 点 · 个人余额 {view.balance:,} 点"


def format_charge(result: ChargeResult) -> str:
    return f"本次消耗 {result.points:,} 点 · {format_wallet(result.wallet)}"


def extract_text(content: str) -> str:
    return MENTION.sub("", json.loads(content).get("text", "")).strip()


class FeishuError(RuntimeError):
    pass


def _check(response, action: str):
    if not response.success():
        raise FeishuError(f"{action} 失败：code={response.code} msg={response.msg}")
    return response


class CardStream:
    """一张流式卡片：创建 → 回复到原消息 → 持续更新 → 关闭流式。"""

    def __init__(self, client: lark.Client, message_id: str):
        self._client = client
        self._message_id = message_id
        self._card_id = ""
        self._sequence = 0
        self._latest = ""
        self._pushed = ""
        self._pusher: asyncio.Task | None = None

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    async def open(self) -> None:
        body = CreateCardRequestBody.builder().type("card_json").data(build_card()).build()
        response = _check(await self._client.cardkit.v1.card.acreate(
            CreateCardRequest.builder().request_body(body).build()), "创建卡片")
        self._card_id = response.data.card_id
        content = json.dumps({"type": "card", "data": {"card_id": self._card_id}})
        reply = ReplyMessageRequestBody.builder().msg_type("interactive").content(content).uuid(uuid4().hex).build()
        _check(await self._client.im.v1.message.areply(
            ReplyMessageRequest.builder().message_id(self._message_id).request_body(reply).build()), "回复卡片")
        self._pusher = asyncio.create_task(self._push_loop())

    async def _set(self, element: str, text: str) -> None:
        body = ContentCardElementRequestBody.builder().uuid(uuid4().hex).content(text).sequence(self._next()).build()
        request = (ContentCardElementRequest.builder().card_id(self._card_id).element_id(element)
                   .request_body(body).build())
        _check(await self._client.cardkit.v1.card_element.acontent(request), "更新卡片")

    def update(self, text: str) -> None:
        """只记下最新内容，由后台任务推送；读模型输出的循环不等飞书接口。"""
        self._latest = text

    async def _push_loop(self) -> None:
        while True:
            await asyncio.sleep(UPDATE_INTERVAL)
            if self._latest != self._pushed:
                text = self._latest
                await self._set(ANSWER_ELEMENT, text)
                self._pushed = text

    async def _stop_pusher(self) -> None:
        if self._pusher is None:
            return
        self._pusher.cancel()
        try:
            await self._pusher
        except asyncio.CancelledError:
            pass
        except FeishuError:
            # 中途刷新失败不影响最终结果，结束时还会写一次完整内容。
            log.exception("流式刷新失败")
        self._pusher = None

    async def finish(self, text: str, meta: str) -> None:
        await self._stop_pusher()
        await self._set(ANSWER_ELEMENT, text)
        await self._set(META_ELEMENT, meta)
        settings = json.dumps({"config": {"streaming_mode": False, "summary": {"content": text[:40]}}},
                              ensure_ascii=False)
        body = SettingsCardRequestBody.builder().settings(settings).uuid(uuid4().hex).sequence(self._next()).build()
        _check(await self._client.cardkit.v1.card.asettings(
            SettingsCardRequest.builder().card_id(self._card_id).request_body(body).build()), "关闭流式")


class Bot:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._wallet = WalletService(make_session_factory(settings.database_url), settings.pricing)
        self._assistant = Assistant(settings, self._wallet)
        self._client = lark.Client.builder().app_id(settings.feishu_app_id).app_secret(settings.feishu_app_secret).build()
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="scity-worker").start()

    # 飞书要求 3 秒内处理完事件，这里只登记任务，真正的工作放到后台事件循环。
    def on_message(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        event_id = data.header.event_id
        if event_id in self._seen:
            return
        self._seen[event_id] = None
        while len(self._seen) > SEEN_LIMIT:
            self._seen.popitem(last=False)
        asyncio.run_coroutine_threadsafe(self._guarded(data), self._loop)

    async def _guarded(self, data) -> None:
        message = data.event.message
        try:
            await self._handle(data)
        except Exception:
            log.exception("处理消息失败 message=%s", message.message_id)
            await self._reply_text(message.message_id, "抱歉，这次出错了，请再发一次。")

    async def _reply_text(self, message_id: str, text: str) -> None:
        body = (ReplyMessageRequestBody.builder().msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).uuid(uuid4().hex).build())
        response = await self._client.im.v1.message.areply(
            ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())
        if not response.success():
            log.error("回复文字失败 code=%s msg=%s", response.code, response.msg)

    async def _handle(self, data) -> None:
        message = data.event.message
        message_id = message.message_id
        if message.message_type != "text":
            await self._reply_text(message_id, "我现在只能看文字消息，图片和文件很快支持。")
            return
        text = extract_text(message.content)
        if not text:
            return
        user_id = self._wallet.ensure_user(data.event.sender.sender_id.open_id)
        if text in ("/点数", "点数"):
            await self._reply_text(message_id, format_wallet(self._wallet.view(user_id)))
            return
        async with self._locks[user_id]:
            if text in ("/新对话", "新对话"):
                await self._assistant.reset(user_id)
                await self._reply_text(message_id, "好的，我们重新开始。")
                return
            if self._wallet.view(user_id).available <= 0:
                await self._reply_text(message_id, "你的点数用完了，充值或等学校发放后可以继续使用。")
                return
            await self._answer(user_id, message_id, text)

    async def _answer(self, user_id: int, message_id: str, text: str) -> None:
        started = time.monotonic()
        card = CardStream(self._client, message_id)
        await card.open()
        answer = ""
        calls: list[ModelCall] = []
        failed = False
        try:
            turn = await self._assistant.start_turn(user_id, text)
            calls = turn.calls
            async for chunk in turn.stream():
                answer += chunk
                card.update(answer)
        except Exception:
            failed = True
            log.exception("生成回答失败 message=%s", message_id)
        # 中途出错也按已发生的模型调用结算，保证账本和火山账单对得上。
        result = self._wallet.charge_turn(user_id, calls, message_id)
        log.info("answered message=%s user=%s calls=%s input=%s output=%s points=%s shortfall=%s failed=%s seconds=%.1f",
                 message_id, user_id, len(calls), sum(c.input_tokens for c in calls),
                 sum(c.output_tokens for c in calls), result.points, result.shortfall, failed,
                 time.monotonic() - started)
        if failed:
            answer = (answer + "\n\n" if answer else "") + "抱歉，这次回答中断了，请再发一次。"
        await card.finish(answer or "这次没有生成内容，请换个说法再试一次。", format_charge(result))

    def _warm_up(self) -> None:
        started = time.monotonic()
        try:
            calls = asyncio.run_coroutine_threadsafe(self._assistant.warm_up(), self._loop).result(timeout=90)
        except Exception:
            log.exception("启动热身失败，继续运行")
            return
        # 这点用量不计入任何学生，记在日志里，和火山账单对账时可以核对。
        log.info("warm_up seconds=%.1f input=%s output=%s", time.monotonic() - started,
                 sum(c.input_tokens for c in calls), sum(c.output_tokens for c in calls))

    def run(self) -> None:
        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self.on_message).build()
        self._warm_up()
        client = lark.ws.Client(self._settings.feishu_app_id, self._settings.feishu_app_secret,
                                event_handler=handler, log_level=lark.LogLevel.WARNING)
        log.info("长连接启动 app=%s", self._settings.feishu_app_id)
        client.start()


def main() -> None:
    # VeADK 导入时会接管根日志，这里给机器人单独一个通道，避免运行记录被吞掉。
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    Bot(load_settings()).run()


if __name__ == "__main__":
    main()
