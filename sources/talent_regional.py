"""地方人才网聚合源 — 覆盖各省市人才市场官方网站

策略：
  1. 广东人才网 (gdrc.com) 有清晰的 HTML 列表结构，直接解析
  2. 广西人才网 (gxrc.com) 有搜索 URL 但返回 SPA 壳，用 Bing 站内搜索
  3. 其余站点统一用 Bing site: 搜索兜底

站点列表来源：各省市政府人力资源官网 + 用户提供的参考图片。
"""
import asyncio
import re
from typing import Optional
from urllib.parse import urljoin, quote

from bs4 import BeautifulSoup

from .base import BaseSource, make_id
from ._http import fetch_text


# ─── 站点目录 ───
# domain: 用于 Bing site: 搜索
# name:   前端 source_label
# city:   所属省/市（用于默认城市关联）

_SITES: list[dict] = [
    {"domain": "www.bjrc.com.cn", "name": "北京人才网", "city": "北京"},
    {"domain": "www.fsg.com.cn", "name": "上海外服", "city": "上海"},
    {"domain": "sdrc.com.cn", "name": "山东人才网", "city": "山东"},
    {"domain": "www.gdrc.com", "name": "广东人才网", "city": "广东"},
    {"domain": "www.zjrc.com", "name": "浙江人才网", "city": "浙江"},
    {"domain": "www.scrc168.com", "name": "四川人才网", "city": "四川"},
    {"domain": "www.gxrc.com", "name": "广西人才网", "city": "广西"},
    {"domain": "www.hbrc.com.cn", "name": "河北人才网", "city": "河北"},
    {"domain": "www.hnrcsc.com", "name": "湖南人才网", "city": "湖南"},
    {"domain": "www.jobhb.com", "name": "湖北人才网", "city": "湖北"},
    {"domain": "www.hnrc.com.cn", "name": "河南人才网", "city": "河南"},
    {"domain": "www.jxrcw.com", "name": "江西人才网", "city": "江西"},
    {"domain": "www.fjrc.com", "name": "福建人才网", "city": "福建"},
    {"domain": "www.ahrc.com.cn", "name": "安徽人才网", "city": "安徽"},
    {"domain": "www.lnrc.com.cn", "name": "辽宁人才网", "city": "辽宁"},
    {"domain": "www.jlrc.com.cn", "name": "吉林人才网", "city": "吉林"},
    {"domain": "www.hljrc.com", "name": "黑龙江人才网", "city": "黑龙江"},
    {"domain": "www.jsrc.com.cn", "name": "江苏人才网", "city": "江苏"},
    {"domain": "www.tjrc.com.cn", "name": "天津人才网", "city": "天津"},
    {"domain": "www.cqrc.net", "name": "重庆人才网", "city": "重庆"},
    {"domain": "www.ynrc.com.cn", "name": "云南人才网", "city": "云南"},
    {"domain": "www.gzrc.com.cn", "name": "贵州人才网", "city": "贵州"},
    {"domain": "www.gsrc.com", "name": "甘肃人才网", "city": "甘肃"},
    {"domain": "www.nxrc.com.cn", "name": "宁夏人才网", "city": "宁夏"},
    {"domain": "www.sxrc.com.cn", "name": "陕西人才网", "city": "陕西"},
    {"domain": "www.nmgrc.com", "name": "内蒙古人才网", "city": "内蒙古"},
    {"domain": "www.xjrc.com.cn", "name": "新疆人才网", "city": "新疆"},
    {"domain": "www.qhrc.com.cn", "name": "青海人才网", "city": "青海"},
    {"domain": "www.hnrc.com", "name": "海南人才网", "city": "海南"},
]


# ─── 广东人才网 (GDRC) 直接 HTML 解析 ───
# 搜索: /index.php?m=&c=jobs&a=jobs_list&key={keyword}
# 结构: div.positionItemBorder > a[href*="jobs_show"]
#   a 内部: span.title / span.wages / div.welfare (多个) / div.companyName / div.industry

_GDRC_SEARCH = "https://www.gdrc.com/index.php?m=&c=jobs&a=jobs_list&key={kw}"
_GDRC_BASE = "https://www.gdrc.com"

_LOC_RE = re.compile(r"\[([^\]]+)\]")


