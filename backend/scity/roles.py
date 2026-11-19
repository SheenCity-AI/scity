"""课里的 AI 角色（居民代表、银行经理这类）：一问一答，不带工具，不留会话记忆。

设定和已经谈过的内容每次都由课题引擎给出（见 game.Seat），这里只负责问模型、数用量。
和常驻助理分开：助理跟人走、有自己的记忆；角色属于一局课里的一个组，记录存在这一局的事件里。
"""
from __future__ import annotations

import httpx

from .settings import Settings
from .wallet import ModelCall

MAX_ANSWER_TOKENS = 400


class RolePlayer:
    def __init__(self, settings: Settings, timeout: float = 40.0):
        self._model = settings.default_model
        self._url = settings.ark_base_url.rstrip("/") + "/chat/completions"
        self._key = settings.ark_api_key
        self._timeout = timeout

    async def reply(self, prompt: str, history: tuple[tuple[str, str], ...], text: str) -> tuple[str, list[ModelCall]]:
        messages = [{"role": "system", "content": prompt}]
        for question, answer in history:
            messages += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        messages.append({"role": "user", "content": text})
        body = {"model": self._model, "messages": messages, "max_tokens": MAX_ANSWER_TOKENS,
                "thinking": {"type": "disabled"}}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(self._url, json=body, headers={"Authorization": f"Bearer {self._key}"})
        response.raise_for_status()
        data = response.json()
        answer = (data["choices"][0]["message"].get("content") or "").strip()
        usage = data.get("usage") or {}
        calls = [ModelCall(self._model, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0)]
        if not answer:
            raise ValueError("模型没有给出回答")
        return answer, calls
