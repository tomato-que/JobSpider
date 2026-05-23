"""Anthropic Claude API 客户端"""
import json
import httpx

from .base import LLMClient, LLMMessage, LLMResponse


class AnthropicClient(LLMClient):
    """Anthropic Messages API 客户端"""

    def __init__(self, api_key: str, model: str, base_url: str, **kwargs):
        super().__init__(api_key, model, base_url)
        self._client = httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def chat(self, messages: list[LLMMessage], **kwargs) -> LLMResponse:
        system_msgs = [m.content for m in messages if m.role == "system"]
        user_msgs = [m for m in messages if m.role != "system"]

        body = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 2048),
            "temperature": kwargs.get("temperature", 0.3),
            "messages": [{"role": m.role, "content": m.content} for m in user_msgs],
        }
        if system_msgs:
            body["system"] = "\n".join(system_msgs)

        resp = await self._client.post(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            content=json.dumps(body),
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"Anthropic API 错误: {data['error']}")

        content_blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b["type"] == "text")

        return LLMResponse(
            content=text,
            model=data.get("model", self.model),
            usage=data.get("usage", {}),
        )

    async def extract(self, prompt: str, text: str, **kwargs) -> str:
        resp = await self.chat([
            LLMMessage(role="user", content=f"{prompt}\n\n{text}"),
        ], temperature=0.1, max_tokens=kwargs.get("max_tokens", 1024))
        return resp.content

    async def close(self):
        await self._client.aclose()
