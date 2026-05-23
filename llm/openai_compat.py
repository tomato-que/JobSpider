"""OpenAI 兼容接口客户端 (DeepSeek / OpenAI / OpenRouter / SiliconFlow)"""
import json
import httpx

from .base import LLMClient, LLMMessage, LLMResponse


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容的 Chat Completions API 客户端"""

    def __init__(self, api_key: str, model: str, base_url: str, **kwargs):
        super().__init__(api_key, model, base_url)
        self._client = httpx.AsyncClient(timeout=60.0, trust_env=False)

    async def chat(self, messages: list[LLMMessage], **kwargs) -> LLMResponse:
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        body = {
            "model": self.model,
            "messages": msgs,
            "temperature": kwargs.get("temperature", 0.3),
            "max_tokens": kwargs.get("max_tokens", 2048),
        }

        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(body),
        )
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            raise RuntimeError(f"LLM API 错误: {data['error']}")

        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"LLM API 返回无 choices: {data}")

        choice = choices[0]
        return LLMResponse(
            content=choice["message"]["content"],
            model=data.get("model", self.model),
            usage=data.get("usage", {}),
        )

    async def extract(self, prompt: str, text: str, **kwargs) -> str:
        resp = await self.chat([
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=text),
        ], temperature=0.1, max_tokens=kwargs.get("max_tokens", 1024))
        return resp.content

    async def close(self):
        await self._client.aclose()
