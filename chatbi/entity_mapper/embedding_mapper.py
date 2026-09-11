# entity_mapper/embedding_mapper.py
"""向量召回 Mapper 占位：后续接入 embedding + ANN 检索。"""
from __future__ import annotations

import logging
from typing import List

from ..schemas import SchemaMapInfo
from .base_mapper import BaseEntityMapper

logger = logging.getLogger(__name__)


class EmbeddingMapper(BaseEntityMapper):
    """Schema 实体向量召回（本次仅占位，不做真实向量检索）。"""

    def __init__(self, metric_dict: List[str] | None = None):
        self.metric_dict = list(metric_dict or [])

    def match(self, query: str, schema_info: SchemaMapInfo) -> SchemaMapInfo:
        # TODO: embed(query) → 向量库召回 metric/dim 候选 → 合并进 schema_info
        logger.debug(
            "EmbeddingMapper placeholder skip vector recall, query=%s, catalog_size=%d",
            query,
            len(self.metric_dict),
        )
        return schema_info