async def _fetch_gdrc(limit: int, keyword: str, city: str) -> list[dict]:
    kw = keyword.strip()
    url = _GDRC_SEARCH.format(kw=quote(kw))
    html = await fetch_text(url, ttl=180.0)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    links = soup.select('div.positionItemBorder > a[href*="jobs_show"]')
    if not links:
        return []

    city_key = (city or "").replace("市", "").strip()
    out: list[dict] = []
    seen: set[str] = set()

    for a in links:
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        title_el = a.select_one("span.title")
        if not title_el:
            continue
        title_text = title_el.get_text(strip=True)
        if not title_text:
            continue

        loc_m = _LOC_RE.search(title_text)
        location = loc_m.group(1) if loc_m else None
        title = title_text[:loc_m.start()].strip() if loc_m else title_text

        if city_key and not BaseSource.match_city(location, city_key):
            continue

        wages_el = a.select_one("span.wages")
        salary = wages_el.get_text(strip=True) if wages_el else None

        welfare_els = a.select("div.welfare")
        welfare = [w.get_text(strip=True) for w in welfare_els]
        education = next((w for w in welfare
                          if w in ("博士", "硕士", "本科", "大专", "学历不限")), None)
        experience = next((w for w in welfare if w != education), None)

        cn_el = a.select_one("div.companyName")
        company = cn_el.get_text(strip=True) if cn_el else "未公开"

        ind_el = a.select_one("div.industry")
        industry = ind_el.get_text(strip=True) if ind_el else None

        summary_parts = [p for p in [salary, experience, industry] if p]
        full_url = urljoin(_GDRC_BASE, href)

        out.append({
            "id": make_id("regional:gdrc", full_url),
            "source": "regional:gdrc",
            "source_label": "广东人才网",
            "title": title,
            "company": company,
            "location": location,
            "education": education,
            "experience": experience,
            "publish_time": None,
            "apply_url": full_url,
            "summary": " · ".join(summary_parts) if summary_parts else None,
            "fetched_at": BaseSource.now_iso(),
        })
        if len(out) >= limit:
            break
    return out


# ─── Bing 站内搜索兜底 ───
_BING_URL = "https://cn.bing.com/search?q={q}&count=20&FORM=PERE"

# 已被其他源 / agent 覆盖的平台，从 Bing 结果中过滤
_EXCLUDED_DOMAINS = {
    "zhipin.com", "zhaopin.com", "51job.com", "liepin.com",
    "sasac.gov.cn", "mohrss.gov.cn", "iguopin.com",
}


def _build_site_query(domains: list[str], keyword: str, city: str) -> str:
    """构造 Bing 站内搜索 query — 每批 ≤3 个域名保证 site: 生效"""
    site_parts = " OR ".join(f"site:{d}" for d in domains)
    parts = [f"({site_parts})", "招聘 2026"]
    if keyword.strip():
        parts.append(keyword.strip())
    if city.strip():
        parts.append(city.strip().replace("市", ""))
    return " ".join(parts)


def _parse_bing_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for li in soup.select("li.b_algo"):
        h = li.select_one("h2 a")
        if not h:
            continue
        url = (h.get("href") or "").strip()
        title = re.sub(r"\s+", " ", h.get_text(strip=True))
        if not url or not title or not url.startswith("http"):
            continue
        if "bing.com/aclk" in url:
            continue
        snippet = ""
        cap = li.select_one("div.b_caption p, p.b_lineclamp4, p.b_lineclamp3, p")
        if cap:
            snippet = re.sub(r"\s+", " ", cap.get_text(strip=True))
        out.append({"title": title, "url": url, "snippet": snippet})
    return out


def _domain_from_url(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).lstrip("www.") if m else ""


async def _bing_site_search(
    domains: list[str], keyword: str, city: str, limit: int,
) -> list[dict]:
    q = _build_site_query(domains, keyword, city)
    html = await fetch_text(
        _BING_URL.format(q=quote(q)),
        ttl=300.0,
        headers={
            "Accept": "text/html",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://cn.bing.com/",
        },
    )
    if not html:
        return []

    results = _parse_bing_results(html)
    site_name_map = {s["domain"].lstrip("www."): s["name"] for s in _SITES}

    out: list[dict] = []
    seen: set[str] = set()
    for r in results:
        sig = r["url"].split("#")[0]
        if sig in seen:
            continue
        seen.add(sig)

        domain = _domain_from_url(r["url"])
        if any(domain.endswith(ex) for ex in _EXCLUDED_DOMAINS):
            continue

        label = site_name_map.get(domain, domain)

        out.append({
            "id": make_id("regional", sig),
            "source": "regional",
            "source_label": label,
            "title": r["title"],
            "company": label,
            "location": city.strip() or None,
            "education": None,
            "experience": None,
            "publish_time": None,
            "apply_url": r["url"],
            "summary": r["snippet"][:200] if r["snippet"] else None,
            "fetched_at": BaseSource.now_iso(),
        })
        if len(out) >= limit:
            break
    return out


# ─── 聚合 Source ───

class RegionalTalentSource(BaseSource):
    key = "regional"
    display_name = "地方人才网"
    category = "地方人才"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        kw = keyword.strip()
        city_key = (city or "").replace("市", "").strip()

        tasks: list = []

        # GDRC 直接解析（有无关键词均可）
        tasks.append(_fetch_gdrc(limit, kw, city))

        # Bing 搜其他站点（有无关键词均触发）
        other_domains = [s["domain"] for s in _SITES
                        if s["domain"] != "www.gdrc.com"]
        if city_key:
            priority = [s["domain"] for s in _SITES
                        if city_key in s["city"]]
            rest = [d for d in other_domains if d not in priority]
            other_domains = priority + rest
        tasks.append(
            _bing_site_search(other_domains[:8], kw, city, limit)
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: list[dict] = []
        for res in results:
            if isinstance(res, Exception):
                print(f"  [地方人才网] 子任务异常: {res}", flush=True)
                continue
            merged.extend(res or [])

        # 按 source_label 去重（同 URL 不重复）
        seen: set[str] = set()
        deduped: list[dict] = []
        for item in merged:
            sig = item["apply_url"].split("#")[0]
            if sig not in seen:
                seen.add(sig)
                deduped.append(item)
        return deduped[:limit]
