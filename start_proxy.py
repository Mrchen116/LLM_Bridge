# start_proxy.py
import os
import argparse
import asyncio
import threading
import webbrowser
from dotenv import load_dotenv
from token_auth import ensure_codex_login_for_startup
from upstream_config import load_and_validate_config, UpstreamConfigError, get_effective_auth_type

load_dotenv(override=True)

def _has_codex_oauth_profile(cfg: dict) -> bool:
    profiles = cfg.get("profiles") if isinstance(cfg, dict) else {}
    if not isinstance(profiles, dict):
        return False
    for _, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        try:
            if get_effective_auth_type(profile) == "codex_oauth":
                return True
        except Exception:
            # 配置校验阶段会给出明确错误；这里只做探测
            continue
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the LLM proxy")
    parser.add_argument(
        "--ban_explore",
        action="store_true",
        help="Remove '- Explore:' line from Task tool descriptions in /v1/messages",
    )
    parser.add_argument(
        "--ban_stream",
        action="store_true",
        help="Disable stream requests for anthropic api /v1/messages",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Enable built-in session inspector web UI and API",
    )
    parser.add_argument(
        "--open-ui",
        action="store_true",
        help="Enable UI and open browser automatically",
    )
    return parser


def _apply_startup_env_flags(args: argparse.Namespace) -> None:
    if args.ban_explore:
        os.environ["BAN_EXPLORE"] = "true"
    if args.ban_stream:
        os.environ["BAN_STREAM"] = "true"
    if args.ui or args.open_ui:
        os.environ["ENABLE_SESSION_INSPECTOR_UI"] = "true"


def _schedule_ui_open(host: str, port: int) -> None:
    url = f"http://{host}:{port}/ui/session-inspector"
    timer = threading.Timer(0.8, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()


if __name__ == "__main__":
    import uvicorn

    parser = _build_parser()
    args = parser.parse_args()
    _apply_startup_env_flags(args)

    try:
        cfg = load_and_validate_config()
    except UpstreamConfigError as e:
        raise SystemExit(f"[FATAL] 上游配置校验失败: {e}")

    if _has_codex_oauth_profile(cfg):
        try:
            asyncio.run(ensure_codex_login_for_startup())
        except KeyboardInterrupt:
            raise SystemExit("[FATAL] Codex 登录被中断，服务未启动")
        except Exception as e:
            raise SystemExit(f"[FATAL] Codex 登录失败，服务未启动: {e}")

    host = os.getenv("PROXY_HOST", "127.0.0.1")
    port = int(os.getenv("PROXY_PORT", "4000"))
    if args.open_ui:
        _schedule_ui_open(host, port)
    uvicorn.run("app:app", host=host, port=port, log_level="info")
