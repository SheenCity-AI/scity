import json

from scity.feishu_bot import ANSWER_ELEMENT, META_ELEMENT, build_card, extract_text, format_charge
from scity.wallet import ChargeResult, WalletView


def test_extract_text_removes_mentions():
    assert extract_text(json.dumps({"text": "@_user_1 你好 @_user_2"})) == "你好"


def test_card_is_streaming_with_both_elements():
    card = json.loads(build_card())
    assert card["config"]["streaming_mode"] is True
    assert [e["element_id"] for e in card["body"]["elements"]] == [ANSWER_ELEMENT, META_ELEMENT]


def test_format_charge():
    text = format_charge(ChargeResult(12, 12, 0, 0, WalletView(2988, 260)))
    assert text == "本次消耗 12 点 · 学校补贴 2,988 点 · 个人余额 260 点"
