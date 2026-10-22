"""共享 HTTP / HTML 抓取工具。

设计要点：
- 模块级共享 AsyncClient（连接池 + keep-alive + 超时）
- 每 host 并发 2，429/5xx 指数退避，尊重 Retry-After
- 自动探测编码（GB2312/GBK 的 .gov.cn 子站点常见），严格试解后再 replace
- 简单的内存级 TTL 缓存：同 URL 在 TTL 内只打一次
- 失败记录到 last_errors，/api/public_jobs/refresh 会把它们带回前端
- trust_env=False：禁用 httpx 自动读 HTTP_PROXY/系统代理。
  Windows 上 Clash/v2rayN 退出后常残留 127.0.0.1:7890 系统代理设置，
  httpx 默认会去用，导致所有 HTTPS 请求 ConnectError。本工具直连即可。
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Optional
from urllib.parse import urlparse

import httpx


_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

# url -> (expire_ts, payload)
_cache: dict[str, tuple[float, str]] = {}
_inflight: dict[str, asyncio.Task] = {}
_MAX_CACHE_SIZE = 500

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_host_sems: dict[str, asyncio.Semaphore] = {}

_last_errors: list[dict] = []
_MAX_ERRORS = 50


def _detect_encoding(content: bytes, declared: Optional[str]) -> str:
    """返回用于严格解码的候选编码（按优先级）。"""
    head = content[:2048].decode("ascii", errors="ignore").lower()
    if 'charset="gb' in head or "charset=gb" in head or "charset='gb" in head:
        return "gb18030"
    if "charset=utf-8" in head or 'charset="utf-8"' in head:
        return "utf-8"
    if declared:
        d = declared.lower()
        if "gb" in d:
            return "gb18030"
        if "utf" in d:
            return "utf-8"
    try:
        content[:1024].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


def _decode(content: bytes, declared: Optional[str]) -> str:
    first = _detect_encoding(content, declared)
    candidates = [first, "utf-8", "gb18030"]
    seen = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def _record_error(url: str, err) -> None:
    _last_errors.append({
        "url": url[:160],
        "error": f"{type(err).__name__}: {err}"[:200],
        "at": time.strftime("%H:%M:%S"),
    })
    if len(_last_errors) > _MAX_ERRORS:
        del _last_errors[:-_MAX_ERRORS]


def last_errors() -> list[dict]:
    return list(_last_errors)


def drain_errors() -> list[dict]:
    out = list(_last_errors)
    _last_errors.clear()
    return out


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None:
            _client = httpx.AsyncClient(
                timeout=15.0,
                headers={"User-Agent": _DEFAULT_UA,
                         "Accept-Language": "zh-CN,zh;q=0.9"},
                follow_redirects=True,
                trust_env=False,
                limits=httpx.Limits(max_connections=20,
                                    max_keepalive_connections=10),
            )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _host_sem(url: str) -> asyncio.Semaphore:
    host = (urlparse(url).hostname or "").lower()
    sem = _host_sems.get(host)
    if sem is None:
        sem = asyncio.Semaphore(2)
        _host_sems[host] = sem
    return sem


async def fetch_text(
    url: str,
    *,
    timeout: float = 15.0,
    headers: Optional[dict] = None,
    ttl: float = 300.0,
    method: str = "GET",
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Optional[str]:
    """抓 URL → 解码后的字符串。

    - ttl > 0 时开内存缓存（同 URL+method+body 在 TTL 秒内只打一次）
    - 429/5xx/超时最多重试 3 次，指数退避 + 抖动
    - 失败返回 None，并把错误记入 last_errors
    """
    cache_key = f"{method}::{url}::{json_body!r}::{params!r}"
    now = time.time()
    if ttl > 0:
        cached = _cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        # 同一 url 多个并发请求共享一个抓取任务，避免雪崩
        running = _inflight.get(cache_key)
        if running and not running.done():
            try:
                return await running
            except Exception:
                return None

    async def _do() -> Optional[str]:
        hh = {"User-Agent": _DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        if headers:
            hh.update(headers)
        cli = await _get_client()
        sem = _host_sem(url)
        last_err = None
        for attempt in range(3):
            try:
                async with sem:
                    if method.upper() == "POST":
                        r = await cli.post(url, json=json_body, params=params,
                                           headers=hh, timeout=timeout)
                    else:
                        r = await cli.get(url, params=params, headers=hh,
                                          timeout=timeout)
                if r.status_code == 429 or 500 <= r.status_code < 600:
                    retry_after = r.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.0
                    except (TypeError, ValueError):
                        delay = 0.0
                    if delay <= 0:
                        delay = min(8.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5)
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    if attempt < 2:
                        await asyncio.sleep(delay)
                        continue
                    raise last_err
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")

                text = _decode(r.content, r.headers.get("content-type"))
                if ttl > 0:
                    if len(_cache) >= _MAX_CACHE_SIZE:
                        expired = [k for k, (ts, _) in _cache.items() if ts < now]
                        for k in expired:
                            del _cache[k]
                        if len(_cache) >= _MAX_CACHE_SIZE:
                            oldest = min(_cache, key=lambda k: _cache[k][0])
                            del _cache[oldest]
                    _cache[cache_key] = (now + ttl, text)
                return text
            except Exception as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(
                        min(8.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5))
                    continue
        _record_error(url, last_err)
        print(f"  [http] {method} {url[:80]}… "
              f"{type(last_err).__name__}: {last_err}", flush=True)
        return None

    task = asyncio.create_task(_do())
    if ttl > 0:
        _inflight[cache_key] = task
    try:
        return await task
    finally:
        if ttl > 0:
            _inflight.pop(cache_key, None)


async def fetch_json(url: str, **kw) -> Optional[dict | list]:
    """fetch_text + json.loads，方便用。"""
    import json
    text = await fetch_text(url, **kw)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception as e:
        _record_error(url, e)
        print(f"  [http] JSON 解析失败 {url[:80]}…: {e}", flush=True)
        return None


def clear_cache() -> int:
    """清空缓存（调试用）。返回清掉的条数。"""
    n = len(_cache)
    _cache.clear()
    return n
