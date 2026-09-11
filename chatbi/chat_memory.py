# chat_memory.py
from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from .schemas import IntentStruct

logger = logging.getLogger(__name__)


class ChatMemory:
    """会话记忆：本地内存存储，生产可替换为 Redis。"""

    def __init__(self):
        self.store: Dict[str, str] = {}

    def save(self, session_id: str, intent: IntentStruct) -> None:
        data = intent.model_dump()
        self.store[session_id] = json.dumps(data, ensure_ascii=False)
        logger.info("ChatMemory save session_id=%s intent=%s", session_id, data)

    def get_last_intent(self, session_id: str) -> Optional[IntentStruct]:
        raw = self.store.get(session_id)
        if not raw:
            return None
        intent = IntentStruct(**json.loads(raw))
        logger.debug("ChatMemory hit session_id=%s", session_id)
        return intent

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self.store.clear()
        else:
            self.store.pop(session_id, None)
