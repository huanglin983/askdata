# entity_mapper/base_mapper.py
from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import SchemaMapInfo


class BaseEntityMapper(ABC):
    @abstractmethod
    def match(self, query: str, schema_info: SchemaMapInfo) -> SchemaMapInfo:
        """从问句中抽取指标/维度/筛选/币种候选，写入 SchemaMapInfo。"""
