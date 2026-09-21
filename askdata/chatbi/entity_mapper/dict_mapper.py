# entity_mapper/dict_mapper.py
from __future__ import annotations

import logging
import re

from ..config import DIMENSION_DICT, METRIC_DICT
from ..schemas import SchemaMapInfo
from .base_mapper import BaseEntityMapper

logger = logging.getLogger(__name__)


class DictMapper(BaseEntityMapper):
    """词典 Mapper：指标/维度子串匹配 + 币种/阶段规则。"""

    def __init__(
        self,
        metric_dict: list[str] | None = None,
        dimension_dict: list[str] | None = None,
    ):
        self.metric_dict = list(metric_dict or METRIC_DICT)
        self.dimension_dict = list(dimension_dict or DIMENSION_DICT)
        # 美元须先于「元」，避免「美元」被人民币规则误命中
        self.currency_rule = [
            (re.compile(r"美元|USD|美金", re.I), "美元(USD)"),
            (re.compile(r"原币"), "原币"),
            (re.compile(r"人民币|CNY|RMB|(?<![美日欧])元"), "人民币(CNY)"),
        ]
        self.stage_rule = re.compile(r"PJ|Contract|YTD|FinalCST")
        self.project_rule = re.compile(r"(?:项目\s*)?(P\d{3,})", re.I)

    def match(self, query: str, schema_info: SchemaMapInfo) -> SchemaMapInfo:
        # 指标：长名优先；命中后从剩余文本抹掉，避免「成本GAP」误伤「成本GAP率」
        remaining = query
        for name in sorted(self.metric_dict, key=len, reverse=True):
            if name and name in remaining and name not in schema_info.metric_candidates:
                schema_info.metric_candidates.append(name)
                remaining = remaining.replace(name, " " * len(name), 1)

        dim_remaining = query
        for name in sorted(self.dimension_dict, key=len, reverse=True):
            if (
                name
                and name in dim_remaining
                and name not in schema_info.dim_candidates
            ):
                schema_info.dim_candidates.append(name)
                dim_remaining = dim_remaining.replace(name, " " * len(name), 1)

        for pat, val in self.currency_rule:
            if pat.search(query):
                schema_info.currency_candidate = val
                break

        # 阶段词若仅作为已命中指标名的一部分出现，则不写入筛选
        stage_matches = []
        for s in self.stage_rule.findall(query):
            if any(s in m for m in schema_info.metric_candidates):
                continue
            if s not in stage_matches:
                stage_matches.append(s)
        if stage_matches:
            schema_info.filter_candidates["阶段"] = stage_matches

        project = self.project_rule.search(query)
        if project:
            schema_info.filter_candidates["项目编号"] = project.group(1).upper()

        logger.info(
            "DictMapper match query=%s metrics=%s dims=%s currency=%s filters=%s",
            query,
            schema_info.metric_candidates,
            schema_info.dim_candidates,
            schema_info.currency_candidate,
            schema_info.filter_candidates,
        )
        return schema_info
