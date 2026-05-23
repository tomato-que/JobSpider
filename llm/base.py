"""LLM 客户端基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class LLMMessage:
    role: str       # system / user / assistant
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens}


class LLMClient(ABC):
    """LLM 客户端抽象基类"""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    @abstractmethod
    async def chat(self, messages: list[LLMMessage], **kwargs) -> LLMResponse:
        """发送对话请求"""
        ...

    @abstractmethod
    async def extract(self, prompt: str, text: str, **kwargs) -> str:
        """结构化提取：从文本中提取关键信息"""
        ...

    async def analyze_page(self, dom_summary: str, goal: str) -> str:
        """分析页面结构，生成提取规则"""
        prompt = f"""你是一个网页数据提取专家。根据以下页面DOM摘要，生成JavaScript代码来提取指定信息。

任务目标：{goal}

页面DOM摘要：
{dom_summary}

请只返回可执行的JavaScript代码（一个自执行函数），不要包含任何解释或markdown格式。
代码应该返回一个对象数组，每个对象代表一条数据。
如果页面中没有相关数据，返回空数组[]。

代码格式示例：
(function() {{
  const items = document.querySelectorAll('...');
  return Array.from(items).map(el => ({{
    title: el.querySelector('...')?.textContent?.trim() || '',
  }}));
}})()"""
        resp = await self.chat([
            LLMMessage(role="user", content=prompt)
        ], temperature=0.1, max_tokens=2000)
        return resp.content

    async def classify_block(self, text: str, categories: list[str]) -> str:
        """分类文本块"""
        cat_str = ", ".join(categories)
        resp = await self.chat([
            LLMMessage(role="user", content=f"将以下文本分类为 [{cat_str}] 之一。只返回分类名称，不要解释。\n\n文本：{text}")
        ], temperature=0, max_tokens=20)
        return resp.content.strip()
