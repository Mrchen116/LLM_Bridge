#!/usr/bin/env python3
import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


REQ_FILE_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{3})-req\.json$")
ANY_SESSION_JSON_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{3})-[^.]+\.json$"
)


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _iter_texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_texts(item)
        return
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            yield value["text"]
        if "content" in value:
            yield from _iter_texts(value.get("content"))


def _strip_system_reminder(text: str) -> str:
    lowered = text.lower()
    if "<system-reminder>" in lowered and "</system-reminder>" in lowered:
        start = lowered.find("<system-reminder>")
        end = lowered.find("</system-reminder>")
        if start != -1 and end != -1 and end >= start:
            left = text[:start]
            right = text[end + len("</system-reminder>") :]
            return f"{left} {right}".strip()
    return text


def _extract_agent_key(req_body: Dict[str, Any], prefix_len: int) -> str:
    messages = req_body.get("messages")
    if not isinstance(messages, list):
        return "unknown-agent"

    candidates: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        for raw in _iter_texts(message.get("content")):
            txt = _normalize_text(_strip_system_reminder(raw))
            if not txt:
                continue
            if txt.lower().startswith("<system-reminder>"):
                continue
            candidates.append(txt)

    if not candidates:
        return "unknown-agent"

    # 优先使用首段可读文本作为分组依据，避免把整段历史都纳入键值。
    return candidates[0][:prefix_len]


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data


def _collect_req_files(session_dir: Path) -> List[Tuple[str, Path]]:
    items: List[Tuple[str, Path]] = []
    for path in sorted(session_dir.glob("*-req.json")):
        m = REQ_FILE_RE.match(path.name)
        if not m:
            continue
        items.append((m.group("ts"), path))
    return items


def _collect_all_session_files(session_dir: Path) -> Dict[str, List[Path]]:
    ts_to_paths: Dict[str, List[Path]] = {}
    for path in sorted(session_dir.glob("*.json")):
        m = ANY_SESSION_JSON_RE.match(path.name)
        if not m:
            continue
        ts = m.group("ts")
        ts_to_paths.setdefault(ts, []).append(path)
    return ts_to_paths


def split_session(
    session_dir: Path,
    output_root: Path,
    prefix_len: int,
    dry_run: bool,
    overwrite: bool,
) -> None:
    if not session_dir.exists() or not session_dir.is_dir():
        raise ValueError(f"session目录不存在或不是目录: {session_dir}")

    req_files = _collect_req_files(session_dir)
    if not req_files:
        raise ValueError(f"未找到req日志文件: {session_dir}")

    ts_to_all_files = _collect_all_session_files(session_dir)

    # key -> {"first_ts": str, "ts_list": List[str]}
    groups: Dict[str, Dict[str, Any]] = {}
    for ts, req_path in req_files:
        req_body = _read_json(req_path)
        key = _extract_agent_key(req_body, prefix_len)
        if key not in groups:
            groups[key] = {"first_ts": ts, "ts_list": [ts]}
        else:
            groups[key]["ts_list"].append(ts)
            if ts < groups[key]["first_ts"]:
                groups[key]["first_ts"] = ts

    ordered = sorted(groups.items(), key=lambda kv: kv[1]["first_ts"])
    key_to_agent_idx = {key: idx + 1 for idx, (key, _) in enumerate(ordered)}

    session_name = session_dir.name
    out_session_dir = output_root / session_name
    if out_session_dir.exists():
        if not overwrite:
            raise ValueError(f"输出目录已存在，请加 --overwrite: {out_session_dir}")
        if not dry_run:
            shutil.rmtree(out_session_dir)

    copied_count = 0
    for key, info in ordered:
        agent_idx = key_to_agent_idx[key]
        agent_dir = out_session_dir / str(agent_idx)
        if not dry_run:
            agent_dir.mkdir(parents=True, exist_ok=True)

        for ts in sorted(info["ts_list"]):
            for src in ts_to_all_files.get(ts, []):
                dst = agent_dir / src.name
                if dry_run:
                    print(f"[DRY-RUN] {src} -> {dst}")
                else:
                    shutil.copy2(src, dst)
                copied_count += 1

    print(f"session: {session_dir}")
    print(f"输出: {out_session_dir}")
    print(f"agent数量: {len(ordered)}")
    for key, info in ordered:
        idx = key_to_agent_idx[key]
        preview = key if len(key) <= 80 else f"{key[:77]}..."
        print(f"  agent {idx}: 请求数={len(info['ts_list'])}, 首次时间={info['first_ts']}")
        print(f"    key前缀: {preview}")
    print(f"拷贝文件数: {copied_count}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按messages.content前缀将session日志拆分到multiagent目录"
    )
    parser.add_argument("session_dir", help="session目录路径，如 logs/session/xxx")
    parser.add_argument(
        "--output-root",
        default="logs/multiagent",
        help="输出根目录（默认: logs/multiagent）",
    )
    parser.add_argument(
        "--prefix-len",
        type=int,
        default=180,
        help="用于agent分组的文本前缀长度（默认: 180）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印分组和拷贝计划，不实际写文件",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="若目标session目录已存在则覆盖",
    )
    args = parser.parse_args()

    session_dir = Path(args.session_dir).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    split_session(
        session_dir=session_dir,
        output_root=output_root,
        prefix_len=args.prefix_len,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()
