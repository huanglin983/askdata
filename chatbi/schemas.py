# schemas.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IntentStruct(BaseModel):
    """标准化意图结构体，JSON 字段与历史 ask 接口 payload_zh 兼容。"""

    意图来源: str
    意图类型: str = Field(pattern="^(数据查询|指标口径咨询|指标字典检索)$")
    指标: List[str] = Field(default_factory=list)
    币种: str = ""
    分析维度: List[str] = Field(default_factory=list)
    筛选条件: Dict[str, Any] = Field(default_factory=dict)
    计算指令: Optional[str] = None
    是否查询关联指标: bool = False
    原始问句: str = ""


class SchemaMapInfo(BaseModel):
    """实体 Mapper 中间结果，对标 Supersonic SchemaMapInfo。"""

    metric_candidates: List[str] = Field(default_factory=list)
    dim_candidates: List[str] = Field(default_factory=list)
    filter_candidates: Dict[str, Any] = Field(default_factory=dict)
    currency_candidate: str = ""


class VerifyResult(BaseModel):
    """校验返回结果。"""

    intent: IntentStruct
    warning_msg: Optional[str] = None
    need_disambiguate: bool = False
