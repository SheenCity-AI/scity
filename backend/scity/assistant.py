"""常驻助理：基于 VeADK，流式生成回答，并收集每次模型调用的 token 用量。

同一个底座上有多套岗位设定（见 personas.py），每轮对话按场合和角色选用一套。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.tools import ToolContext
from google.genai import types
from veadk import Agent

from .personas import PERSONAS, Persona, persona_for, situation
from .settings import Settings
from .spaces import Context, SpaceService
from .wallet import ModelCall, WalletService

APP_NAME = "scity"
WARM_UP = Context(user_id=0)  # 不对应任何真实账号


class Turn:
    """一轮对话：逐段产出文字，结束后 calls 里是本轮所有模型调用的用量。"""

    def __init__(self, events: AsyncIterator, model: str, persona: Persona):
        self._events = events
        self._model = model
        self.persona = persona
        self.calls: list[ModelCall] = []

    async def stream(self) -> AsyncIterator[str]:
        async for event in self._events:
            if event.partial:
                for part in (event.content.parts if event.content else None) or []:
                    if part.text and not part.thought:
                        yield part.text
            elif event.usage_metadata:
                usage = event.usage_metadata
                self.calls.append(ModelCall(self._model, usage.prompt_token_count or 0,
                                            usage.candidates_token_count or 0))


class Assistant:
    def __init__(self, settings: Settings, spaces: SpaceService, wallet: WalletService):
        self._model = settings.default_model
        self._sessions = DatabaseSessionService(settings.session_database_url)

        def get_my_points(tool_context: ToolContext) -> dict:
            """查询当前用户在这里能用的点数：空间发的额度（如学校补贴）和个人余额。"""
            ctx = spaces.context(int(tool_context.user_id), tool_context.state.get("workspace_id"))
            view = wallet.view(ctx)
            return {"空间额度": view.subsidy, "个人余额": view.balance, "合计可用": view.available}

        tools = {"get_my_points": get_my_points}

        def build(persona: Persona) -> Runner:
            prompt = settings.prompt(persona.prompt, persona.default_prompt)

            def instruction(context) -> str:
                """每轮现算：提示词 + 当前场合。身份和位置取自后台，不取自对话。"""
                if int(context.user_id) == WARM_UP.user_id:
                    return prompt
                ctx = spaces.context(int(context.user_id), context.state.get("workspace_id"))
                return f"{prompt}\n\n{situation(spaces, ctx)}"

            agent = Agent(
                name=f"scity_{persona.key}",
                description=persona.title,
                instruction=instruction,
                model_name=self._model,
                model_provider="openai",
                model_api_base=settings.ark_base_url,
                model_api_key=settings.ark_api_key,
                model_extra_config={"extra_body": {"thinking": {"type": "disabled"}}},
                tools=[tools[name] for name in persona.tools],
            )
            return Runner(agent=agent, app_name=APP_NAME, session_service=self._sessions)

        self._runners = {key: build(persona) for key, persona in PERSONAS.items()}

    @staticmethod
    def _session_id(ctx: Context) -> str:
        """主聊天和每个工作区各有一份对话记忆，互不交叉。"""
        main = f"user-{ctx.user_id}"
        return main if ctx.workspace_id is None else f"{main}-ws-{ctx.workspace_id}"

    async def _ensure_session(self, ctx: Context) -> None:
        kwargs = {"app_name": APP_NAME, "user_id": str(ctx.user_id), "session_id": self._session_id(ctx)}
        if await self._sessions.get_session(**kwargs) is None:
            await self._sessions.create_session(state={"workspace_id": ctx.workspace_id}, **kwargs)

    async def start_turn(self, ctx: Context, text: str) -> Turn:
        await self._ensure_session(ctx)
        persona = persona_for(ctx)
        events = self._runners[persona.key].run_async(
            user_id=str(ctx.user_id),
            session_id=self._session_id(ctx),
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
        return Turn(events, self._model, persona)

    async def history(self, ctx: Context) -> list[dict]:
        """这个场合里已有的对话，给网页重新打开时显示。只有文字，不含工具调用。"""
        session = await self._sessions.get_session(app_name=APP_NAME, user_id=str(ctx.user_id),
                                                   session_id=self._session_id(ctx))
        messages = []
        for event in (session.events if session else []):
            parts = (event.content.parts if event.content else None) or []
            text = "".join(p.text for p in parts if p.text and not p.thought)
            if text and not event.partial:
                messages.append({"role": "user" if event.author == "user" else "assistant", "text": text})
        return messages

    async def warm_up(self) -> list[ModelCall]:
        """启动时先跑一轮，让框架加载完，学生的第一句话不用多等约 8 秒。用完即删会话。"""
        turn = await self.start_turn(WARM_UP, "你好")
        async for _ in turn.stream():
            pass
        await self.reset(WARM_UP)
        return turn.calls

    async def reset(self, ctx: Context) -> None:
        await self._sessions.delete_session(app_name=APP_NAME, user_id=str(ctx.user_id),
                                            session_id=self._session_id(ctx))
