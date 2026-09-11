# semantic_corrector.py
from __future__ import annotations

import logging

from .config import DIMENSION_DICT, METRIC_DICT
from .schemas import IntentStruct, VerifyResult

logger = logging.getLogger(__name__)


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

        if len(intent.指标) == 0 and intent.意图类型 == "数据查询":
            warning = "未识别到平台内有效指标，请核对指标名称"
        elif len(intent.指标) > 1:
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
