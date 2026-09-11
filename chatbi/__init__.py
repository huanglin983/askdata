# chatbi/__init__.py
"""ChatBI 意图识别流水线（不含 SQL 生成）。"""

from .chat_workflow import ChatBIWorkflow
from .schemas import IntentStruct, SchemaMapInfo, VerifyResult

__all__ = [
    "ChatBIWorkflow",
    "IntentStruct",
    "SchemaMapInfo",
    "VerifyResult",
]
