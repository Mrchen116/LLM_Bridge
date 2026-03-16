import argparse
import asyncio
import time
from typing import Any, Dict

import httpx

from proxy_converters import _build_codex_responses_payload_from_chat
from token_auth import (
    add_codex_account_via_browser_oauth,
    add_codex_account_via_device_oauth,
    get_codex_headers_for_label,
    list_codex_accounts,
    login_all_codex_accounts,
    remove_codex_account,
    set_codex_account_enabled,
    set_codex_account_priority,
)

CODEX_ENDPOINT_DEFAULT = "https://chatgpt.com/backend-api/codex/responses"
CODEX_MODEL_DEFAULT = "gpt-5.4"


def _prompt_select(title: str, options: list[str], default_index: int = 0) -> int:
    while True:
        print(f"\n{title}")
        for idx, text in enumerate(options, start=1):
            mark = " (default)" if idx - 1 == default_index else ""
            print(f"  {idx}) {text}{mark}")
        raw = input("请选择编号: ").strip()
        if not raw:
            return default_index
        try:
            idx = int(raw)
        except Exception:
            print("[accounts] 请输入数字编号。")
            continue
        if 1 <= idx <= len(options):
            return idx - 1
        print(f"[accounts] 请输入 1 到 {len(options)} 之间的编号。")


def _prompt_field(title: str, default: str = "", *, required: bool = False) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{title}{suffix}: ")
        val = raw.strip()
        if val:
            return val
        if default:
            return default
        if not required:
            return ""
        print(f"[accounts] {title} 不能为空，请重新输入。")


def _prompt_int(title: str, default: int) -> int:
    while True:
        raw = _prompt_field(title, default=str(default))
        try:
            return int(raw)
        except Exception:
            print("[accounts] 请输入整数。")


def _prompt_confirm(title: str, *, default: bool = False) -> bool:
    suffix = " [y/N]" if not default else " [Y/n]"
    while True:
        raw = input(f"{title}{suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "是"}:
            return True
        if raw in {"n", "no", "0", "否"}:
            return False
        print("[accounts] 请输入 y 或 n。")


def _format_duration(seconds: int) -> str:
    remain = max(0, int(seconds))
    hours, remain = divmod(remain, 3600)
    minutes, secs = divmod(remain, 60)
    if hours > 0:
        return f"{hours}h{minutes}m"
    if minutes > 0:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def _format_ts(ts: int) -> str:
    if int(ts or 0) <= 0:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


def _account_summary(item: dict) -> str:
    now = int(time.time())
    label = str(item.get("label") or "")
    priority = int(item.get("priority") or 100)
    enabled = bool(item.get("enabled"))
    cooldown_until = int(item.get("cooldown_until") or 0)
    expires_at = int(item.get("expires_at") or 0)
    last_error = str(item.get("last_error") or "").strip()

    if not enabled:
        state = "已禁用"
    elif cooldown_until > now:
        state = f"冷却中，还需 {_format_duration(cooldown_until - now)}"
    else:
        state = "可用"

    summary = f"{label} | 优先级 {priority} | 状态 {state} | 过期 { _format_ts(expires_at) }"
    if last_error:
        summary += f" | 最近错误 {last_error[:80]}"
    return summary


async def _choose_account(
    title: str,
    *,
    enabled: bool | None = None,
    empty_message: str,
) -> dict | None:
    status = await _print_accounts_list()
    accounts = status.get("accounts") or []
    filtered = []
    for item in accounts:
        if enabled is True and not bool(item.get("enabled")):
            continue
        if enabled is False and bool(item.get("enabled")):
            continue
        filtered.append(item)

    if not filtered:
        print(f"[accounts] {empty_message}")
        return None

    options = [_account_summary(item) for item in filtered] + ["返回主菜单"]
    idx = _prompt_select(title, options, default_index=0)
    if idx == len(options) - 1:
        return None
    return filtered[idx]


async def _print_accounts_list() -> None:
    status = await list_codex_accounts()
    accounts = status.get("accounts") or []
    print("\n[accounts] 账号列表（优先级越小越先被使用）")
    if not accounts:
        print("  暂无账号。")
        return status

    enabled_count = 0
    for idx, item in enumerate(accounts, start=1):
        if bool(item.get("enabled")):
            enabled_count += 1
        print(f"  {idx}) {_account_summary(item)}")
    print(f"[accounts] 共 {len(accounts)} 个账号，启用中 {enabled_count} 个。")
    return status


def _resolve_codex_profile_for_test() -> tuple[str, str, Dict[str, Any] | None]:
    """从 upstream 配置解析 Codex 端点、模型与 profile，失败时使用默认值。"""
    try:
        from upstream_config import load_and_validate_config

        root = load_and_validate_config()
        profiles = root.get("profiles") or {}
        default_name = str(root.get("defaultProfile") or "")
        profile = None
        if default_name and profiles.get(default_name):
            p = profiles[default_name]
            if str(p.get("provider") or "") == "codex_oauth":
                profile = p
        if not profile:
            for p in profiles.values():
                if isinstance(p, dict) and str(p.get("provider") or "") == "codex_oauth":
                    profile = p
                    break
        if profile:
            auth = profile.get("auth") or {}
            endpoint = str(auth.get("codexEndpoint") or CODEX_ENDPOINT_DEFAULT).rstrip("/")
            defaults = profile.get("defaults") or {}
            model = str(defaults.get("model") or CODEX_MODEL_DEFAULT)
            return endpoint, model, profile
    except Exception:
        pass
    return CODEX_ENDPOINT_DEFAULT, CODEX_MODEL_DEFAULT, None


async def batch_test_codex_accounts() -> Dict[str, Any]:
    """向所有已启用账号发送 hi 请求，返回各账号成功/失败结果。每测完一个立即打印。"""
    endpoint, model, profile = _resolve_codex_profile_for_test()
    # Codex API 要求 stream=True
    chat_body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    payload = _build_codex_responses_payload_from_chat(chat_body, model)

    status = await list_codex_accounts()
    accounts = status.get("accounts") or []
    ok = 0
    failed = 0
    details: list[Dict[str, Any]] = []
    first = True

    print(f"\n[accounts] 开始批量测试（endpoint={endpoint}）", flush=True)
    for item in accounts:
        label = str(item.get("label") or "")
        enabled = bool(item.get("enabled"))
        if not label or not enabled:
            continue
        # 每个账号测试间隔 2 秒，避免短时间大量建连触发服务器连接级限流
        if not first:
            await asyncio.sleep(2)
        first = False

        headers = await get_codex_headers_for_label(label, profile=profile)
        last_error: BaseException | None = None
        for attempt in range(3):  # ConnectError 最多重试 3 次
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code < 400:
                    ok += 1
                    details.append({"label": label, "ok": True})
                    print(f"- {label}: ok", flush=True)
                else:
                    failed += 1
                    err_text = (resp.text or "")[:200]
                    details.append({"label": label, "ok": False, "error": f"{resp.status_code} {err_text}"})
                    print(f"- {label}: failed error={resp.status_code} {err_text}", flush=True)
                break
            except httpx.ConnectError as e:
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))  # 第 1、2 次重试前等待 2s、4s
                    continue
                failed += 1
                err_msg = str(e) or type(e).__name__
                details.append({"label": label, "ok": False, "error": err_msg})
                print(f"- {label}: failed error={err_msg}", flush=True)
                break
            except Exception as e:
                failed += 1
                err_msg = str(e) or type(e).__name__
                details.append({"label": label, "ok": False, "error": err_msg})
                print(f"- {label}: failed error={err_msg}", flush=True)
                break

    print(f"[accounts] 批量测试完成：成功 {ok}，失败 {failed}", flush=True)
    return {"ok": ok, "failed": failed, "details": details, "endpoint": endpoint}


