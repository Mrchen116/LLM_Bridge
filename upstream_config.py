import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_ANTHROPIC_MESSAGES = "anthropic_messages"
PROTOCOL_OPENAI_CHAT = "openai_chat"
PROTOCOL_OPENAI_RESPONSES = "openai_responses"
SUPPORTED_PROTOCOLS = {PROTOCOL_ANTHROPIC_MESSAGES, PROTOCOL_OPENAI_CHAT, PROTOCOL_OPENAI_RESPONSES}
SUPPORTED_PROVIDERS = {"openai_compatible", "anthropic", "codex_oauth"}
SUPPORTED_AUTH_TYPES = {"internal_hw", "bearer", "anthropic_key", "none", "codex_oauth"}


class UpstreamConfigError(Exception):
    """上游配置错误。"""


class UpstreamCapabilityError(Exception):
    """当前上游 profile 不支持目标入口协议。"""


@dataclass
class ResolvedProfile:
    profile_name: str
    profile: Dict[str, Any]
    model: str


def _ensure_dict(name: str, value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise UpstreamConfigError(f"{name} 必须是对象")
    return value


def _ensure_list(name: str, value: Any) -> List[Any]:
    if not isinstance(value, list):
        raise UpstreamConfigError(f"{name} 必须是数组")
    return value


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _resolve_auth_type_for_profile(profile_name: str, provider: str, auth: Dict[str, Any]) -> str:
    if provider == "codex_oauth":
        # codex_oauth 的鉴权类型与 provider 绑定，不接受 auth.type 覆盖
        if auth.get("type") is not None:
            raise UpstreamConfigError(f"profiles.{profile_name}.provider=codex_oauth 时不允许配置 auth.type")
        return "codex_oauth"

    auth_type = auth.get("type")
    if auth_type is not None:
        if auth_type not in SUPPORTED_AUTH_TYPES:
            raise UpstreamConfigError(f"profiles.{profile_name}.auth.type 非法: {auth_type}")
        return auth_type

    # 仅内网场景需要显式 type，外网场景按 provider 自动推断，减少重复配置
    if provider == "openai_compatible":
        return "bearer"
    if provider == "anthropic":
        return "anthropic_key"
    raise UpstreamConfigError(f"profiles.{profile_name}.provider 非法: {provider}")


def load_and_validate_config(path: Optional[str] = None) -> Dict[str, Any]:
    cfg_path = path or os.getenv("UPSTREAM_CONFIG_PATH", "upstreams.json")
    if not os.path.exists(cfg_path):
        raise UpstreamConfigError(f"未找到上游配置文件: {cfg_path}")

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise UpstreamConfigError(f"读取配置失败: {cfg_path}, error={e}") from e

    root = _ensure_dict("root", data)
    default_profile = root.get("defaultProfile")
    profiles = _ensure_dict("profiles", root.get("profiles"))
    if not default_profile or not isinstance(default_profile, str):
        raise UpstreamConfigError("defaultProfile 必须是非空字符串")
    if default_profile not in profiles:
        raise UpstreamConfigError(f"defaultProfile={default_profile} 不存在于 profiles")
    if not profiles:
        raise UpstreamConfigError("profiles 不能为空")

    for name, raw_profile in profiles.items():
        profile = _ensure_dict(f"profiles.{name}", raw_profile)
        provider = profile.get("provider")
        if provider not in SUPPORTED_PROVIDERS:
            raise UpstreamConfigError(f"profiles.{name}.provider 非法: {provider}")
        base_url = profile.get("baseUrl")
        if not isinstance(base_url, str) or not base_url.strip():
            raise UpstreamConfigError(f"profiles.{name}.baseUrl 必须是非空字符串")

        auth = profile.get("auth") or {}
        auth = _ensure_dict(f"profiles.{name}.auth", auth)
        auth_type = _resolve_auth_type_for_profile(name, provider, auth)
        if auth_type == "bearer":
            if not isinstance(auth.get("apiKeyEnv"), str) or not auth.get("apiKeyEnv"):
                raise UpstreamConfigError(f"profiles.{name}.auth.apiKeyEnv 必填")
        if auth_type == "anthropic_key":
            if not isinstance(auth.get("apiKeyEnv"), str) or not auth.get("apiKeyEnv"):
                raise UpstreamConfigError(f"profiles.{name}.auth.apiKeyEnv 必填")
        if auth_type == "internal_hw":
            # 内网鉴权需要 bearer key + X-HW-*，避免运行期隐性失败
            required = ["apiKeyEnv", "hwIdEnv", "hwAppKeyEnv"]
            for key in required:
                if not isinstance(auth.get(key), str) or not auth.get(key):
                    raise UpstreamConfigError(f"profiles.{name}.auth.{key} 必填")
        if auth_type == "codex_oauth":
            codex_endpoint = auth.get("codexEndpoint")
            if codex_endpoint is not None and (not isinstance(codex_endpoint, str) or not codex_endpoint.strip()):
                raise UpstreamConfigError(f"profiles.{name}.auth.codexEndpoint 非法")

        capabilities = _ensure_dict(f"profiles.{name}.capabilities", profile.get("capabilities"))
        ingress = _ensure_list(f"profiles.{name}.capabilities.ingress", capabilities.get("ingress"))
        if not ingress:
            raise UpstreamConfigError(f"profiles.{name}.capabilities.ingress 不能为空")
        for item in ingress:
            if item not in SUPPORTED_PROTOCOLS:
                raise UpstreamConfigError(f"profiles.{name}.capabilities.ingress 包含非法值: {item}")

        defaults = _ensure_dict(f"profiles.{name}.defaults", profile.get("defaults"))
        default_model = defaults.get("model")
        if not isinstance(default_model, str) or not default_model.strip():
            raise UpstreamConfigError(f"profiles.{name}.defaults.model 必须是非空字符串")

    return root


def _normalize_model(raw_model: Any, default_model: str) -> str:
    # 新模式仍支持 byenv 作为“用 profile 默认模型”的语义别名
    if raw_model is None or raw_model == "" or raw_model == "byenv":
        return default_model
    return str(raw_model)


def resolve_profile(
    cfg: Dict[str, Any],
    body: Dict[str, Any],
    protocol: str,
) -> ResolvedProfile:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise UpstreamConfigError(f"未知协议: {protocol}")

    profiles = cfg["profiles"]
    default_profile = cfg["defaultProfile"]
    selected_profile = ""
    model_raw = body.get("model")

    # model 语法支持 profile:model
    if (not selected_profile) and isinstance(model_raw, str) and ":" in model_raw:
        maybe_profile, maybe_model = model_raw.split(":", 1)
        if maybe_profile in profiles:
            selected_profile = maybe_profile
            model_raw = maybe_model

    if not selected_profile:
        selected_profile = default_profile
    if selected_profile not in profiles:
        raise UpstreamConfigError(f"请求指定的 profile 不存在: {selected_profile}")

    profile = profiles[selected_profile]
    ingress = profile["capabilities"]["ingress"]
    if protocol not in ingress:
        raise UpstreamCapabilityError(
            f"profile={selected_profile} 不支持协议 {protocol}"
        )

    default_model = profile["defaults"]["model"]
    model = _normalize_model(model_raw, default_model)
    return ResolvedProfile(profile_name=selected_profile, profile=profile, model=model)


def build_upstream_url(profile: Dict[str, Any], protocol: str) -> str:
    base = str(profile["baseUrl"]).rstrip("/")
    provider = profile["provider"]
    auth = _ensure_dict("profile.auth", profile.get("auth") or {})
    auth_type = _resolve_auth_type_for_profile("runtime", provider, auth)
    if auth_type == "codex_oauth":
        # Codex OAuth 走 ChatGPT 的 codex 专用响应端点，不拼接 /chat/completions。
        return str(auth.get("codexEndpoint") or "https://chatgpt.com/backend-api/codex/responses").rstrip("/")

    if protocol == PROTOCOL_OPENAI_CHAT:
        if provider not in {"openai_compatible", "codex_oauth"}:
            raise UpstreamCapabilityError("anthropic provider 不支持 openai_chat")
        return f"{base}/chat/completions"
    if protocol == PROTOCOL_OPENAI_RESPONSES:
        if provider not in {"openai_compatible", "codex_oauth"}:
            raise UpstreamCapabilityError("anthropic provider 不支持 openai_responses")
        return f"{base}/responses"
    if protocol == PROTOCOL_ANTHROPIC_MESSAGES:
        if provider == "anthropic":
            # 兼容两种 baseUrl 写法：
            # - .../v1      -> .../v1/messages
            # - .../anthropic -> .../anthropic/v1/messages
            if base.endswith("/v1"):
                return f"{base}/messages"
            return f"{base}/v1/messages"
        return f"{base}/chat/completions"
    raise UpstreamConfigError(f"未知协议: {protocol}")


def get_runtime_options(profile: Dict[str, Any]) -> Tuple[bool, float, int, bool]:
    defaults = profile.get("defaults") or {}
    ssl_verify = _as_bool(defaults.get("sslVerify"), True)
    timeout_seconds = float(defaults.get("timeoutSeconds") or 500.0)
    retry_max = int(defaults.get("retryMax") or 20)
    retry_max = max(1, retry_max)
    # 某些运行环境访问外网上游必须经过系统代理，是否信任环境代理按 profile 显式配置。
    trust_env = _as_bool(defaults.get("trustEnv"), False)
    return ssl_verify, timeout_seconds, retry_max, trust_env


def build_auth_headers(profile: Dict[str, Any], model: str, x_auth_token: str = "") -> Dict[str, str]:
    auth = _ensure_dict("profile.auth", profile.get("auth") or {})
    auth_type = _resolve_auth_type_for_profile("runtime", profile["provider"], auth)
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
    }

    if auth_type == "none":
        return headers

    if auth_type == "bearer":
        api_key_env = auth["apiKeyEnv"]
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise UpstreamConfigError(f"未设置环境变量: {api_key_env}")
        headers["Authorization"] = f"Bearer {api_key}"
        return headers

    if auth_type == "anthropic_key":
        api_key_env = auth["apiKeyEnv"]
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise UpstreamConfigError(f"未设置环境变量: {api_key_env}")
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        return headers

    if auth_type == "internal_hw":
        api_key_env = auth["apiKeyEnv"]
        hw_id_env = auth["hwIdEnv"]
        hw_appkey_env = auth["hwAppKeyEnv"]
        api_key = os.getenv(api_key_env, "")
        hw_id = os.getenv(hw_id_env, "")
        hw_appkey = os.getenv(hw_appkey_env, "")
        if not api_key:
            raise UpstreamConfigError(f"未设置环境变量: {api_key_env}")
        if not hw_id:
            raise UpstreamConfigError(f"未设置环境变量: {hw_id_env}")
        if not hw_appkey:
            raise UpstreamConfigError(f"未设置环境变量: {hw_appkey_env}")
        if not x_auth_token:
            raise UpstreamConfigError("internal_hw 缺少 x_auth_token")

        headers["Authorization"] = f"Bearer {api_key}"
        headers["Model-Id"] = model
        headers["X-Auth-Token"] = x_auth_token
        headers["X-HW-ID"] = hw_id
        headers["X-HW-APPKEY"] = hw_appkey
        return headers

    if auth_type == "codex_oauth":
        access_token_env = str(auth.get("accessTokenEnv") or "CODEX_ACCESS_TOKEN")
        access_token = os.getenv(access_token_env, "")
        if not access_token:
            raise UpstreamConfigError(f"未设置环境变量: {access_token_env}")
        headers["Authorization"] = f"Bearer {access_token}"

        account_id_env = str(auth.get("accountIdEnv") or "CODEX_ACCOUNT_ID").strip()
        if account_id_env:
            account_id = os.getenv(account_id_env, "")
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
        return headers

    raise UpstreamConfigError(f"未知 auth.type: {auth_type}")


def get_effective_auth_type(profile: Dict[str, Any]) -> str:
    auth = _ensure_dict("profile.auth", profile.get("auth") or {})
    provider = str(profile.get("provider") or "")
    return _resolve_auth_type_for_profile("runtime", provider, auth)
