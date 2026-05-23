"""LLM 抽象层 — 支持多种 API 提供商"""
from .base import LLMClient
from .factory import get_llm, list_providers

__all__ = ["LLMClient", "get_llm", "list_providers"]
