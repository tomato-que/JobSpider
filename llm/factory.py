"""LLM 工厂 — 根据配置创建对应的客户端"""
import os
from .base import LLMClient

# 支持的提供商元数据
PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "env_key": "DEEPSEEK_API_KEY",
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5"],
        "env_key": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "env_key": "OPENAI_API_KEY",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["deepseek/deepseek-chat", "anthropic/claude-sonnet-4-6", "openai/gpt-4o"],
        "env_key": "OPENROUTER_API_KEY",
    },
    "siliconflow": {
        "name": "硅基流动 (SiliconFlow)",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B"],
        "env_key": "SILICONFLOW_API_KEY",
    },
}


def list_providers() -> dict:
    """列出所有支持的提供商"""
    return PROVIDERS


def _get_config(provider: str) -> dict:
    """从环境变量读取指定提供商的配置"""
    if provider not in PROVIDERS:
        raise ValueError(f"不支持的 LLM 提供商: {provider}。可选: {list(PROVIDERS.keys())}")

    info = PROVIDERS[provider]
    api_key = os.getenv(info["env_key"], "")
    if not api_key:
        # 也尝试通用 LLM_API_KEY
        api_key = os.getenv("LLM_API_KEY", "")

    model = os.getenv("LLM_MODEL", info["models"][0])
    base_url = os.getenv("LLM_BASE_URL", info["base_url"])

    return {
        "api_key": api_key,
        "model": model,
        "base_url": base_url,
        "provider": provider,
    }


def get_llm(provider: str = None) -> LLMClient:
    """获取 LLM 客户端实例

    优先级：参数 > 环境变量 LLM_PROVIDER
    如果未配置提供商，抛出异常
    """
    provider = provider or os.getenv("LLM_PROVIDER", "")
    if not provider:
        raise ValueError(
            "未设置 LLM_PROVIDER。请在 .env 文件或前端面板中配置。\n"
            "可选值: deepseek | anthropic | openai | openrouter | siliconflow"
        )
    config = _get_config(provider)

    if not config["api_key"]:
        raise ValueError(
            f"未设置 {PROVIDERS[provider]['env_key']}。"
            f"请在 .env 文件或前端面板中填写 API Key。"
        )

    # 动态导入对应的客户端实现
    if provider == "deepseek" or provider == "openai" or provider == "openrouter" or provider == "siliconflow":
        from .openai_compat import OpenAICompatClient
        return OpenAICompatClient(**config)
    elif provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(**config)
    else:
        # 默认尝试 OpenAI 兼容接口
        from .openai_compat import OpenAICompatClient
        return OpenAICompatClient(**config)
