"""各入口（飞书机器人、网页）共用的规矩：什么情况下不调用模型，以及怎么告诉用户。"""
from __future__ import annotations

from .spaces import Context, SpaceService
from .wallet import SpendCapReached, WalletService

CAP_REACHED = "平台的演示额度已经用完，暂时不能回答。"
NOT_ENROLLED = "你还没有加入任何课程，开课后就可以使用了。"
OUT_OF_POINTS = "你的点数用完了，充值或等学校发放后可以继续使用。"


def refusal(spaces: SpaceService, wallet: WalletService, ctx: Context, spend_cap_yuan: float | None) -> str | None:
    """能回答就返回 None；不能回答就返回给用户看的原因。"""
    try:
        wallet.check_spend_cap(spend_cap_yuan)
    except SpendCapReached:
        return CAP_REACHED
    if wallet.view(ctx).available <= 0:
        return OUT_OF_POINTS if spaces.memberships(ctx.user_id) else NOT_ENROLLED
    return None
