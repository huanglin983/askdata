# chat_workflow.py
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .chat_memory import ChatMemory
from .config import DIMENSION_DICT, METRIC_DICT, SYSTEM_PROMPT
from .entity_mapper.dict_mapper import DictMapper
from .entity_mapper.embedding_mapper import EmbeddingMapper
from .schemas import IntentStruct, SchemaMapInfo, VerifyResult
from .semantic_corrector import SemanticCorrector
from .semantic_parser import LLMSemanticParser, RuleSemanticParser

logger = logging.getLogger(__name__)


class ChatBIWorkflow:
    """
    企业级 ChatBI 意图识别流水线（对标 Supersonic）：
    预处理 → Schema 实体 Mapper → LLM/规则语义抽取 → 元数据校验 → 会话记忆 → 路由分发。

    不负责 SQL 生成与数据库查询，交由现有指标引擎执行。
    """

    def __init__(
        self,
        llm_client,
        *,
        metric_dict: list[str] | None = None,
        dimension_dict: list[str] | None = None,
        system_prompt: str | None = None,
        lineage_fn: Callable[[list[str]], list[str]] | None = None,
        memory: ChatMemory | None = None,
    ):
        metrics = list(metric_dict or METRIC_DICT)
        dims = list(dimension_dict or DIMENSION_DICT)
        self.mappers = [
            DictMapper(metric_dict=metrics, dimension_dict=dims),
            EmbeddingMapper(metrics),
        ]
        self.llm_parser = LLMSemanticParser(
            llm_client, system_prompt=system_prompt or SYSTEM_PROMPT
        )
        self.rule_parser = RuleSemanticParser()
        self.corrector = SemanticCorrector(
            metric_white_list=metrics,
            dim_white_list=dims,
        )
        self.memory = memory or ChatMemory()
        self._lineage_fn = lineage_fn

    async def run(self, user_query: str, session_id: str) -> dict[str, Any]:
        logger.info(
            "ChatBI start session_id=%s raw_query=%s",
            session_id,
            user_query,
        )

        # Step1 读取历史，多轮改写
        last_intent = self.memory.get_last_intent(session_id)
        rewritten_query = self._rewrite_query(user_query, last_intent)

        # Step2 实体匹配（词典 + 向量占位）
        schema_info = SchemaMapInfo()
        for mapper in self.mappers:
            schema_info = mapper.match(rewritten_query, schema_info)

        # Step3 LLM 解析，异常降级规则
        parse_source = "llm"
        try:
            intent: IntentStruct = await self.llm_parser.parse(
                rewritten_query, schema_info
            )
        except Exception as exc:
            parse_source = "rule_fallback"
            logger.exception(
                "LLM parse failed, fallback to RuleSemanticParser: %s",
                exc,
            )
            intent = await self.rule_parser.parse(rewritten_query, schema_info)

        logger.info(
            "ChatBI intent extracted source=%s intent=%s",
            parse_source,
            intent.model_dump(),
        )

        # Step4 元数据校验 & 消歧
        verify_result: VerifyResult = self.corrector.verify(intent)
        intent = verify_result.intent

        if verify_result.need_disambiguate:
            logger.warning(
                "ChatBI disambiguate session_id=%s msg=%s",
                session_id,
                verify_result.warning_msg,
            )
            return {
                "code": "disambiguate",
                "msg": verify_result.warning_msg,
                "intent": intent.model_dump(),
            }

        # Step5 路由分发（不下钻 SQL）；未识别不写入会话记忆
        route_result = self._route(intent)
        if route_result.get("action") == "unknown" or intent.意图类型 == "未识别":
            logger.warning(
                "ChatBI unknown intent session_id=%s intent=%s",
                session_id,
                intent.model_dump(),
            )
            if route_result.get("action") != "unknown":
                route_result = self._route(
                    intent.model_copy(update={"意图类型": "未识别"})
                )
            return {
                "code": "unknown",
                "msg": "暂未识别到明确意图",
                "intent": intent.model_dump(),
                "route": route_result,
                "warning": verify_result.warning_msg,
            }

        # Step6 保存会话（仅可执行意图）
        self.memory.save(session_id, intent)

        result = {
            "code": "success",
            "intent": intent.model_dump(),
            "route": route_result,
        }
        if verify_result.warning_msg:
            result["warning"] = verify_result.warning_msg
            logger.warning(
                "ChatBI success with warning session_id=%s warning=%s",
                session_id,
                verify_result.warning_msg,
            )
        else:
            logger.info(
                "ChatBI success session_id=%s route=%s",
                session_id,
                route_result,
            )
        return result

    def _rewrite_query(
        self, query: str, last_intent: Optional[IntentStruct] = None
    ) -> str:
        """多轮指代改写，后续扩展；当前原样返回。"""
        _ = last_intent
        return (query or "").strip()

    def _route(self, intent: IntentStruct) -> dict[str, Any]:
        if intent.意图类型 == "指标口径咨询":
            return {"action": "query_metric_meta", "metrics": intent.指标}
        if intent.意图类型 == "数据查询":
            expand_metrics: list[str] = []
            if intent.是否查询关联指标:
                expand_metrics = self._get_metric_lineage(intent.指标)
            seen: set[str] = set()
            final_metrics: list[str] = []
            for m in intent.指标 + expand_metrics:
                if m not in seen:
                    seen.add(m)
                    final_metrics.append(m)
            return {
                "action": "query_data",
                "metrics": final_metrics,
                "dimensions": intent.分析维度,
                "filter": intent.筛选条件,
                "currency": intent.币种,
                "calc_cmd": intent.计算指令,
            }
        if intent.意图类型 == "指标字典检索":
            return {"action": "list_metric"}
        # 未识别及其它
        return {
            "action": "unknown",
            "capabilities": [
                {
                    "type": "数据查询",
                    "desc": "查指标数值，走规则引擎生成/执行 SQL",
                },
                {
                    "type": "指标口径咨询",
                    "desc": "问定义/公式/口径，返回指标元数据（不查数）",
                },
                {
                    "type": "指标字典检索",
                    "desc": "查有哪些指标/清单，返回字典列表",
                },
            ],
        }

    def _get_metric_lineage(self, metric_list: list[str]) -> list[str]:
        """指标血缘接口：可注入外部服务；默认占位。"""
        if self._lineage_fn is not None:
            try:
                return list(self._lineage_fn(metric_list) or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("lineage_fn failed: %s", exc)
                return []
        logger.debug("_get_metric_lineage placeholder, metrics=%s", metric_list)
        return []
