"""公开岗位源 — 抽象基类

每个 Source 类对应一个数据来源（国资委/人社部/字节官网/公众号/国聘…），
统一返回字段：
  id, source, title, company, location, education, experience,
  publish_time, apply_url, summary, fetched_at
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional


def make_id(source_key: str, *parts: str) -> str:
    """根据 source + 业务唯一字段（URL / postId / title）生成稳定 id"""
    h = hashlib.md5()
    h.update(source_key.encode("utf-8"))
    for p in parts:
        h.update(b"|")
        h.update((p or "").encode("utf-8"))
    return f"{source_key}:{h.hexdigest()[:16]}"


_EDU_PATS = [
    (r"博士", "博士"), (r"硕士|研究生", "硕士"),
    (r"本科", "本科"), (r"大专|专科", "大专"),
]


def parse_education(text: str) -> Optional[str]:
    if not text:
        return None
    for pat, lbl in _EDU_PATS:
        if re.search(pat, text):
            return lbl
    return None


def parse_experience(text: str) -> Optional[str]:
    if not text:
        return None
    if re.search(r"应届|无经验|不限.*经验", text):
        return "应届"
    if re.search(r"实习", text):
        return "实习"
    m = re.search(r"(\d+)\s*[-~到至]\s*(\d+)\s*年", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}年"
    m = re.search(r"(\d+)\s*年以上", text)
    if m:
        return f"{m.group(1)}年+"
    m = re.search(r"(\d+)\s*年", text)
    if m:
        return f"{m.group(1)}年"
    return None


class BaseSource:
    """所有公开岗位源的基类。

    子类至少要实现 async fetch(limit, keyword, city) → list[dict]。
    每条记录推荐结构：
      {
        "id": str, "source": str, "title": str, "company": str,
        "location": str|None, "education": str|None, "experience": str|None,
        "publish_time": str|None,  # ISO 字符串
        "apply_url": str, "summary": str|None,
        "fetched_at": str,
      }
    """
    key: str = ""              # 内部 key，e.g. "company:bytedance"
    display_name: str = ""     # 前端显示名，e.g. "字节跳动"
    category: str = ""         # 分类标签 e.g. "企业官网" / "国家发布" / "求职平台"
    enabled: bool = True       # 默认是否启用
    # bulletin=True: 公告型源 — 标题是 "XX单位2026招聘公告" 格式，不含具体岗位词，
    #                所以服务端「关键词硬过滤」时跳过此类源，避免被全部砍光。
    #                同时前端默认不抓取此类源，需用户主动勾选。
    bulletin: bool = False

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        raise NotImplementedError

    # ────── 通用过滤辅助 ──────

    @staticmethod
    def match_city(location: Optional[str], city: str) -> bool:
        if not city:
            return True
        if not location:
            return False
        needle = city.replace("市", "").strip()
        return needle in location

    @staticmethod
    def match_keyword(blob: str, keyword: str) -> bool:
        if not keyword:
            return True
        if not blob:
            return False
        return keyword.lower() in blob.lower()

    @staticmethod
    def now_iso() -> str:
        return datetime.now().isoformat(timespec="seconds")
