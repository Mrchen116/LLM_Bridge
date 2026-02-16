from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeContext:
    ban_explore: bool
    ban_stream: bool
    expose_thinking: bool
    upstream_config: dict
    logs_openai_dir: str
    logs_anthropic_dir: str
    logs_session_dir: str
    logs_codeagent_dir: str
    converters: Any
    proxy_logging: Any
    reasoning: Any
    executor: Any
