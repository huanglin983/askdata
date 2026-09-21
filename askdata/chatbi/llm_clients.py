# chatbi/llm_clients.py
"""问数接入用的 LLM Client：百炼适配 + 强制走规则兜底。"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class BailianChatClient:
    """将 intent_llm 百炼调用适配为 ChatBI `chat(system, user) -> str`。"""

    async def chat(self, system: str, user: str) -> str:
        def _call() -> str:
            import askdata.intent.llm as intent_llm

            return intent_llm.call_bailian_raw(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )

        return await asyncio.to_thread(_call)


class ForceRuleLLMClient:
    """无 Key / 强制规则路径：抛错触发 ChatBI RuleSemanticParser。"""

    async def chat(self, system: str, user: str) -> str:
        _ = (system, user)
        raise RuntimeError("未配置百炼 Key，使用 ChatBI 规则兜底")
