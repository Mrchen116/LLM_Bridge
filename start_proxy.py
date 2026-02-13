# start_proxy.py
import os
import argparse
from dotenv import load_dotenv
from upstream_config import load_and_validate_config, UpstreamConfigError

load_dotenv(override=True)

if __name__ == "__main__":
    import uvicorn

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
    args = parser.parse_args()

    if args.ban_explore:
        os.environ["BAN_EXPLORE"] = "true"

    if args.ban_stream:
        os.environ["BAN_STREAM"] = "true"

    try:
        load_and_validate_config()
    except UpstreamConfigError as e:
        raise SystemExit(f"[FATAL] 上游配置校验失败: {e}")

    host = os.getenv("PROXY_HOST", "127.0.0.1")
    port = int(os.getenv("PROXY_PORT", "4000"))
    uvicorn.run("app:app", host=host, port=port, log_level="info")