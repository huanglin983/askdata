# semantic_corrector.py
from __future__ import annotations

import logging
import re

from .config import DIMENSION_DICT, METRIC_DICT
from .schemas import IntentStruct, VerifyResult

logger = logging.getLogger(__name__)

_BI_QUERY_HINT = re.compile(
    r"查|查询|看下|看看|统计|汇总|对比|报表|指标|金额|成本|GAP|gap|"
    r"人民币|美元|CNY|USD|项目|电站|区域|数据|P\d{3,}",
    re.I,
)
_ALL_METRICS_HINT = re.compile(
    r"所有指标|全部指标|所有的指标|全部的指标|每个指标|各项指标|全部数据|所有数据"
)


class SemanticCorrector:
    """元数据校验器，对标 Supersonic SemanticCorrector。"""

    def __init__(
        self,
        metric_white_list: list[str] | None = None,
        dim_white_list: list[str] | None = None,
    ):
        self.metric_white_list = set(metric_white_list or METRIC_DICT)
        self.dim_white_list = set(dim_white_list or DIMENSION_DICT)

    def verify(self, intent: IntentStruct) -> VerifyResult:
        warning = None
        need_disambiguate = False

        invalid_metrics = [m for m in intent.指标 if m not in self.metric_white_list]
        valid_metrics = [m for m in intent.指标 if m in self.metric_white_list]
        intent.指标 = valid_metrics

        invalid_dims = [d for d in intent.分析维度 if d not in self.dim_white_list]
        intent.分析维度 = [d for d in intent.分析维度 if d in self.dim_white_list]

        if invalid_metrics or invalid_dims:
            logger.warning(
                "SemanticCorrector dropped invalid entities metrics=%s dims=%s raw_query=%s",
                invalid_metrics,
                invalid_dims,
                intent.原始问句,
            )

        q = intent.原始问句 or ""
        has_bi = bool(
            _BI_QUERY_HINT.search(q)
            or intent.筛选条件
            or _ALL_METRICS_HINT.search(q)
        )

        if len(intent.指标) == 0 and intent.意图类型 == "数据查询":
            if has_bi:
                # 真问数但未抽出指标：保留数据查询，由适配层展开「所有指标」或提示补指标
                warning = (
                    "未点名具体指标"
                    if not _ALL_METRICS_HINT.search(q)
                    else "问句含所有指标，交由服务端展开可查询指标"
                )
            else:
                intent.意图类型 = "未识别"
                warning = "未识别到明确的问数意图或平台内有效指标"
        elif len(intent.指标) == 0 and intent.意图类型 == "未识别":
            warning = "未识别到明确的问数意图"
        elif len(intent.指标) > 1 and intent.意图类型 not in ("未识别",):
            # 「所有指标」展开后会有多个指标，不消歧
            if _ALL_METRICS_HINT.search(q):
                warning = None
                need_disambiguate = False
            else:
                warning = f"匹配到多个指标：{intent.指标}，请问您需要查询哪一项？"
                need_disambiguate = True

        if warning:
            logger.warning(
                "SemanticCorrector alert need_disambiguate=%s msg=%s intent=%s",
                need_disambiguate,
                warning,
                intent.model_dump(),
            )
        else:
            logger.info(
                "SemanticCorrector passed intent=%s",
                intent.model_dump(),
            )

        return VerifyResult(
            intent=intent,
            warning_msg=warning,
            need_disambiguate=need_disambiguate,
        )
