from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


ReasoningKey = Tuple[str, str, str]


class ReasoningStore(ABC):
    @abstractmethod
    def get_decorated_prefix(self, key: ReasoningKey, fingerprint: str) -> Optional[List[Dict[str, Any]]]:
        raise NotImplementedError

    @abstractmethod
    def set_decorated_prefix(
        self,
        key: ReasoningKey,
        fingerprint: str,
        decorated_prefix_input: List[Dict[str, Any]],
    ) -> None:
        raise NotImplementedError
