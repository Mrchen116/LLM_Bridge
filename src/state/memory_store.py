from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from .interfaces import ReasoningKey, ReasoningStore


class InMemoryReasoningStore(ReasoningStore):
    def __init__(self) -> None:
        self._cache: Dict[ReasoningKey, Dict[str, List[Dict[str, Any]]]] = {}

    def get_decorated_prefix(self, key: ReasoningKey, fingerprint: str) -> Optional[List[Dict[str, Any]]]:
        bucket = self._cache.get(key) or {}
        value = bucket.get(fingerprint)
        if value is None:
            return None
        return copy.deepcopy(value)

    def set_decorated_prefix(
        self,
        key: ReasoningKey,
        fingerprint: str,
        decorated_prefix_input: List[Dict[str, Any]],
    ) -> None:
        bucket = self._cache.setdefault(key, {})
        bucket[fingerprint] = copy.deepcopy(decorated_prefix_input)
