# intent_chatbi.py
"""问数自然语言主链路：ChatBI 意图流水线 → engine.Intent。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from typing import Any

from engine import Intent

logger = logging.getLogger(__name__)

# 进程内共享会话记忆（与 ChatBIWorkflow 绑定）
_workflow_singleton: Any = None
_workflow_key: tuple | None = None


def _run_coro(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _dim_names(catalog: dict[str, Any]) -> list[str]:
    names = [d["name"] for d in catalog.get("dims") or [] if d.get("name")]
    for extra in ("项目", "项目编号", "项目名称", "电站", "区域", "阶段", "年份", "月份"):
        if extra not in names:
            names.append(extra)
    return names


def _build_workflow(catalog: dict[str, Any]):
    """按当前 catalog / 百炼配置构建（或复用）ChatBIWorkflow。"""
    global _workflow_singleton, _workflow_key
    import intent_llm
    from chatbi.chat_workflow import ChatBIWorkflow
    from chatbi.llm_clients import BailianChatClient, ForceRuleLLMClient

    metric_names = sorted(catalog["name_to_id"].keys(), key=len, reverse=True)
    dim_names = _dim_names(catalog)
    use_bailian = intent_llm.bailian_configured()
    key = (tuple(metric_names), tuple(dim_names), use_bailian)
    if _workflow_singleton is not None and _workflow_key == key:
        return _workflow_singleton

    # 复用百炼侧带指标字典的 system prompt（去掉末尾示例问句绑定）
    system_prompt = intent_llm.build_messages("", catalog)[0]["content"]
    llm = BailianChatClient() if use_bailian else ForceRuleLLMClient()
    workflow = ChatBIWorkflow(
        llm_client=llm,
        metric_dict=metric_names,
        dimension_dict=dim_names,
        system_prompt=system_prompt,
    )
    _workflow_singleton = workflow
    _workflow_key = key
    return workflow


def from_text_chatbi(text: str, *, session_id: str | None = None) -> Intent:
    """
    ChatBI 主链路：实体 Mapper → LLM/规则 → 校验/消歧 → 转为 engine.Intent。
    血缘展开仍走 intent_llm.normalize_llm_payload（对接现有指标引擎）。
    """
    import intent_llm

    text = (text or "").strip()
    catalog = intent_llm.build_catalog()
    workflow = _build_workflow(catalog)
    sid = session_id or f"ask-{uuid.uuid4().hex[:12]}"

    out = _run_coro(workflow.run(user_query=text, session_id=sid))
    raw = dict(out.get("intent") or {})
    raw["原始问句"] = text

    if out.get("code") == "disambiguate":
        intent = intent_llm.normalize_llm_payload(raw, catalog, raw_text=text)
        intent.source = "chatbi"
        intent.disambiguate_msg = out.get("msg") or "请明确要查询的指标"
        if intent.payload_zh is not None:
            intent.payload_zh["意图来源"] = _source_label(raw)
        logger.info("chatbi disambiguate: %s", intent.disambiguate_msg)
        return intent

    intent = intent_llm.normalize_llm_payload(raw, catalog, raw_text=text)
    intent.source = "chatbi"
    if intent.payload_zh is not None:
        intent.payload_zh["意图来源"] = _source_label(raw)
        if out.get("warning"):
            intent.payload_zh["校验告警"] = out["warning"]
    return intent


def _source_label(raw: dict[str, Any]) -> str:
    src = str(raw.get("意图来源") or "")
    if "规则" in src:
        return "ChatBI规则兜底"
    return "ChatBI意图流水线"


def reset_chatbi_workflow() -> None:
    """测试或元数据变更后重置单例。"""
    global _workflow_singleton, _workflow_key
    _workflow_singleton = None
    _workflow_key = None
