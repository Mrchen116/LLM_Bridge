from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _preset_paths() -> tuple[Path, Path]:
    preset_dir = Path.cwd() / "logs" / "session_inspector"
    preset_file = preset_dir / "keyword_presets.json"
    return preset_dir, preset_file


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sanitize_keyword_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        token = str(item or "").strip()
        if not token:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        out.append(token)
    return out


def _sanitize_preset(item: Any, fallback_idx: int) -> Dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    preset_id = str(item.get("id") or f"preset-{fallback_idx}").strip()
    if not preset_id:
        preset_id = f"preset-{fallback_idx}"
    name = str(item.get("name") or "").strip() or preset_id
    include_keywords = _sanitize_keyword_list(item.get("include_keywords"))
    exclude_keywords = _sanitize_keyword_list(item.get("exclude_keywords"))
    updated_at = str(item.get("updated_at") or "").strip() or _utc_now_iso()
    return {
        "id": preset_id,
        "name": name,
        "include_keywords": include_keywords,
        "exclude_keywords": exclude_keywords,
        "updated_at": updated_at,
    }


def _default_payload() -> Dict[str, Any]:
    return {"version": 1, "default_preset_id": None, "presets": []}


def normalize_presets_payload(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _default_payload()

    presets_in = raw.get("presets")
    presets_out: List[Dict[str, Any]] = []
    seen_ids = set()
    if isinstance(presets_in, list):
        for idx, item in enumerate(presets_in, start=1):
            preset = _sanitize_preset(item, idx)
            if preset["id"] in seen_ids:
                continue
            seen_ids.add(preset["id"])
            presets_out.append(preset)

    default_preset_id = raw.get("default_preset_id")
    default_value = str(default_preset_id).strip() if default_preset_id is not None else None
    if default_value and default_value not in seen_ids:
        default_value = None

    return {"version": 1, "default_preset_id": default_value, "presets": presets_out}


def load_keyword_presets() -> Dict[str, Any]:
    _preset_dir, preset_file = _preset_paths()
    if not preset_file.exists():
        return _default_payload()
    try:
        raw = json.loads(preset_file.read_text(encoding="utf-8"))
    except Exception:
        return _default_payload()
    return normalize_presets_payload(raw)


def save_keyword_presets(raw: Any) -> Dict[str, Any]:
    payload = normalize_presets_payload(raw)
    preset_dir, preset_file = _preset_paths()
    preset_dir.mkdir(parents=True, exist_ok=True)
    tmp = preset_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(preset_file)
    return payload
