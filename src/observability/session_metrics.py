from __future__ import annotations

import glob
import json
import os
from typing import Any, Callable, Dict, List, Optional


def _scan_session_dirs(
    session_dirs: List[str],
    collect_usage_tokens: Callable[[Any], tuple[int, int]],
) -> Dict[str, int]:
    total_input = 0
    total_output = 0
    num_turns = 0
    for d in session_dirs:
        for fp in glob.glob(os.path.join(d, "*res.json")):
            num_turns += 1
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            in_tok, out_tok = collect_usage_tokens(data)
            total_input += in_tok
            total_output += out_tok
    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "num_turns": num_turns,
    }


def get_session_stats(
    *,
    session_id: str,
    logs_session_dir: str,
    logs_codeagent_dir: str,
    collect_usage_tokens: Callable[[Any], tuple[int, int]],
) -> Optional[Dict[str, int]]:
    session_dirs = sorted(glob.glob(os.path.join(logs_session_dir, f"*_{session_id}")))
    stats = _scan_session_dirs(session_dirs, collect_usage_tokens) if session_dirs else None
    if not stats or stats.get("num_turns", 0) == 0:
        session_dirs = sorted(glob.glob(os.path.join(logs_codeagent_dir, f"*_{session_id}")))
        stats = _scan_session_dirs(session_dirs, collect_usage_tokens) if session_dirs else None
    if not stats or stats.get("num_turns", 0) == 0:
        return None
    return stats