async def _run_wizard() -> int:
    menu_options = [
        "新增账号",
        "查看账号列表",
        "修改账号优先级",
        "启用账号",
        "禁用账号",
        "删除账号",
        "批量校验并刷新(login-all)",
        "批量测试(向所有账号发送 hi 请求)",
        "退出",
    ]
    while True:
        action = menu_options[_prompt_select(title="账户操作", options=menu_options, default_index=0)]

        if action == "退出":
            return 0

        try:
            if action == "新增账号":
                status = await list_codex_accounts()
                existing_labels = {
                    str(item.get("label") or "").strip().casefold() for item in status.get("accounts") or []
                }
                methods = ["browser", "headless"]
                method = methods[_prompt_select("登录方式", methods, default_index=0)]
                while True:
                    label = _prompt_field("label (账号标签，必须唯一)", required=True)
                    if label.strip().casefold() in existing_labels:
                        print(f"[accounts] label 已存在: {label}")
                        continue
                    break
                priority = _prompt_int("priority (数字越小优先级越高)", default=100)
                if method == "browser":
                    rec = await add_codex_account_via_browser_oauth(label=label, priority=priority)
                else:
                    rec = await add_codex_account_via_device_oauth(label=label, priority=priority)
                print(f"[accounts] added label={rec['label']} account_id={rec['account_id']}")
                await _print_accounts_list()
            elif action == "查看账号列表":
                await _print_accounts_list()
            elif action == "修改账号优先级":
                item = await _choose_account(
                    "选择要修改优先级的账号",
                    empty_message="当前没有可调整优先级的账号。",
                )
                if not item:
                    continue
                current = int(item.get("priority") or 100)
                priority = _prompt_int(
                    f"新的 priority（当前 {current}，数字越小优先级越高）",
                    default=current,
                )
                if priority == current:
                    print("[accounts] 优先级未变化。")
                    continue
                rec = await set_codex_account_priority(label=str(item["label"]), priority=priority)
                print(f"[accounts] priority updated label={rec['label']} priority={rec['priority']}")
                await _print_accounts_list()
            elif action == "启用账号":
                item = await _choose_account(
                    "选择要启用的账号",
                    enabled=False,
                    empty_message="当前没有已禁用的账号。",
                )
                if not item:
                    continue
                rec = await set_codex_account_enabled(label=str(item["label"]), enabled=True)
                print(f"[accounts] enabled label={rec['label']}")
                await _print_accounts_list()
            elif action == "禁用账号":
                item = await _choose_account(
                    "选择要禁用的账号",
                    enabled=True,
                    empty_message="当前没有可禁用的账号。",
                )
                if not item:
                    continue
                label = str(item["label"])
                if not _prompt_confirm(f"确认禁用账号 {label} 吗？", default=False):
                    print("[accounts] 已取消禁用。")
                    continue
                rec = await set_codex_account_enabled(label=label, enabled=False)
                print(f"[accounts] disabled label={rec['label']}")
                await _print_accounts_list()
            elif action == "删除账号":
                item = await _choose_account(
                    "选择要删除的账号",
                    empty_message="当前没有可删除的账号。",
                )
                if not item:
                    continue
                label = str(item["label"])
                if not _prompt_confirm(f"确认删除账号 {label} 吗？该操作不可恢复", default=False):
                    print("[accounts] 已取消删除。")
                    continue
                rec = await remove_codex_account(label=label)
                print(f"[accounts] removed label={rec['label']}")
                await _print_accounts_list()
            elif action == "批量校验并刷新(login-all)":
                result = await login_all_codex_accounts()
                print(f"[accounts] login-all 完成：成功 {result['ok']}，失败 {result['failed']}")
                for item in result.get("details") or []:
                    if item.get("ok"):
                        print(f"- {item['label']}: ok")
                    else:
                        print(f"- {item['label']}: failed error={item.get('error', '')}")
                await _print_accounts_list()
            elif action == "批量测试(向所有账号发送 hi 请求)":
                await batch_test_codex_accounts()
            else:
                raise SystemExit(f"Unknown wizard action: {action}")
        except Exception as e:
            print(f"[accounts] 操作失败: {e}")

        print("\n[accounts] 已返回主菜单。")


