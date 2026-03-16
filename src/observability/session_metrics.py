from __future__ import annotations

import glob
import json
import os
from typing import Any, Callable, Dict, List, Optional


def _scan_session_dirs(
    session_dirs: List[str],
    collect_usage_tokens: Callable[[Any, str], tuple[int, int, str]],
) -> Dict[str, Any]:
    total_input = 0
    total_output = 0
    num_turns = 0
    by_format: Dict[str, Dict[str, int]] = {}
    for d in session_dirs:
        files = sorted(glob.glob(os.path.join(d, "*-non-stream-res-*.json")))
        if not files:
            files = sorted(glob.glob(os.path.join(d, "*res.json")))
        for fp in files:
            num_turns += 1
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            in_tok, out_tok, fmt = collect_usage_tokens(data, fp)
            total_input += in_tok
            total_output += out_tok
            slot = by_format.setdefault(fmt, {"input_tokens": 0, "output_tokens": 0, "num_turns": 0})
            slot["input_tokens"] += in_tok
            slot["output_tokens"] += out_tok
            slot["num_turns"] += 1
    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "num_turns": num_turns,
        "by_format": by_format,
    }


def get_session_stats(
    *,
    session_id: str,
    logs_session_dir: str,
    collect_usage_tokens: Callable[[Any, str], tuple[int, int, str]],
) -> Optional[Dict[str, Any]]:
    session_dirs = sorted(glob.glob(os.path.join(logs_session_dir, f"*_{session_id}")))
    stats = _scan_session_dirs(session_dirs, collect_usage_tokens) if session_dirs else None
    if not stats or stats.get("num_turns", 0) == 0:
        return None
    return stats
