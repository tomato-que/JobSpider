"""公开岗位 LLM 增强 / 打分的纯业务逻辑（无 FastAPI 依赖）。

把原本散落在 server.py 的 _pj_enrich_cache / _pj_score_cache /
_BULLETIN_RE / enrich / score 逻辑搬过来。

server.py 的端点函数后续可以改成调用本模块的 PublicJobsEnricher / PublicJobsScorer
实例，避免在 HTTP handler 里写一堆业务逻辑。本次重构不修改 server.py，
等后续单独 PR 切换。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Optional

from core.llm_extractor import LLMExtractor, is_llm_available
from core.log import get_logger

log = get_logger("public_jobs_service")


# 公告类源（title-only，summary 几乎是空的）— 跑 LLM 前需要先抓详情页正文
BULLETIN_SRC_RE = re.compile(r"(sasac|mohrss|gov|wechat|rsshub)", re.I)

# 单次最多处理多少条，防 API 用量爆掉
_MAX_BATCH = 80
# 并发数 — 控制对 LLM 服务方的瞬时压力
_CONCURRENCY = 4
_MAX_CACHE_SIZE = 2000


async def _maybe_fetch_detail(it: dict) -> Optional[str]:
    """对公告类源补抓详情页正文。

    返回 detail_text（截断至 3000 字以内），不需要补抓 / 抓失败时返回 None。
    """
    src = it.get("source") or ""
    url = it.get("apply_url") or ""
    if not url or not BULLETIN_SRC_RE.search(src):
        return None
    # 已经有 summary 的不再多抓一次
    if (it.get("summary") or "").strip():
        return None
    try:
        # 延迟 import — bs4/sources._http 都是重依赖，调用方没用到时不要拉进来
        from sources._http import fetch_text
        from bs4 import BeautifulSoup
        html = await fetch_text(url, ttl=600.0, timeout=12.0)
        if not html or len(html) < 200:
            return None
        soup = BeautifulSoup(html, "lxml")
        # 大多数中文政府/公告页：正文在 .TRS_Editor / .pages_content / #zoom / .article 里
        for sel in (".TRS_Editor", ".pages_content", "#zoom", ".article",
                    ".content", ".main-content", "article"):
            node = soup.select_one(sel)
            if node:
                text = node.get_text(" ", strip=True)
                if len(text) > 80:
                    return text[:3000]
        # fallback: 整个 body 抽文本
        body = soup.find("body")
        if body:
            return body.get_text(" ", strip=True)[:3000]
    except Exception as e:
        log.error("fetch_detail failed",
                  url=url, error=type(e).__name__, detail=str(e))
    return None


class PublicJobsEnricher:
    """对公开岗位条目做 LLM 字段抽取（deadline / location / education / ...）。

    内置进程级缓存，按 item id 去重，避免同一岗位反复花 API 费用。
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._llm = LLMExtractor()

    @property
    def cache(self) -> dict[str, dict]:
        return self._cache

    async def enrich(
        self,
        items: list[dict],
    ) -> dict[str, Any]:
        """跑 LLM 增强。

        返回:
          {"results": {id: {字段}}, "cached": n, "fresh": m}
          或 {"error": "..."} 当 LLM 不可用 / 入参为空
        """
        if not is_llm_available():
            return {"error": "未配置 LLM API Key — 请到「配置」面板设置或 .env 中填写"}

        if not items:
            return {"results": {}}

        cached: dict[str, dict] = {}
        pending: list[dict] = []
        for it in items[:_MAX_BATCH]:
            _id = it.get("id")
            if not _id:
                continue
            if _id in self._cache:
                cached[_id] = self._cache[_id]
            else:
                pending.append(it)

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def one(it: dict) -> tuple[str, dict]:
            async with sem:
                try:
                    detail = await _maybe_fetch_detail(it)
                    if detail:
                        it = {**it, "detail_text": detail}
                    r = await self._llm.enrich_public_job(it)
                except Exception as e:
                    log.error("enrich item failed",
                              id=it.get("id"),
                              error=type(e).__name__, detail=str(e))
                    r = {}
                return it["id"], r

        pairs = await asyncio.gather(*[one(it) for it in pending])
        fresh: dict[str, dict] = {}
        for _id, r in pairs:
            self._cache[_id] = r
            fresh[_id] = r

        # LRU 淘汰
        if len(self._cache) > _MAX_CACHE_SIZE:
            excess = len(self._cache) - _MAX_CACHE_SIZE
            for k in list(self._cache)[:excess]:
                del self._cache[k]

        return {
            "results": {**cached, **fresh},
            "cached": len(cached),
            "fresh": len(fresh),
        }


class PublicJobsScorer:
    """根据用户偏好给岗位打 0-100 匹配分。

    缓存 key 把 prefs 哈希也算进去 — 偏好变了要重算。
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._llm = LLMExtractor()

    @property
    def cache(self) -> dict[str, dict]:
        return self._cache

    @staticmethod
    def _prefs_hash(prefs: str) -> str:
        return hashlib.md5(prefs.encode("utf-8")).hexdigest()[:10]

    async def score(
        self,
        items: list[dict],
        prefs: str,
    ) -> dict[str, Any]:
        """跑 LLM 打分。

        返回:
          {"results": {id: {score, reason}}, "cached": n, "fresh": m, "prefs_hash": h}
          或 {"error": "..."} 当 LLM 不可用 / prefs 为空 / 入参为空
        """
        if not is_llm_available():
            return {"error": "未配置 LLM API Key — 请到「配置」面板设置或 .env 中填写"}

        prefs = (prefs or "").strip()
        if not prefs:
            return {"error": "需要先在「求职偏好」里写明你的期望（自然语言即可）"}

        if not items:
            return {"results": {}}

        prefs_hash = self._prefs_hash(prefs)

        cached: dict[str, dict] = {}
        pending: list[dict] = []
        for it in items[:_MAX_BATCH]:
            _id = it.get("id")
            if not _id:
                continue
            ckey = f"{_id}:{prefs_hash}"
            if ckey in self._cache:
                cached[_id] = self._cache[ckey]
            else:
                pending.append(it)

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def one(it: dict) -> tuple[str, dict]:
            async with sem:
                try:
                    r = await self._llm.score_public_job(it, prefs)
                except Exception as e:
                    log.error("score item failed",
                              id=it.get("id"),
                              error=type(e).__name__, detail=str(e))
                    r = {}
                return it["id"], r

        pairs = await asyncio.gather(*[one(it) for it in pending])
        fresh: dict[str, dict] = {}
        for _id, r in pairs:
            self._cache[f"{_id}:{prefs_hash}"] = r
            fresh[_id] = r

        # LRU 淘汰
        if len(self._cache) > _MAX_CACHE_SIZE:
            excess = len(self._cache) - _MAX_CACHE_SIZE
            for k in list(self._cache)[:excess]:
                del self._cache[k]

        return {
            "results": {**cached, **fresh},
            "cached": len(cached),
            "fresh": len(fresh),
            "prefs_hash": prefs_hash,
        }