async def _run_command(args: argparse.Namespace) -> int:
    cmd = str(args.cmd or "")
    if not cmd:
        return await _run_wizard()

    if cmd == "add":
        method = str(args.method or "headless")
        if method == "browser":
            rec = await add_codex_account_via_browser_oauth(label=args.label, priority=int(args.priority))
        else:
            rec = await add_codex_account_via_device_oauth(label=args.label, priority=int(args.priority))
        print(f"[accounts] added label={rec['label']} account_id={rec['account_id']}")
        return 0

    if cmd == "list":
        await _print_accounts_list()
        return 0

    if cmd == "remove":
        rec = await remove_codex_account(label=args.label)
        print(f"[accounts] removed label={rec['label']}")
        return 0

    if cmd == "enable":
        rec = await set_codex_account_enabled(label=args.label, enabled=True)
        print(f"[accounts] enabled label={rec['label']}")
        return 0

    if cmd == "disable":
        rec = await set_codex_account_enabled(label=args.label, enabled=False)
        print(f"[accounts] disabled label={rec['label']}")
        return 0

    if cmd == "priority":
        rec = await set_codex_account_priority(label=args.label, priority=int(args.priority))
        print(f"[accounts] priority updated label={rec['label']} priority={rec['priority']}")
        return 0

    if cmd == "login-all":
        result = await login_all_codex_accounts()
        print(f"[accounts] login-all 完成：成功 {result['ok']}，失败 {result['failed']}")
        for item in result.get("details") or []:
            if item.get("ok"):
                print(f"- {item['label']}: ok")
            else:
                print(f"- {item['label']}: failed error={item.get('error', '')}")
        return 0

    if cmd == "test-all":
        await batch_test_codex_accounts()
        return 0

    raise SystemExit(f"Unknown command: {cmd}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Codex OAuth account pool")
    subparsers = parser.add_subparsers(dest="cmd")

    add_parser = subparsers.add_parser("add", help="Add an account")
    add_parser.add_argument("--label", required=True, help="Human-friendly unique account label")
    add_parser.add_argument("--priority", type=int, default=100, help="Lower value means higher priority")
    add_parser.add_argument("--method", choices=["browser", "headless"], default="headless")

    subparsers.add_parser("list", help="List accounts")

    remove_parser = subparsers.add_parser("remove", help="Remove account by label")
    remove_parser.add_argument("--label", required=True)

    enable_parser = subparsers.add_parser("enable", help="Enable account by label")
    enable_parser.add_argument("--label", required=True)

    disable_parser = subparsers.add_parser("disable", help="Disable account by label")
    disable_parser.add_argument("--label", required=True)

    priority_parser = subparsers.add_parser("priority", help="Update account priority by label")
    priority_parser.add_argument("--label", required=True)
    priority_parser.add_argument("--priority", type=int, required=True, help="Lower value means higher priority")

    subparsers.add_parser("login-all", help="Refresh/check all enabled accounts")
    subparsers.add_parser("test-all", help="Batch test: send 'hi' to all enabled accounts")
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run_command(args)))
    except Exception as e:
        raise SystemExit(f"[FATAL] accounts 管理失败: {e}")
