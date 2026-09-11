# semantic_parser.py
from __future__ import annotations

import json
import logging
import re

from .config import SYSTEM_PROMPT
from .schemas import IntentStruct, SchemaMapInfo

logger = logging.getLogger(__name__)

_RELATED_HINT = re.compile(
    r"相关指标|关联指标|对应的原子|派生指标|依赖指标|配套指标|相关的原子|以及其他相关"
)
_CALIBER_HINT = re.compile(r"口径|定义|计算公式|含义是什么|怎么算|是什么")
_DICT_HINT = re.compile(r"有哪些指标|指标清单|指标字典|指标列表")
_CALC_HINT = re.compile(r"\btop\b|TopN|前\s*\d+|排序|从大到小|合计|汇总|对比", re.I)


class BaseSemanticParser:
    async def parse(self, query: str, schema_info: SchemaMapInfo) -> IntentStruct:
        raise NotImplementedError


class LLMSemanticParser(BaseSemanticParser):
    """LLM 结构化意图抽取，主解析器。"""

    def __init__(self, llm_client, system_prompt: str | None = None):
        self.llm_client = llm_client
        self.system_prompt = system_prompt or SYSTEM_PROMPT

    async def parse(self, query: str, schema_info: SchemaMapInfo) -> IntentStruct:
        user_payload = (
            f"用户问句：{query}\n"
            f"SchemaMap候选："
            f"{json.dumps(schema_info.model_dump(), ensure_ascii=False)}"
        )
        resp = await self.llm_client.chat(
            system=self.system_prompt,
            user=user_payload,
        )
        json_str = self._clean_resp(resp)
        intent_dict = json.loads(json_str)
        intent_dict.setdefault("原始问句", query)
        intent = IntentStruct(**intent_dict)
        # SchemaMap 补全：LLM 漏抽时用词典候选兜底填充空字段
        if not intent.指标 and schema_info.metric_candidates:
            intent.指标 = list(schema_info.metric_candidates)
        if not intent.分析维度 and schema_info.dim_candidates:
            intent.分析维度 = list(schema_info.dim_candidates)
        if not intent.币种 and schema_info.currency_candidate:
            intent.币种 = schema_info.currency_candidate
        if not intent.筛选条件 and schema_info.filter_candidates:
            intent.筛选条件 = dict(schema_info.filter_candidates)
        logger.info(
            "LLMSemanticParser output intent=%s",
            intent.model_dump(),
        )
        return intent

    def _clean_resp(self, text: str) -> str:
        text = (text or "").strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("LLM response has no JSON object")
        return text[start:end]


class RuleSemanticParser(BaseSemanticParser):
    """规则兜底降级解析：LLM 失败时使用 SchemaMap + 关键词。"""

    async def parse(self, query: str, schema_info: SchemaMapInfo) -> IntentStruct:
        if _DICT_HINT.search(query):
            intent_type = "指标字典检索"
        elif _CALIBER_HINT.search(query):
            intent_type = "指标口径咨询"
        else:
            intent_type = "数据查询"

        calc_cmd = None
        if _CALC_HINT.search(query):
            calc_cmd = "TopN" if re.search(r"\btop\b|前\s*\d+", query, re.I) else "排序/汇总"

        intent = IntentStruct(
            意图来源="规则兜底",
            意图类型=intent_type,
            指标=list(schema_info.metric_candidates),
            币种=schema_info.currency_candidate or "",
            分析维度=list(schema_info.dim_candidates),
            筛选条件=dict(schema_info.filter_candidates),
            计算指令=calc_cmd,
            是否查询关联指标=bool(_RELATED_HINT.search(query)),
            原始问句=query,
        )
        logger.warning(
            "RuleSemanticParser fallback used, query=%s intent=%s",
            query,
            intent.model_dump(),
        )
        return intent
