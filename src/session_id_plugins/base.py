from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class SessionIdPlugin(Protocol):
    """Protocol that every session-ID extraction plugin must satisfy.

    Plugins are tried in ascending ``priority`` order; the first non-empty
    result wins.  Built-in plugins use priority 100 / 200 so custom plugins
    can take precedence by choosing a lower value (e.g. 50).
    """

    name: str
    priority: int

    def extract(
        self,
        headers: Mapping[str, Any],
        body: Dict[str, Any],
    ) -> Optional[str]:
        """Return a non-empty session ID string, or None to pass to the next plugin."""
        ...
