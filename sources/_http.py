"""共享 HTTP / HTML 抓取工具。

设计要点：
- 统一 UA + 超时
- 自动 follow redirect
- 自动探测编码（GB2312/GBK 的 .gov.cn 子站点常见）
- 简单的内存级 TTL 缓存：同 URL 在 TTL 内只打一次
- 失败/超时只 print 提示并返回 None，让上层降级
- trust_env=False：禁用 httpx 自动读 HTTP_PROXY/系统代理。
  Windows 上 Clash/v2rayN 退出后常残留 127.0.0.1:7890 系统代理设置，
  httpx 默认会去用，导致所有 HTTPS 请求 ConnectError。本工具直连即可。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

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


def _detect_encoding(content: bytes, declared: Optional[str]) -> str:
    """gov.cn 子站点经常返回 charset=utf-8 头，正文却是 GBK / GB2312。

    优先看 <meta charset>，其次看 Content-Type，最后试 UTF-8 → 失败回 GBK。
    """
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
    # 最后尝试用 utf-8 解码，失败 fallback 到 gb18030
    try:
        content[:1024].decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "gb18030"


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
    - 失败返回 None，错误吞到 print 里方便调试
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
        try:
            async with httpx.AsyncClient(
                timeout=timeout, headers=hh, follow_redirects=True,
                trust_env=False,
            ) as cli:
                if method.upper() == "POST":
                    r = await cli.post(url, json=json_body, params=params)
                else:
                    r = await cli.get(url, params=params)
                r.raise_for_status()
                enc = _detect_encoding(r.content, r.headers.get("content-type"))
                try:
                    text = r.content.decode(enc, errors="replace")
                except Exception:
                    text = r.text
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
            print(f"  [http] {method} {url[:80]}… {type(e).__name__}: {e}",
                  flush=True)
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
        print(f"  [http] JSON 解析失败 {url[:80]}…: {e}", flush=True)
        return None


def clear_cache() -> int:
    """清空缓存（调试用）。返回清掉的条数。"""
    n = len(_cache)
    _cache.clear()
    return n
