"""国务院国资委 (SASAC) — 央企招聘公告抓取

栏目：http://www.sasac.gov.cn/n2588035/n2588325/n2588350/
  - 人事招聘列表（社招/校招/博士招混合），静态 HTML，UTF-8
  - 每条 li 里 <a title="..." href="../../../n.../c<id>/content.html">  <span>[YYYY-MM-DD]</span>
  - 分页 URL: index_20742332_<N>.html (N=2,3,...)
"""
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseSource, make_id
from ._http import fetch_text


_BASE = "http://www.sasac.gov.cn/n2588035/n2588325/n2588350/"
_PAGE1 = _BASE + "index.html"
_PAGE_N = _BASE + "index_20742332_{n}.html"
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _parse_list(html: str, base_url: str) -> list[dict]:
    """从 SASAC 栏目页 HTML 抽 list[{title, url, date}]"""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    container = soup.select_one("div.zsy_conlist ul") or soup
    for li in container.select("li"):
        a = li.find("a", attrs={"title": True})
        span = li.find("span")
        if not a or not span:
            continue
        date_txt = span.get_text(strip=True)
        if not date_txt:  # 置顶特例（红色专题导流），跳过
            continue
        m = _DATE_RE.search(date_txt)
        if not m:
            continue
        href = a.get("href") or ""
        if not href:
            continue
        title = (a.get("title") or a.get_text(strip=True)).strip()
        if not title:
            continue
        out.append({
            "title": title,
            "url": urljoin(base_url, href),
            "date": m.group(1),
        })
    return out


class SasacSource(BaseSource):
    key = "sasac"
    display_name = "国务院国资委"
    category = "公告类"
    bulletin = True  # 公告型：标题不含具体岗位词，跳过服务端关键词硬过滤

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        # bulletin 源：忽略 keyword（标题里几乎不会有岗位词，过滤会清零）
        # 用户在前端会被告知"未选岗位 / 公告源不过滤关键词"
        keyword = ""
        all_items: list[dict] = []
        pages_to_try = 3  # 公告列表页，最多翻 3 页
        for page in range(1, pages_to_try + 1):
            url = _PAGE1 if page == 1 else _PAGE_N.format(n=page)
            html = await fetch_text(url, ttl=180.0)
            if not html:
                break
            parsed = _parse_list(html, url)
            if not parsed:
                break
            all_items.extend(parsed)
            if len(all_items) >= limit * 3:
                break

        out: list[dict] = []
        kw = (keyword or "").strip()
        for it in all_items:
            blob = it["title"]
            if not self.match_keyword(blob, kw):
                continue
            # 公告无独立 location 字段，整篇正文里才有 — 跳过 city 过滤
            # （city 留给企业官网那一类有结构化 location 的源）
            company = self._extract_company(it["title"])
            iso_time = f"{it['date']}T00:00:00"
            out.append({
                "id": make_id(self.key, it["url"]),
                "source": self.key,
                "source_label": "国资委",
                "title": it["title"],
                "company": company or "央企",
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

    @staticmethod
    def _extract_company(title: str) -> Optional[str]:
        """从标题里抠公司名 — 标题通常以公司名开头，到「招聘/校招/启动」前。

        粗启发式：取 「招聘/招生/校招/社招/2026/启动」 之前的前缀。
        """
        m = re.match(r"^(.+?)(?=20\d{2}|招聘|招生|校招|社招|启动|公告|启事)", title)
        if m:
            cand = m.group(1).strip("，,、 ·-")
            if 2 <= len(cand) <= 24:
                return cand
        # fallback: 取前 14 个字
        s = title.strip()
        return s[:14] if len(s) > 2 else None
