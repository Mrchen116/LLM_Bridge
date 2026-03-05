import argparse
import asyncio

from token_auth import (
    add_codex_account_via_browser_oauth,
    add_codex_account_via_device_oauth,
    list_codex_accounts,
    login_all_codex_accounts,
    remove_codex_account,
    set_codex_account_enabled,
    switch_codex_default_account,
)


def _prompt_select(title: str, options: list[str], default_index: int = 0) -> str:
    print(f"\n{title}")
    for idx, text in enumerate(options, start=1):
        mark = " (default)" if idx - 1 == default_index else ""
        print(f"  {idx}) {text}{mark}")
    raw = input("请选择编号: ").strip()
    if not raw:
        return options[default_index]
    try:
        idx = int(raw)
    except Exception:
        return options[default_index]
    if idx < 1 or idx > len(options):
        return options[default_index]
    return options[idx - 1]


def _prompt_field(title: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{title}{suffix}: ")
    val = raw.strip()
    if not val:
        return default
    return val


async def _print_accounts_list() -> None:
    status = await list_codex_accounts()
    default_label = str(status.get("default_label") or "")
    print(f"[accounts] default={default_label or '-'}")
    for item in status.get("accounts") or []:
        print(
            f"- label={item['label']} default={item['is_default']} enabled={item['enabled']} "
            f"priority={item['priority']} cooldown_until={item['cooldown_until']} "
            f"expires_at={item['expires_at']} account_id={item['account_id']}"
        )


async def _run_wizard() -> int:
    while True:
        action = _prompt_select(
            title="账户操作",
            options=[
                "新增账号",
                "查看账号列表",
                "启用账号",
                "禁用账号",
                "删除账号",
                "切换默认账号",
                "批量校验(login-all)",
                "退出",
            ],
            default_index=0,
        )

        if action == "退出":
            return 0

        try:
            if action == "新增账号":
                method = _prompt_select("登录方式", ["browser", "headless"], default_index=0)
                label = _prompt_field("label (账号标签，必须唯一)")
                priority_raw = _prompt_field("priority (数字越小优先级越高)", default="100")
                priority = int(priority_raw or "100")
                if method == "browser":
                    rec = await add_codex_account_via_browser_oauth(label=label, priority=priority)
                else:
                    rec = await add_codex_account_via_device_oauth(label=label, priority=priority)
                print(f"[accounts] added label={rec['label']} account_id={rec['account_id']}")
            elif action == "查看账号列表":
                await _print_accounts_list()
            elif action == "启用账号":
                label = _prompt_field("label (要启用的账号)")
                rec = await set_codex_account_enabled(label=label, enabled=True)
                print(f"[accounts] enabled label={rec['label']}")
            elif action == "禁用账号":
                label = _prompt_field("label (要禁用的账号)")
                rec = await set_codex_account_enabled(label=label, enabled=False)
                print(f"[accounts] disabled label={rec['label']}")
            elif action == "删除账号":
                label = _prompt_field("label (要删除的账号)")
                rec = await remove_codex_account(label=label)
                print(f"[accounts] removed label={rec['label']}")
            elif action == "切换默认账号":
                label = _prompt_field("label (切为默认)")
                rec = await switch_codex_default_account(label=label)
                print(f"[accounts] default switched to label={rec['label']}")
            elif action == "批量校验(login-all)":
                result = await login_all_codex_accounts()
                print(f"[accounts] login-all ok={result['ok']} failed={result['failed']}")
                for item in result.get("details") or []:
                    if item.get("ok"):
                        print(f"- label={item['label']} ok=true")
                    else:
                        print(f"- label={item['label']} ok=false error={item.get('error', '')}")
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

    if cmd == "switch":
        rec = await switch_codex_default_account(label=args.label)
        print(f"[accounts] default switched to label={rec['label']}")
        return 0

    if cmd == "login-all":
        result = await login_all_codex_accounts()
        print(f"[accounts] login-all ok={result['ok']} failed={result['failed']}")
        for item in result.get("details") or []:
            if item.get("ok"):
                print(f"- label={item['label']} ok=true")
            else:
                print(f"- label={item['label']} ok=false error={item.get('error', '')}")
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

    switch_parser = subparsers.add_parser("switch", help="Switch default account by label")
    switch_parser.add_argument("--label", required=True)

    subparsers.add_parser("login-all", help="Refresh/check all enabled accounts")
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run_command(args)))
    except Exception as e:
        raise SystemExit(f"[FATAL] accounts 管理失败: {e}")
