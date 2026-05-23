"""网络检索源 — 通过搜索引擎 API 查找全网招聘信息

定位：当 5 个直连源（国资委/人社部/企业官网/公众号/国聘）覆盖不到的岗位，
靠搜索引擎兜底。query 模板：「{keyword} 招聘 {city} 2026 -site:zhipin.com -site:lagou.com」
排除掉的几个站点是因为本工具的"投递任务"那侧已经覆盖了。

引擎：Bing（cn.bing.com）静态 HTML 结果页可直接抓，无需 key，比百度反爬更轻。
仅在用户输入了 keyword 时启用 —— 没关键词时全网搜没意义。
"""
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from .base import BaseSource, make_id
from ._http import fetch_text


_BING_URL = "https://cn.bing.com/search?q={q}&count=20&first={first}&FORM=PERE"

# 关键词必带（保证结果是招聘相关）
_JOB_QUALIFIERS = "招聘 OR 校招 OR 社招 OR 实习"

# 我们已经在"投递任务"覆盖了的平台，从全网搜索中排除避免重复
_EXCLUDED_DOMAINS = [
    "zhipin.com", "lagou.com", "zhaopin.com",
    "51job.com", "liepin.com",
    "sasac.gov.cn", "mohrss.gov.cn", "iguopin.com",
    "gdrc.com", "shixiseng.com",
]


def _build_query(keyword: str, city: str) -> str:
    """关键词组合：招聘 + keyword + city + 年份。

    实测 Bing 中文站对 `intitle:` 等高级算符的支持很弱（往往直接忽略），
    但前缀放「招聘」+ 年份能稳稳进入招聘垂直域。
    """
    parts = ["招聘"]
    parts.append(keyword.strip())
    if city.strip():
        parts.append(city.strip().replace("市", ""))
    # 加年份过滤过期内容（含 2025/2026）
    parts.append(str(datetime.now().year))
    for d in _EXCLUDED_DOMAINS:
        parts.append(f"-site:{d}")
    return " ".join(parts)


def _strip_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_bing(html: str) -> list[dict]:
    """Bing 中文站结果结构：ol#b_results > li.b_algo
       每个 li：h2 > a (标题+链接), p / div.b_caption p (摘要)
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for li in soup.select("li.b_algo"):
        h = li.select_one("h2 a")
        if not h:
            continue
        url = (h.get("href") or "").strip()
        title = _strip_ws(h.get_text())
        if not url or not title:
            continue
        if not url.startswith("http"):
            continue
        snippet = ""
        cap = li.select_one("div.b_caption p, p.b_lineclamp4, p.b_lineclamp3, p")
        if cap:
            snippet = _strip_ws(cap.get_text())
        # 排除引擎缓存链接 / 广告
        if "bing.com/aclk" in url:
            continue
        out.append({
            "title": title,
            "url": url,
            "snippet": snippet,
        })
    return out


def _extract_domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    if not m:
        return ""
    host = m.group(1)
    # 去掉 www.
    if host.startswith("www."):
        host = host[4:]
    return host


_DATE_RE = re.compile(r"(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})")


def _extract_date_from_snippet(snippet: str) -> Optional[str]:
    m = _DATE_RE.search(snippet or "")
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 2020 <= y <= 2030:
            return f"{y:04d}-{mo:02d}-{d:02d}T00:00:00"
    except Exception:
        pass
    return None


class WebSearchSource(BaseSource):
    key = "websearch"
    display_name = "全网搜索"
    category = "联网检索"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        kw = keyword.strip()
        if not kw:
            # 没关键词时不发起，避免回 trending 不相关结果
            return []

        q = _build_query(kw, city)
        all_results: list[dict] = []

        # 翻 1-2 页（每页 ~10 条 Bing 算法卡片）
        for page in range(2):
            url = _BING_URL.format(q=quote(q), first=page * 10 + 1)
            html = await fetch_text(
                url,
                ttl=300.0,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": "https://cn.bing.com/",
                },
            )
            if not html:
                break
            page_results = _parse_bing(html)
            if not page_results:
                break
            all_results.extend(page_results)
            if len(all_results) >= limit * 2:
                break

        out: list[dict] = []
        seen: set[str] = set()
        for r in all_results:
            url = r["url"]
            # URL 去重
            sig = url.split("#")[0]
            if sig in seen:
                continue
            seen.add(sig)

            title = r["title"]
            snippet = r["snippet"]
            domain = _extract_domain(url)
            # Bing 的 -site: 算符不可靠，这里再做一次硬过滤把 zhipin/51job 等首页/聚合页砍掉
            if any(domain.endswith(d) for d in _EXCLUDED_DOMAINS):
                continue
            # 砍掉"域名首页"（无路径 / 路径就是 /index.html）—— 这种几乎都是搜索引擎兜底返回的招聘平台门户
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = (parsed.path or "/").rstrip("/")
            if not path or path in ("/index.html", "/index.htm", "/index.shtml", "/zhaopin"):
                continue
            # 标题里没出现 kw（或行业关键词）就跳过 —— 多半是搜索引擎相关性兜底的不相关结果
            kw_l = kw.lower()
            if kw_l and kw_l not in (title + snippet).lower():
                # 中文里也可能是别名，留个宽松通道：标题包含"招聘/校招/社招"就放行
                if not re.search(r"招聘|校招|社招|招考|公开招聘|岗位", title):
                    continue
            pub = _extract_date_from_snippet(snippet)

            # "公司"字段：用域名，让用户一眼看出来源
            company = domain or "网络结果"

            out.append({
                "id": make_id(self.key, sig),
                "source": self.key,
                "source_label": f"🔍 {domain}" if domain else "🔍 网络",
                "title": title,
                "company": company,
                "location": city.strip() or None,
                "education": None,
                "experience": None,
                "publish_time": pub,
                "apply_url": url,
                "summary": snippet[:200] if snippet else None,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
