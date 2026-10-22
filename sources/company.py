"""企业官网聚合源 — 把多家大厂的官方 API 聚合到一个 source 下面

目前接入：
  - 字节跳动 (jobs.bytedance.com)
  - 腾讯     (careers.tencent.com)

后续可加：阿里、华为、美团、百度、小米…
"""
import asyncio
import time
from datetime import datetime

import httpx

from .base import BaseSource, make_id, parse_education, parse_experience


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")


# ─────────────── 腾讯 ───────────────

_TX_URL = "https://careers.tencent.com/tencentcareer/api/post/Query"
_TX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://careers.tencent.com/search.html",
    "User-Agent": _UA,
}


async def _fetch_tencent(limit: int, keyword: str, city: str) -> list[dict]:
    params = {
        "timestamp": int(time.time() * 1000),
        "keyword": keyword,
        "pageIndex": 1,
        "pageSize": min(max(limit, 10), 50),
        "language": "zh-cn",
        "area": "cn",
    }
    async with httpx.AsyncClient(timeout=15.0, headers=_TX_HEADERS,
                                 trust_env=False) as cli:
        r = await cli.get(_TX_URL, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("Code") != 200:
        return []
    items = (data.get("Data") or {}).get("Posts") or []
    out = []
    for p in items[:limit * 2]:
        post_id = p.get("PostId") or p.get("RecruitPostId") or ""
        title = p.get("RecruitPostName") or ""
        apply_url = p.get("PostURL") or (
            f"https://careers.tencent.com/jobdesc.html?postId={post_id}" if post_id else "")
        loc = p.get("LocationName") or p.get("CountryName") or None
        if not BaseSource.match_city(loc, city):
            continue
        upd = p.get("LastUpdateTime") or ""
        published_at = None
        if upd:
            try:
                dt = (upd.replace("年", "-").replace("月", "-")
                         .replace("日", "").strip())
                published_at = datetime.strptime(dt, "%Y-%m-%d").isoformat(timespec="seconds")
            except Exception:
                published_at = upd
        desc = p.get("Responsibility") or p.get("Requirement") or ""
        req = p.get("Requirement") or ""
        bg = p.get("BGName") or ""
        company = f"腾讯 · {bg}" if bg else "腾讯"
        out.append({
            "id": make_id("company:tencent", apply_url, post_id),
            "source": "company:tencent",
            "source_label": "腾讯",
            "title": title,
            "company": company,
            "location": loc,
            "education": parse_education(req) or parse_education(desc),
            "experience": parse_experience(req) or parse_experience(desc),
            "publish_time": published_at,
            "apply_url": apply_url,
            "summary": (desc or "").replace("\r", "").replace("\n", " ").strip()[:160] or None,
            "fetched_at": BaseSource.now_iso(),
        })
        if len(out) >= limit:
            break
    return out


# ─────────────── 网易 ───────────────

_NE_URL = "https://hr.163.com/api/hr163/position/queryPage"
_NE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://hr.163.com",
    "Referer": "https://hr.163.com/",
    "User-Agent": _UA,
}


async def _fetch_netease(limit: int, keyword: str, city: str) -> list[dict]:
    body = {
        "currentPage": 1,
        "pageSize": min(max(limit, 10), 50),
        "keyword": keyword,
    }
    async with httpx.AsyncClient(timeout=15.0, headers=_NE_HEADERS,
                                 trust_env=False) as cli:
        r = await cli.post(_NE_URL, json=body)
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 200:
        return []
    items = (data.get("data") or {}).get("list") or []
    out = []
    for p in items[:limit * 2]:
        post_id = str(p.get("id") or "")
        title = (p.get("name") or "").strip()
        if not title or not post_id:
            continue
        # 城市：workPlaceNameList: list[str]，取首个
        wp_list = p.get("workPlaceNameList") or []
        loc = wp_list[0] if wp_list else None
        if isinstance(loc, dict):  # 防御：少数情况下嵌套
            loc = loc.get("name") or None
        if not BaseSource.match_city(loc, city):
            continue
        upd = p.get("updateTime")
        published_at = None
        if isinstance(upd, (int, float)) and upd > 0:
            try:
                published_at = datetime.fromtimestamp(upd / 1000).isoformat(timespec="seconds")
            except Exception:
                pass
        edu = (p.get("reqEducationName") or "").strip() or None
        exp = (p.get("reqWorkYearsName") or "").strip() or None
        desc = p.get("description") or ""
        req = p.get("requirement") or ""
        product = (p.get("productName") or "").strip()
        company = f"网易 · {product}" if product else "网易"
        # 详情页 URL：beeUrl 是相对路径，常见形如 "/position/xxxx"
        bee = (p.get("beeUrl") or "").strip()
        apply_url = (f"https://hr.163.com{bee}" if bee.startswith("/")
                     else f"https://hr.163.com/position/detail.html?id={post_id}")
        summary = (desc or "").replace("\r", "").replace("\n", " ").strip()[:160] or None
        out.append({
            "id": make_id("company:netease", apply_url, post_id),
            "source": "company:netease",
            "source_label": "网易",
            "title": title,
            "company": company,
            "location": loc,
            "education": edu or parse_education(req) or parse_education(desc),
            "experience": exp or parse_experience(req) or parse_experience(desc),
            "publish_time": published_at,
            "apply_url": apply_url,
            "summary": summary,
            "fetched_at": BaseSource.now_iso(),
        })
        if len(out) >= limit:
            break
    return out


# ─────────────── 聚合 ───────────────

class CompanyOfficialSource(BaseSource):
    """聚合多家大厂官方招聘 API"""
    key = "company"
    display_name = "企业官网"
    category = "官方发布"

    # 各子源 fetch 函数，未来加阿里/美团就在这里加一行
    # 字节跳动（2026-05 调研）：API 强制要求查询参数携带 `_signature`，
    #   由前端 JS 动态生成，纯 httpx 无法构造；需要时用 Playwright 接入。
    # 美团（2026-05）：/api/web/position/list 返回 401「未登陆」，需登录态。
    # 百度（2026-05）：talent.baidu.com 旧 ajax 接口 302 → /jobs/404。
    _sub_sources = [
        ("腾讯", _fetch_tencent),
        ("网易", _fetch_netease),
    ]

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        per_sub = max(limit // len(self._sub_sources), 5)
        tasks = [fn(per_sub, keyword, city) for _, fn in self._sub_sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: list[dict] = []
        sub_errors: list[str] = []
        for label, res in zip([n for n, _ in self._sub_sources], results):
            if isinstance(res, Exception):
                err_msg = f"{label}: {type(res).__name__}"
                sub_errors.append(err_msg)
                print(f"  [企业官网/{label}] 异常: {res}", flush=True)
                continue
            merged.extend(res or [])
        # 子源全挂时，把汇总错误丢出去让上层进 errors[]
        if not merged and sub_errors:
            raise RuntimeError("企业官网子源全失败: " + "; ".join(sub_errors))
        return merged[:limit]
