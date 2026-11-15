import asyncio
import json
import time

from types import SimpleNamespace

from scity.demo import DEMO_BALANCE, DEMO_SUBSIDY, DemoEnrollment
from scity.feishu_bot import (ANSWER_ELEMENT, CAP_REACHED, META_ELEMENT, Bot, CardStream, FeishuError, build_card,
                              extract_text, format_charge)
from scity.settings import DEFAULT_COSTS
from scity.wallet import ChargeResult, ModelCall, WalletView


def test_extract_text_removes_mentions():
    assert extract_text(json.dumps({"text": "@_user_1 你好 @_user_2"})) == "你好"


def test_card_is_streaming_with_both_elements():
    card = json.loads(build_card())
    assert card["config"]["streaming_mode"] is True
    assert [e["element_id"] for e in card["body"]["elements"]] == [ANSWER_ELEMENT, META_ELEMENT]


def test_format_charge():
    text = format_charge(ChargeResult(12, 12, 0, 0, WalletView(2988, 260)))
    assert text == "本次消耗 12 点 · 学校补贴 2,988 点 · 个人余额 260 点"


class SlowCard(CardStream):
    """飞书接口每次要 50 毫秒的卡片，用来验证读模型的循环不会被它拖慢。"""

    def __init__(self):
        super().__init__(client=None, message_id="m")
        self.pushes: list[tuple[str, str]] = []

    async def _set(self, element, text):
        await asyncio.sleep(0.05)
        self.pushes.append((element, text))

    async def finish_settings_skipped(self):
        await self._stop_pusher()


async def test_slow_card_api_does_not_block_the_model_stream(monkeypatch):
    monkeypatch.setattr("scity.feishu_bot.UPDATE_INTERVAL", 0.02)
    card = SlowCard()
    card._pusher = asyncio.create_task(card._push_loop())
    started = time.monotonic()
    answer = ""
    for i in range(200):
        answer += str(i)
        card.update(answer)
        await asyncio.sleep(0.001)
    elapsed = time.monotonic() - started
    await card.finish_settings_skipped()
    # 200 段内容如果逐段等接口，至少要 10 秒
    assert elapsed < 2
    assert 0 < len(card.pushes) < 200


async def test_failed_refresh_is_contained():
    card = SlowCard()

    async def boom():
        raise FeishuError("x")

    card._pusher = asyncio.create_task(boom())
    await asyncio.sleep(0)
    await card._stop_pusher()
    assert card._pusher is None


class FakeCard:
    instances: list["FakeCard"] = []

    def __init__(self, client, message_id):
        self.finished = None
        FakeCard.instances.append(self)

    async def open(self):
        pass

    def update(self, text):
        pass

    async def finish(self, text, meta):
        self.finished = (text, meta)


class FakeTurn:
    def __init__(self, calls):
        self.calls = calls

    async def stream(self):
        yield "前半段"
        self.calls.append(ModelCall("m", 1000, 0))
        raise RuntimeError("模型中断")


def make_bot(sessions, spaces, wallets, turn, cap=None):
    bot = Bot.__new__(Bot)
    bot._client = None
    bot._settings = SimpleNamespace(spend_cap_yuan=cap)
    bot._spaces, bot._wallet = spaces, wallets
    bot._demo = DemoEnrollment(sessions, spaces, wallets)
    bot._locks = {}
    bot.replies = []

    class FakeAssistant:
        async def start_turn(self, ctx, text):
            return turn

    async def reply_text(message_id, text):
        bot.replies.append(text)

    bot._assistant = FakeAssistant()
    bot._reply_text = reply_text
    return bot


def message(text, open_id="ou_a"):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(message_id="m1", message_type="text", content=json.dumps({"text": text})),
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id))))


async def test_interrupted_answer_closes_card_and_charges_actual_usage(sessions, spaces, wallets, monkeypatch):
    monkeypatch.setattr("scity.feishu_bot.CardStream", FakeCard)
    bot = make_bot(sessions, spaces, wallets, FakeTurn([]))
    ctx = spaces.context(bot._demo.enroll("ou_a"), None)
    await bot._answer(ctx, "m1", "你好")
    text, meta = FakeCard.instances[-1].finished
    assert text.startswith("前半段") and "回答中断" in text
    assert "本次消耗 1 点" in meta


def test_demo_enrollment_happens_once(sessions, spaces, wallets):
    demo = DemoEnrollment(sessions, spaces, wallets)
    uid = demo.enroll("ou_a")
    assert demo.enroll("ou_a") == uid
    view = wallets.view(spaces.context(uid, None))
    assert (view.subsidy, view.balance) == (DEMO_SUBSIDY, DEMO_BALANCE)
    assert [(m.kind, m.role) for m in spaces.memberships(uid)] == [("class", "student")]
    other = demo.enroll("ou_b")
    assert spaces.memberships(other)[0].space_id == spaces.memberships(uid)[0].space_id


def test_demo_enrollment_resumes_after_a_half_finished_attempt(sessions, spaces, wallets, monkeypatch):
    demo = DemoEnrollment(sessions, spaces, wallets)
    real_grant = wallets.grant

    def fail_on_personal(points, ref, **kwargs):
        if kwargs.get("space_id") is None:
            raise RuntimeError("断了")
        real_grant(points, ref, **kwargs)

    monkeypatch.setattr(wallets, "grant", fail_on_personal)
    try:
        demo.enroll("ou_a")
    except RuntimeError:
        pass
    monkeypatch.setattr(wallets, "grant", real_grant)
    view = wallets.view(spaces.context(demo.enroll("ou_a"), None))
    assert (view.subsidy, view.balance) == (DEMO_SUBSIDY, DEMO_BALANCE)


async def test_first_private_message_asks_for_points_in_main_chat(sessions, spaces, wallets):
    bot = make_bot(sessions, spaces, wallets, None)
    await bot._handle(message("点数"))
    assert bot.replies == [f"学校补贴 {DEMO_SUBSIDY:,} 点 · 个人余额 {DEMO_BALANCE} 点"]


async def test_spend_cap_blocks_model_calls_but_not_balance_queries(sessions, spaces, wallets, monkeypatch):
    monkeypatch.setattr("scity.feishu_bot.CardStream", FakeCard)
    FakeCard.instances.clear()
    bot = make_bot(sessions, spaces, wallets, FakeTurn([]), cap=0.05)
    bot._locks = {bot._demo.enroll("ou_a"): asyncio.Lock()}
    wallets.record_system_usage([ModelCall("m", 60_000, 0)], "warmup", "boot")  # 测试成本下是 0.06 元
    await bot._handle(message("你好"))
    assert bot.replies == [CAP_REACHED]
    assert FakeCard.instances == []
    await bot._handle(message("点数"))
    assert len(bot.replies) == 2 and "学校补贴" in bot.replies[1]


def test_unknown_model_cost_is_estimated_high():
    assert DEFAULT_COSTS["default"]["output_per_1k"] >= 0.03
