"""微信公众号 — 通过 RSSHub 转 RSS 抓取

⚠️ 现实情况（2026-05 调研）：
  - 公开 rsshub.app 实例对微信路由长期不稳，多数 wechat 子路由（ce/ershicimi/
    feeddd）所依赖的第三方镜像站已下线，唯一存活的 wechat2rss 收录池里几乎没有
    招聘类公众号。
  - 因此实用路线是：用户自建 RSSHub + Wechat2RSS 私部署，自行加目标公众号，
    然后把 feed URL 配进本源。

接入方式：
  环境变量 JOBSPIDER_WECHAT_FEEDS = "url1|name1, url2|name2, ..."
    例：
    JOBSPIDER_WECHAT_FEEDS="https://rss.mydomain.com/wechat/wechat2rss/abc|央国企招聘,https://rss.mydomain.com/wechat/wechat2rss/def|应届生求职"

  也可放在同目录 wechat_feeds.txt（每行 "url|公众号名"，# 开头为注释）。

  feed 必须是标准 RSS 2.0（RSSHub 输出即可）：
    rss > channel > item > {title, link, pubDate, description}
"""
import os
import re
from pathlib import Path
from typing import Optional
from email.utils import parsedate_to_datetime

import feedparser

from .base import BaseSource, make_id
from ._http import fetch_text


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _load_feeds() -> list[tuple[str, str]]:
    """返回 [(url, account_name), ...]，按 env > file > 空 优先级。"""
    out: list[tuple[str, str]] = []

    env = os.environ.get("JOBSPIDER_WECHAT_FEEDS", "").strip()
    if env:
        for raw in env.split(","):
            raw = raw.strip()
            if not raw:
                continue
            if "|" in raw:
                url, name = raw.split("|", 1)
            else:
                url, name = raw, "公众号"
            out.append((url.strip(), name.strip()))

    # 文件配置
    cfg = Path(__file__).parent / "wechat_feeds.txt"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                url, name = line.split("|", 1)
            else:
                url, name = line, "公众号"
            out.append((url.strip(), name.strip()))

    # 去重保序
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for url, name in out:
        if url in seen:
            continue
        seen.add(url)
        uniq.append((url, name))
    return uniq


class WechatSource(BaseSource):
    key = "wechat"
    display_name = "微信公众号"
    category = "官方发布"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        feeds = _load_feeds()
        if not feeds:
            print("  [微信公众号] 未配置 feed — 请设置 JOBSPIDER_WECHAT_FEEDS "
                  "环境变量或 sources/wechat_feeds.txt", flush=True)
            return []

        all_items: list[dict] = []
        kw = keyword.strip()

        for feed_url, account in feeds:
            text = await fetch_text(feed_url, ttl=300.0)
            if not text:
                continue
            try:
                parsed = feedparser.parse(text)
            except Exception as e:
                print(f"  [微信公众号] 解析 {account} 失败: {e}", flush=True)
                continue
            entries = parsed.get("entries") or []
            for entry in entries:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue
                if not self.match_keyword(title, kw):
                    # 公众号文章正文可能命中 — 也试试 summary
                    summary_text = _strip_html(
                        entry.get("summary") or entry.get("description") or "",
                    )
                    if not self.match_keyword(summary_text, kw):
                        continue

                # 发布时间
                pub = None
                pub_raw = entry.get("published") or entry.get("updated")
                if pub_raw:
                    try:
                        pub = parsedate_to_datetime(pub_raw).isoformat(
                            timespec="seconds",
                        )
                    except Exception:
                        pub = pub_raw

                summary = _strip_html(
                    entry.get("summary") or entry.get("description") or "",
                )
                if len(summary) > 160:
                    summary = summary[:157] + "…"

                all_items.append({
                    "id": make_id(self.key, link),
                    "source": self.key,
                    "source_label": account or "公众号",
                    "title": title,
                    "company": account or "公众号",
                    "location": None,
                    "education": None,
                    "experience": None,
                    "publish_time": pub,
                    "apply_url": link,
                    "summary": summary or None,
                    "fetched_at": self.now_iso(),
                })
                if len(all_items) >= limit * 2:
                    break
            if len(all_items) >= limit * 2:
                break

        # 按发布时间倒序（无时间的排末尾）
        all_items.sort(
            key=lambda x: x.get("publish_time") or "",
            reverse=True,
        )
        return all_items[:limit]
