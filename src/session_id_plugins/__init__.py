"""Session ID plugin registry.

Built-in plugins are registered automatically when this module is imported.
To add a plugin for a custom agent, create a new file in this package and
call ``register()`` at module level, then import that file from here.

Example custom plugin (``src/session_id_plugins/plugin_my_agent.py``)::

    from src.session_id_plugins import register
    from src.session_id_plugins.base import SessionIdPlugin

    class MyAgentPlugin:
        name = "my_agent"
        priority = 50  # lower = checked before built-in plugins

        def extract(self, headers, body):
            return body.get("my_agent_session_id") or None

    register(MyAgentPlugin())

Then add ``from . import plugin_my_agent`` at the bottom of this file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from src.session_id_plugins.base import SessionIdPlugin

_plugins: List[SessionIdPlugin] = []


def register(plugin: SessionIdPlugin) -> None:
    """Register a plugin instance into the global registry."""
    _plugins.append(plugin)


def extract_session_id(
    headers: Mapping[str, Any],
    body: Dict[str, Any],
) -> Optional[str]:
    """Try all registered plugins in priority order and return the first match."""
    for plugin in sorted(_plugins, key=lambda p: p.priority):
        result = plugin.extract(headers, body)
        if result:
            return result
    return None


# ── Register built-in plugins ──────────────────────────────────────────────
from src.session_id_plugins.plugin_headers import HeadersPlugin  # noqa: E402
from src.session_id_plugins.plugin_metadata import MetadataPlugin  # noqa: E402

register(HeadersPlugin())
register(MetadataPlugin())

# ── Custom plugins ─────────────────────────────────────────────────────────
# Add ``from . import plugin_<your_agent>`` lines below to activate custom plugins.
