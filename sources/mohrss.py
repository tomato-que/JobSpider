"""人社部 (MOHRSS) — 招聘公告抓取

主源 A：中央和国家机关事业单位公开招聘服务平台 → 招聘公告
  http://www.mohrss.gov.cn/SYrlzyhshbzb/fwyd/SYkaoshizhaopin/zyhgjjgsydwgkzp/zpgg/
  - 站点常年挂着 EO_Bot JS 挑战（~985B 短页），纯 httpx 几乎必中。
  - 实测 Playwright（带 stealth 注入 + 等 cookie 注入 + 等容器渲染）稳定通过。
  - 所以现在直接走 Playwright，跳过 HTTP 一次性失败的开销。

关键词模式：站内 WAS 全文搜索
  /was5/web/search?searchword=XXX&channelid=265899&orderby=-docreltime&perpage=10&page=N
  - channelid=265899 = 招聘公告频道
  - 响应也是 HTML 片段，li.tem_li 列表
"""
import re
from typing import Optional
from urllib.parse import urljoin, quote

from bs4 import BeautifulSoup

from .base import BaseSource, make_id
from ._browser import get_page_html


_BASE_LIST = (
    "https://www.mohrss.gov.cn/SYrlzyhshbzb/fwyd/"
    "SYkaoshizhaopin/zyhgjjgsydwgkzp/zpgg/"
)
_PAGE1 = _BASE_LIST + "index.html"
_PAGE_N = _BASE_LIST + "index_{n}.html"

_SEARCH_URL = (
    "https://www.mohrss.gov.cn/was5/web/search"
    "?searchword={kw}&channelid=265899&orderby=-docreltime"
    "&perpage=20&page={page}"
)

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

_WAIT_SELECTOR = "ul.rsb_ej_zpggList li, ul.rsb_con_rightUl li, li.tem_li"


def _is_challenge(html: str) -> bool:
    """识别 mohrss 的 EO_Bot JS 挑战页（很短，含 _0x649a / __tst_status）。"""
    if not html or len(html) > 4000:
        return False
    return ("__tst_status" in html or "_0x649a" in html
            or "EO_Bot_Ssid" in html)


def _parse_list(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    container = (
        soup.select_one("ul.rsb_ej_zpggList")
        or soup.select_one("ul.rsb_con_rightUl")
        or soup
    )
    for li in container.select("li"):
        a = li.find("a")
        if not a:
            continue
        href = a.get("href") or ""
        if not href or href.startswith("#"):
            continue
        title = (a.get("title") or a.get_text(strip=True)).strip()
        title = re.sub(r"\s+", " ", title)
        if not title or len(title) < 4:
            continue
        date_txt = ""
        span = li.find("span")
        if span:
            date_txt = span.get_text(strip=True)
        if not date_txt:
            date_txt = li.get_text(" ", strip=True)
        m = _DATE_RE.search(date_txt)
        if not m:
            continue
        out.append({
            "title": title,
            "url": urljoin(base_url, href),
            "date": m.group(1),
        })
    return out


def _extract_employer(title: str) -> Optional[str]:
    """从公告标题里抠招聘单位 — 通常形如「XXX 2026年度公开招聘公告」。"""
    m = re.match(
        r"^(.+?)(?=20\d{2}|公开招聘|招聘公告|招聘公示|拟聘用|公开遴选|招考)",
        title,
    )
    if m:
        cand = m.group(1).strip("，,、 ·-")
        if 2 <= len(cand) <= 30:
            return cand
    s = title.strip()
    return s[:14] if len(s) > 2 else None


async def _browser_fetch(url: str) -> Optional[str]:
    """走 Playwright 拿渲染后的 HTML；命中挑战页时多等一轮 cookie 注入。"""
    html = await get_page_html(
        url,
        wait_selector=_WAIT_SELECTOR,
        timeout=25.0,
        extra_wait_ms=2500,
    )
    if html and _is_challenge(html):
        html = await get_page_html(
            url,
            wait_selector=_WAIT_SELECTOR,
            timeout=25.0,
            extra_wait_ms=5000,
        )
    if not html or _is_challenge(html):
        return None
    return html


class MohrssSource(BaseSource):
    key = "mohrss"
    display_name = "中国人社部"
    category = "公告类"
    bulletin = True  # 公告型：标题不含具体岗位词，跳过服务端关键词硬过滤

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        # bulletin 源：忽略 keyword（公告标题里几乎不会有岗位词，过滤会清零）
        all_items: list[dict] = []
        kw = ""

        # 直接走 Playwright（mohrss 常年 EO_Bot 挑战，HTTP 几乎必中，不值得试）
        urls = [_PAGE1, _PAGE_N.format(n=2)]

        for url in urls:
            html = await _browser_fetch(url)
            if not html:
                continue
            parsed = _parse_list(html, url)
            if not parsed:
                continue
            all_items.extend(parsed)
            if len(all_items) >= limit * 2:
                break

        if not all_items:
            raise RuntimeError(
                "人社部抓取失败（Playwright 也未拿到列表，可能反爬升级或网络异常）",
            )

        out: list[dict] = []
        seen: set[str] = set()
        for it in all_items:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            if not self.match_keyword(it["title"], kw):
                continue
            company = _extract_employer(it["title"]) or "事业单位"
            iso_time = f"{it['date']}T00:00:00"
            out.append({
                "id": make_id(self.key, it["url"]),
                "source": self.key,
                "source_label": "人社部",
                "title": it["title"],
                "company": company,
                "location": None,
                "education": None,
                "experience": None,
                "publish_time": iso_time,
                "apply_url": it["url"],
                "summary": None,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
