import asyncio
import json
import time

from scity.feishu_bot import ANSWER_ELEMENT, META_ELEMENT, Bot, CardStream, FeishuError, build_card, extract_text, format_charge
from scity.models import make_session_factory
from scity.settings import DEMO_PRICING
from scity.wallet import ChargeResult, ModelCall, WalletService, WalletView


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


def make_bot(tmp_path, turn):
    bot = Bot.__new__(Bot)
    bot._client = None
    bot._wallet = WalletService(make_session_factory(f"sqlite:///{tmp_path}/t.db"), DEMO_PRICING)

    class FakeAssistant:
        async def start_turn(self, user_id, text):
            return turn

    bot._assistant = FakeAssistant()
    return bot


async def test_interrupted_answer_closes_card_and_charges_actual_usage(tmp_path, monkeypatch):
    monkeypatch.setattr("scity.feishu_bot.CardStream", FakeCard)
    bot = make_bot(tmp_path, FakeTurn([]))
    uid = bot._wallet.ensure_user("ou_a")
    await bot._answer(uid, "m1", "你好")
    text, meta = FakeCard.instances[-1].finished
    assert text.startswith("前半段") and "回答中断" in text
    assert "本次消耗 1 点" in meta
