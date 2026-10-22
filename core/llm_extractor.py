"""LLM 驱动的自适应信息提取器"""
import asyncio
import json
import os
import re
from typing import Any, Optional

from llm import LLMClient, get_llm


# ───────── LLM 可用性判定（单一来源） ─────────

_LLM_KEY_ENV_VARS = (
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "OPENROUTER_API_KEY", "SILICONFLOW_API_KEY", "LLM_API_KEY",
)

# 全局限流：最多 3 个并发 LLM 请求，避免触发 API rate limit
_llm_semaphore = asyncio.Semaphore(3)


def is_llm_available() -> bool:
    """LLM 是否可用：LLM_ENABLED=true 或任一已知 API key 已配置。

    这是判断 LLM 是否启用的"唯一权威实现"。server.py / agents/master.py
    等任何外部模块都应改成 import 这个函数，而不是自己复制 env 检查。
    """
    enabled = os.getenv("LLM_ENABLED", "false").lower() == "true"
    has_key = any(os.getenv(k, "") for k in _LLM_KEY_ENV_VARS)
    return enabled or has_key


_MAX_PROMPT_CHARS = 12000


def _truncate_prompt(text: str, max_chars: int = _MAX_PROMPT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...(已截断)"


_FENCE_HEAD_RE = re.compile(r"^```\w*\n?")
_FENCE_TAIL_RE = re.compile(r"\n```$")


def _strip_fences(text: str) -> str:
    """剥掉 LLM 回答外面可能裹的 ```json ... ``` 代码块"""
    s = text.strip()
    if s.startswith("```"):
        s = _FENCE_HEAD_RE.sub("", s)
        s = _FENCE_TAIL_RE.sub("", s)
    return s


class LLMExtractor:
    """使用 LLM 自适应提取页面数据"""

    def __init__(self):
        self._client: Optional[LLMClient] = None

    async def _get_client(self) -> Optional[LLMClient]:
        # 动态读取：LLM_ENABLED=true 或配置了任意 API key 都视为可用
        if not is_llm_available():
            return None
        if self._client is None:
            try:
                self._client = get_llm()
            except (ValueError, RuntimeError) as e:
                print(f"  [LLM] 初始化失败(回退纯脚本模式): {e}")
                return None
        return self._client

    async def _chat_to_json(
        self,
        prompt: str,
        *,
        default: Any = None,
        system: Optional[str] = None,
        temperature: float = 0,
        max_tokens: int = 800,
        tag: str = "chat_to_json",
    ) -> Any:
        """调一次 LLM 并把返回内容解析为 JSON。

        - 失败（包括 LLM 不可用 / 网络异常 / JSON 解析失败）都返回 `default`
        - 自动剥掉 ```json``` 代码块包裹
        - `tag` 仅用于失败日志定位
        """
        client = await self._get_client()
        if not client:
            return default
        messages = []
        if system:
            messages.append(llm_message("system", system))
        messages.append(llm_message("user", _truncate_prompt(prompt)))
        try:
            async with _llm_semaphore:
                resp = await client.chat(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
            content = _strip_fences(resp.content)
            return json.loads(content)
        except Exception as e:
            print(f"  [LLM/{tag}] 失败: {e}", flush=True)
            return default

    async def generate_selectors(self, dom_text: str, platform: str) -> dict:
        """分析DOM生成CSS选择器"""
        if not await self._get_client():
            return {}

        prompt = f"""你是{platform}网站的前端专家。根据页面的DOM文本片段，生成提取职位信息的CSS选择器。

DOM:
{dom_text[:3000]}

返回JSON格式（不要markdown包裹）:
{{
  "list_selector": "职位卡片容器的CSS选择器",
  "title": "职位名称的选择器",
  "salary": "薪资的选择器",
  "company": "公司名称的选择器",
  "city": "城市/地区的选择器",
  "experience": "经验要求的选择器",
  "education": "学历要求的选择器",
  "job_url": "职位链接的选择器(a标签)",
  "publish_time": "发布时间的选择器",
  "tags": "标签的选择器(多个)",
  "description": "职位描述的选择器(详情页)"
}}"""
        return await self._chat_to_json(
            prompt, default={}, temperature=0.1, max_tokens=1000,
            tag="generate_selectors",
        )

    async def extract_from_text(self, raw_text: str, fields: list[str]) -> dict:
        """从非结构化文本中提取字段"""
        if not await self._get_client():
            return {}

        field_list = ", ".join(fields)
        prompt = f"""从以下招聘信息文本中提取关键字段。返回JSON格式(不要markdown)。

需要提取: {field_list}

如果某个字段不存在，值设为null。

文本:
{raw_text[:2000]}"""
        return await self._chat_to_json(
            prompt, default={}, temperature=0, max_tokens=800,
            tag="extract_from_text",
        )

    async def parse_salary_smart(self, text: str) -> dict:
        """智能解析薪资（处理各种非标准格式）"""
        if not await self._get_client():
            return {}

        prompt = f"""将以下薪资文本解析为标准格式，只返回JSON:
{{
  "salary_min": 数字(K/月),
  "salary_max": 数字(K/月),
  "salary_type": "月薪" | "年薪" | "面议" | "日薪"
}}

如果无法解析，值设为null。

薪资文本: {text}"""
        return await self._chat_to_json(
            prompt, default={}, temperature=0, max_tokens=200,
            tag="parse_salary_smart",
        )

    async def deduplicate_smart(self, jobs_json: str) -> list[int]:
        """智能去重：返回重复项的索引列表"""
        if not await self._get_client():
            return []

        prompt = f"""分析以下职位数据中哪些是重复的（同一公司同一职位）。返回重复项的索引列表JSON数组，如[2,5]。

职位数据:
{jobs_json[:3000]}"""
        result = await self._chat_to_json(
            prompt, default=[], temperature=0, max_tokens=500,
            tag="deduplicate_smart",
        )
        if isinstance(result, list):
            return result
        return []

    async def parse_resume(self, resume_text: str) -> dict:
        """把简历原文解析成结构化摘要；失败返回 {}。"""
        if not resume_text or not resume_text.strip():
            return {}
        if not await self._get_client():
            return {}

        prompt = f"""你是简历解析器。只提取简历中明确写出的事实，不要推测、不要补全。
返回 JSON（不要 markdown 包裹）：
{{
  "name": "姓名，找不到则 null",
  "years": 工作年限数字或 null,
  "current_role": "当前/最近职位，找不到则 null",
  "skills": ["硬技能/技术栈/行业能力"],
  "highlights": ["代表项目、成果或职责"],
  "education": "最高学历/学校/专业，找不到则 null",
  "preferred_roles": ["求职方向"]
}}

规则：
- skills 最多 15 个，优先具体技术、工具、业务能力
- highlights 2-5 条，每条不超过 40 字，必须来自简历
- 不输出手机号、邮箱、身份证、住址、政治面貌等隐私信息
- 没有信息的字段用 null 或 []

简历:
{resume_text[:6000]}"""
        data = await self._chat_to_json(
            prompt, default={}, temperature=0.1, max_tokens=600,
            tag="parse_resume",
        )
        return data if isinstance(data, dict) else {}

    async def generate_greeting(self, job_title: str, company_name: str,
                                jd_text: str, user_template: str = None,
                                resume_text: str = "") -> str:
        """根据岗位 JD（以及可选的简历摘要）生成个性化打招呼语。"""
        client = await self._get_client()
        if not client:
            return self._fallback_greeting(job_title, company_name)

        resume_text = (resume_text or "").strip()[:1200]
        if resume_text:
            prompt = f"""你是一个正在求职的候选人。请结合【简历摘要】和【职位信息】，生成一段专业、简洁的打招呼语（50-100字）。
要求：
- 只使用简历摘要中明确存在的信息，禁止编造公司、项目、年限、学历、技术栈
- 从简历中挑 1-2 个与 JD 最匹配的技能或经历，简要提及
- 如果简历与岗位不匹配，表达兴趣和可迁移能力，不要硬编
- 不要吹嘘"丰富的经验""深厚的功底""多年经验"等，保持谦虚诚恳
- 语气自然真诚，不要过于客套；不要使用"贵公司"等过于正式的表达
- 不要输出手机号、邮箱等联系方式

职位: {job_title}
公司: {company_name}
JD详情: {jd_text[:1500]}

简历摘要:
{resume_text}
{f'参考模板风格: {user_template}' if user_template else ''}

直接输出打招呼语文本，不要任何前缀或解释。"""
        else:
            prompt = f"""你是一个正在求职的候选人。根据以下职位信息，生成一段专业、简洁的打招呼语（50-100字）。
要求：
- 表达对该职位的兴趣
- 可以简要提及自己关注的技术方向或职责，但不要编造具体经验
- 不要吹嘘自己"丰富的经验""深厚的功底""多年经验"等，保持谦虚诚恳
- 语气自然真诚，不要过于客套
- 不要使用"贵公司"等过于正式的表达

职位: {job_title}
公司: {company_name}
JD详情: {jd_text[:1500]}
{f'参考模板风格: {user_template}' if user_template else ''}

直接输出打招呼语文本，不要任何前缀或解释。"""

        prompt = _truncate_prompt(prompt)
        # greeting 不是 JSON，要保留原始 plain-text 走通的路径
        try:
            async with _llm_semaphore:
                resp = await client.chat(
                    [llm_message("user", prompt)],
                    temperature=0.7, max_tokens=220
                )
            greeting = resp.content.strip().strip('"').strip("'")
            if len(greeting) > 200:
                greeting = greeting[:200]
            return greeting
        except Exception:
            return self._fallback_greeting(job_title, company_name)

    @staticmethod
    def _fallback_greeting(job_title: str, company_name: str) -> str:
        """LLM 不可用时的回退打招呼语 — 用 (title,company) 哈希在多个模板中确定性采样,
        避免在批量投递场景下发送完全相同的文本（被风控识别为模板）。
        """
        import hashlib
        title = (job_title or "").strip() or "该岗位"
        company = (company_name or "").strip() or "贵公司"
        templates = [
            "您好，看到{company}在招{title}，对岗位职责很感兴趣，希望有机会进一步沟通。",
            "您好，我对{company}的{title}方向比较关注，方便聊一下岗位的具体要求吗？",
            "您好，{title}岗位的方向跟我目前关注的领域比较契合，想了解一下{company}团队情况。",
            "Hi，看到{company}的{title}招聘，岗位描述跟我想做的事比较匹配，希望进一步交流。",
            "您好，{title}这个岗位很吸引我，想多了解一下{company}团队和具体业务，期待沟通。",
            "您好，对{company}的{title}岗位很感兴趣，想请教下日常工作内容和团队规模，谢谢。",
            "您好，看到{title}的招聘信息，岗位方向我比较有兴趣，想多了解一下{company}的情况。",
        ]
        # 用 (company, title) 的 md5 决定使用哪个模板 — 同一岗位每次都一致，跨岗位多样化
        h = hashlib.md5(f"{company}|{title}".encode("utf-8")).hexdigest()
        idx = int(h[:8], 16) % len(templates)
        text = templates[idx].format(title=title[:30], company=company[:30])
        # 安全裁剪
        return text[:200]

    async def should_skip_by_rule(self, job_brief: str,
                                  exclude_rules: str) -> tuple[bool, str]:
        """根据用户的自然语言规则判断该岗位是否应该跳过。

        job_brief: 岗位简要信息（标题/公司/薪资/标签/JD摘要）
        exclude_rules: 用户写的排除规则，自然语言
        返回 (是否跳过, 跳过原因)
        """
        if not exclude_rules or not exclude_rules.strip():
            return False, ""
        if not await self._get_client():
            return False, ""
        prompt = f"""你是求职助手。判断以下岗位是否应该跳过（不投递）。

用户的排除规则:
{exclude_rules}

岗位信息:
{job_brief}

严格按下列 JSON 格式直接输出，不要任何解释、不要包裹代码块:
{{"skip": true/false, "reason": "20字以内的原因"}}

注意:
- 只有明显匹配排除规则时才设 skip=true
- 信息不足以判断时一律 skip=false
- reason 简短中文"""
        data = await self._chat_to_json(
            prompt, default=None, temperature=0.1, max_tokens=80,
            tag="should_skip_by_rule",
        )
        if not isinstance(data, dict):
            return False, ""
        return bool(data.get("skip", False)), str(data.get("reason", ""))[:30]

    async def enrich_public_job(self, brief: dict) -> dict:
        """从公开岗位的标题/摘要/正文片段里抠出结构化字段。

        brief 期望含：title / company / summary / location / education /
                      experience / publish_time / detail_text(可选)

        返回非空字段（缺失/无法判断的字段不出现在结果里，由调用方保留原值）：
          {"deadline", "location", "education", "experience", "salary"}
        """
        if not await self._get_client():
            return {}
        ctx_lines = []
        for k in ("title", "company", "publish_time"):
            v = brief.get(k)
            if v:
                ctx_lines.append(f"{k}: {v}")
        if brief.get("summary"):
            ctx_lines.append(f"summary: {brief['summary']}")
        if brief.get("detail_text"):
            ctx_lines.append(f"正文片段: {brief['detail_text'][:1500]}")
        if not ctx_lines:
            return {}
        ctx = "\n".join(ctx_lines)

        prompt = f"""你是中文招聘公告解析器。从下面的招聘信息中提取结构化字段。
只返回 JSON（不要 markdown 包裹），字段含义：
{{
  "deadline":   "报名/投递截止日期，格式 YYYY-MM-DD；找不到则 null",
  "location":   "工作城市（去掉'市'后缀，多城市用顿号分隔）；找不到则 null",
  "education":  "学历要求：本科 / 硕士 / 博士 / 大专 / 不限；找不到则 null",
  "experience": "经验要求：应届 / 1-3年 / 3-5年 / 5年以上 / 不限；找不到则 null",
  "salary":     "薪资描述（如 15-25K 或 面议 或 年薪30-50万）；找不到则 null"
}}

招聘信息:
{ctx[:3000]}"""
        data = await self._chat_to_json(
            prompt, default=None, temperature=0, max_tokens=300,
            tag="enrich_public_job",
        )
        if not isinstance(data, dict):
            return {}
        # 清洗 — 空字符串/null/"无"/"不详"统一转 None
        out = {}
        for k in ("deadline", "location", "education", "experience", "salary"):
            v = data.get(k)
            if v in (None, "", "无", "不详", "未提及", "未说明", "无要求"):
                continue
            out[k] = str(v)[:60]
        return out

    async def score_public_job(self, brief: dict, prefs: str) -> dict:
        """按用户偏好给岗位打 0-100 的匹配分。

        返回 {"score": int, "reason": str}；LLM 不可用时返回 {}
        """
        if not prefs or not prefs.strip():
            return {}
        if not await self._get_client():
            return {}

        job_lines = []
        for k in ("title", "company", "location", "education",
                 "experience", "salary", "publish_time"):
            v = brief.get(k)
            if v:
                job_lines.append(f"{k}: {v}")
        if brief.get("summary"):
            job_lines.append(f"summary: {brief['summary'][:300]}")
        job_text = "\n".join(job_lines)

        prompt = f"""你是求职助手。根据用户偏好给以下岗位打 0-100 的匹配分。

用户偏好（自然语言）:
{prefs[:600]}

岗位信息:
{job_text[:1500]}

只返回 JSON（不要 markdown）:
{{"score": 0-100 的整数, "reason": "20字以内的中文原因"}}

打分规则:
- 90-100: 强烈匹配（关键条件全命中）
- 70-89:  较匹配（多数条件命中，少数缺失或不冲突）
- 50-69:  一般（部分匹配，部分模糊）
- 30-49:  弱匹配（明显短板）
- 0-29:   不匹配/冲突（明确违反偏好）
- 信息不足时给 50，并在 reason 注明"信息不足"
"""
        data = await self._chat_to_json(
            prompt, default=None, temperature=0.1, max_tokens=120,
            tag="score_public_job",
        )
        if not isinstance(data, dict):
            return {}
        try:
            score = int(data.get("score", 50))
        except (TypeError, ValueError):
            score = 50
        score = max(0, min(100, score))
        return {"score": score, "reason": str(data.get("reason", ""))[:40]}

    async def close(self):
        if self._client and hasattr(self._client, 'close'):
            await self._client.close()
            self._client = None


def llm_message(role: str, content: str):
    """创建LLM消息对象的辅助函数"""
    from llm.base import LLMMessage
    return LLMMessage(role=role, content=content)
