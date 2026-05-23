"""京东招聘 (zhaopin.jd.com) — POST form 接口

接口：POST https://zhaopin.jd.com/web/job/job_list
  Content-Type: application/x-www-form-urlencoded
  body:
    type=2          (1=校招 / 2=社招, 也可能是其他枚举；用 2 拿社招)
    page=1
    pageSize=20
    keyword=<可选>

响应：直接是 list[obj]，没有外层包裹：
  [
    {
      "id": 163394,
      "positionId": 217946,
      "positionCode": "00878127",
      "positionName": "销售岗",
      "positionDeptName": "京东健康",
      "workCity": "北京市",
      "jobType": "运营类",
      "publishTime": 1779379200000,
      "formatPublishTime": "2026-05-22",
      "qualification": "...",
      "workContent": "...",
      ...
    }, ...
  ]

详情页：https://zhaopin.jd.com/postdetail?reqNumber={reqNumber}&postCode={positionCode}
  （也可走 #/jobDetail/{positionId}，前端 SPA 路由；用 reqNumber 更稳）
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import httpx

from .base import BaseSource, make_id, parse_education, parse_experience


_API = "https://zhaopin.jd.com/web/job/job_list"
_DETAIL_TMPL = (
    "https://zhaopin.jd.com/postdetail?"
    "reqNumber={req_number}&postCode={post_code}"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _iso_ts(ms) -> Optional[str]:
    """JD publishTime 是毫秒时间戳。"""
    if not ms or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
    except Exception:
        return None


class JdSource(BaseSource):
    key = "jd"
    display_name = "京东招聘"
    category = "企业官网"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        kw = (keyword or "").strip()
        city_key = (city or "").strip().replace("市", "")
        # JD 每页固定 ~20 条；想拿 limit 条最多翻 3 页够用。
        page_size = max(min(limit, 30), 20)
        pages_needed = max((limit // page_size) + 1, 2)

        collected: list[dict] = []
        seen_ids: set[int] = set()

        headers = {
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://zhaopin.jd.com/web/job/job_list_page",
        }

        async with httpx.AsyncClient(
            timeout=15.0, headers=headers,
            follow_redirects=True, trust_env=False,
        ) as cli:
            for page in range(1, pages_needed + 1):
                form = {
                    "type": "2",      # 社招
                    "page": str(page),
                    "pageSize": str(page_size),
                }
                if kw:
                    form["keyword"] = kw
                try:
                    r = await cli.post(_API, data=form)
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    print(f"  [jd] POST page={page}: "
                          f"{type(e).__name__}: {e}", flush=True)
                    break
                if not isinstance(data, list) or not data:
                    break
                added = 0
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    pid = it.get("id")
                    if pid is None or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    collected.append(it)
                    added += 1
                if added == 0:
                    break
                if len(collected) >= limit * 2:
                    break

        out: list[dict] = []
        for it in collected:
            pid = it.get("id")
            title = (it.get("positionName") or "").strip()
            if not pid or not title:
                continue

            location = (it.get("workCity") or "").strip() or None
            if city_key and not self.match_city(location, city_key):
                continue

            dept = (it.get("positionDeptName") or "").strip()
            company = f"京东 · {dept}" if dept else "京东"

            qual = it.get("qualification") or ""
            wc = it.get("workContent") or ""
            edu = parse_education(qual) or parse_education(wc)
            exp = parse_experience(qual) or parse_experience(wc)

            pub = _iso_ts(it.get("publishTime"))
            if not pub:
                fp = (it.get("formatPublishTime") or "").strip()
                if fp:
                    pub = f"{fp}T00:00:00"

            req_no = (it.get("reqNumber") or "").strip()
            post_code = (it.get("positionCode") or "").strip()
            if req_no and post_code:
                apply_url = _DETAIL_TMPL.format(
                    req_number=req_no, post_code=post_code)
            else:
                apply_url = f"https://zhaopin.jd.com/postdetail?id={pid}"

            summary_parts: list[str] = []
            jt = (it.get("jobType") or "").strip()
            if jt:
                summary_parts.append(jt)
            if wc:
                w = wc.replace("\n", " ").replace("\r", " ").strip()
                if len(w) > 100:
                    w = w[:100] + "…"
                summary_parts.append(w)
            summary = " · ".join(summary_parts)[:160] if summary_parts else None

            out.append({
                "id": make_id(self.key, str(pid)),
                "source": self.key,
                "source_label": "京东",
                "title": title,
                "company": company,
                "location": location,
                "education": edu,
                "experience": exp,
                "publish_time": pub,
                "apply_url": apply_url,
                "summary": summary,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
