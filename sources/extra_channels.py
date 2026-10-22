"""更多招聘渠道 — Bing 站内搜索聚合

覆盖图片中提到但未被其他源直接覆盖的站点：
  - 央企/国企：中智招聘、国家电网、中国烟草
  - 校园招聘：阿里巴巴校招、字节跳动校招、宝洁校招
  - 公务员/事业：国家公务员局、高校人才网
  - 行业垂直：北极星招聘(能源)、医药英才、中国汽车人才、银行招聘、中公金融
  - 综合平台：应届生求职网、海投网、牛客网、刺猬实习、拉勾网
  - 国际/外企：LinkedIn中国、Indeed、外企德科

策略：用 Bing site: 搜索批量查，需要关键词。
"""
import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from .base import BaseSource, make_id
from ._http import fetch_text


# ─── 站点目录（按分类） ───

_CHANNEL_SITES: list[dict] = [
    # 央企/国企
    {"domain": "www.cicscjob.com", "name": "中智招聘", "cat": "央企国企"},
    {"domain": "zhaopin.sgcc.com.cn", "name": "国家电网", "cat": "央企国企"},
    {"domain": "www.tobacco.gov.cn", "name": "中国烟草", "cat": "央企国企"},
    # 校园招聘
    {"domain": "campus.alibaba.com", "name": "阿里巴巴校招", "cat": "校园招聘"},
    {"domain": "jobs.bytedance.com", "name": "字节跳动校招", "cat": "校园招聘"},
    {"domain": "www.pgcareers.com", "name": "宝洁校招", "cat": "校园招聘"},
    {"domain": "join.qq.com", "name": "腾讯校招", "cat": "校园招聘"},
    {"domain": "career.huawei.com", "name": "华为校招", "cat": "校园招聘"},
    # 公务员/事业单位
    {"domain": "www.scs.gov.cn", "name": "国家公务员局", "cat": "公务员"},
    {"domain": "www.gaoxiaojob.com", "name": "高校人才网", "cat": "事业单位"},
    # 行业垂直
    {"domain": "hr.bjx.com.cn", "name": "北极星招聘", "cat": "能源"},
    {"domain": "www.healthr.com", "name": "医药英才网", "cat": "医药"},
    {"domain": "www.carjob.com.cn", "name": "中国汽车人才网", "cat": "汽车"},
    {"domain": "www.yinhangzhaopin.com", "name": "银行招聘网", "cat": "金融"},
    {"domain": "m.jinrongren.net", "name": "中公金融人", "cat": "金融"},
    # 综合平台
    {"domain": "www.yingjiesheng.com", "name": "应届生求职网", "cat": "综合平台"},
    {"domain": "www.haitou.cc", "name": "海投网", "cat": "综合平台"},
    {"domain": "www.nowcoder.com", "name": "牛客网", "cat": "综合平台"},
    {"domain": "www.ciwei.net", "name": "刺猬实习", "cat": "综合平台"},
    {"domain": "www.lagou.com", "name": "拉勾网", "cat": "综合平台"},
    {"domain": "www.saikr.com", "name": "赛氪网", "cat": "竞赛内推"},
    # 国际/外企
    {"domain": "www.linkedin.cn", "name": "领英中国", "cat": "国际外企"},
    {"domain": "cn.indeed.com", "name": "Indeed中国", "cat": "国际外企"},
    {"domain": "www.fescoadecco.com", "name": "外企德科", "cat": "国际外企"},
]

_SITE_NAME_MAP = {s["domain"].removeprefix("www."): s["name"] for s in _CHANNEL_SITES}

_BING_URL = "https://cn.bing.com/search?q={q}&count=20&first={first}&FORM=PERE"


def _build_query(domains: list[str], keyword: str, city: str) -> str:
    site_clause = " OR ".join(f"site:{d}" for d in domains)
    parts = [f"({site_clause})", f"招聘 {datetime.now().year}"]
    if keyword.strip():
        parts.append(keyword.strip())
    if city.strip():
        parts.append(city.strip().replace("市", ""))
    return " ".join(parts)


def _parse_bing(html: str) -> list[dict]:
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
    return m.group(1).removeprefix("www.") if m else ""


# Bing site: 约束不可靠，结果可能包含其他站点 — 把已覆盖平台硬过滤掉
_EXCLUDED_DOMAINS = {
    "zhipin.com", "zhaopin.com", "51job.com", "liepin.com",
    "sasac.gov.cn", "mohrss.gov.cn", "iguopin.com",
}


class ExtraChannelSource(BaseSource):
    key = "extra"
    display_name = "更多渠道"
    category = "更多渠道"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        kw = keyword.strip()

        all_domains = [s["domain"] for s in _CHANNEL_SITES]
        batch_size = 3
        all_results: list[dict] = []

        for i in range(0, len(all_domains), batch_size):
            batch = all_domains[i:i + batch_size]
            q = _build_query(batch, kw, city)
            html = await fetch_text(
                _BING_URL.format(q=quote(q), first=1),
                ttl=300.0,
                headers={
                    "Accept": "text/html",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://cn.bing.com/",
                },
            )
            if html:
                all_results.extend(_parse_bing(html))
            if len(all_results) >= limit * 3:
                break

        city_txt = city.strip() or None
        out: list[dict] = []
        seen: set[str] = set()
        for r in all_results:
            sig = r["url"].split("#")[0]
            if sig in seen:
                continue
            seen.add(sig)

            domain = _domain_from_url(r["url"])
            if any(domain.endswith(ex) for ex in _EXCLUDED_DOMAINS):
                continue

            label = _SITE_NAME_MAP.get(domain, domain)

            out.append({
                "id": make_id(self.key, sig),
                "source": self.key,
                "source_label": label,
                "title": r["title"],
                "company": label,
                "location": city_txt,
                "education": None,
                "experience": None,
                "publish_time": None,
                "apply_url": r["url"],
                "summary": r["snippet"][:200] if r["snippet"] else None,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
