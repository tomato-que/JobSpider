"""滴滴 (talent.didiglobal.com) — JSON API 抓取

接口：GET https://talent.didiglobal.com/recruit-portal-service/api/job/front/list
  query 参数:
    pageNum=1            (实测：pageSize/pageNum 都被服务端忽略，每页固定 16 条)
    pageSize=16          (留着，不影响)
    jobName=<keyword>    (服务端支持关键词过滤；不传则返全部)
    jobType=<1..5>       (1=技术 / 2=设计 / 3=产品 / 4=运营 / 5=职能等。
                          不传 jobType 时 total=1267 但 items 还是 16 条。
                          为了让默认抓取能多拿几条，我们顺序遍历 jobType 1..6 聚合。)

详情页：https://talent.didiglobal.com/social/list/1?jobId={jdId}
  滴滴前端 SPA 路由，直接拼上 jobId 就能进详情；也可走 jobDetail/{jdId}。

响应：
  {
    "meta": {"code":0, ...},
    "data": {
      "total": 1267,
      "items": [
        {"jdId": 60365, "jdNo":"J251208004",
         "workArea":"北京市", "deptName":"花小猪打车",
         "jobName":"经营分析师 (J251208004)",
         "refreshTime":"2026-05-21 21:10:19",
         "jobType": 4, ...},
        ...
      ]
    }
  }
"""
from __future__ import annotations

from typing import Optional

from .base import BaseSource, make_id
from ._http import fetch_json


_API = (
    "https://talent.didiglobal.com/recruit-portal-service/"
    "api/job/front/list"
)
_DETAIL_TMPL = "https://talent.didiglobal.com/social/list/1?jobId={jd_id}"
_REFERER = "https://talent.didiglobal.com/social/list/1"

# jobType 顺序遍历 — 每个 type 返回 16 条独立数据
_JOB_TYPES = [1, 2, 3, 4, 5, 6]


def _iso_time(s: Optional[str]) -> Optional[str]:
    """滴滴 refreshTime 形如 '2026-05-21 21:10:19'，转 ISO。"""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if " " in s:
        return s.replace(" ", "T")
    return s


class DidiSource(BaseSource):
    key = "didi"
    display_name = "滴滴"
    category = "企业官网"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        kw = (keyword or "").strip()
        city_key = (city or "").strip().replace("市", "")

        collected: list[dict] = []
        seen_ids: set[int] = set()

        async def _grab(params: dict) -> None:
            data = await fetch_json(
                _API,
                params=params,
                headers={"Referer": _REFERER},
                ttl=180.0,
            )
            if not data or not isinstance(data, dict):
                return
            body = data.get("data") or {}
            items = body.get("items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                jd_id = it.get("jdId")
                if jd_id is None or jd_id in seen_ids:
                    continue
                seen_ids.add(jd_id)
                collected.append(it)

        # 有关键词时只发一次（服务端按 jobName 过滤）
        if kw:
            await _grab({"pageNum": 1, "pageSize": 50, "jobName": kw})
        else:
            # 默认：先不带 jobType 抓一次，再按 jobType 各刷一遍直到够 limit*2
            await _grab({"pageNum": 1, "pageSize": 50})
            for jt in _JOB_TYPES:
                if len(collected) >= limit * 2:
                    break
                await _grab({"pageNum": 1, "pageSize": 50, "jobType": jt})

        out: list[dict] = []
        for it in collected:
            jd_id = it.get("jdId")
            title = (it.get("jobName") or "").strip()
            if not title:
                continue
            # 标题里 jobName 通常带 " (JR...)" 尾部 jdNo 后缀，给个清理
            jd_no = (it.get("jdNo") or "").strip()
            if jd_no and title.endswith(f"({jd_no})"):
                title = title[:-(len(jd_no) + 2)].strip().rstrip("(").strip()

            location = (it.get("workArea") or "").strip() or None
            if city_key and not self.match_city(location, city_key):
                continue
            # 关键词模式下 server 已过滤；但仍做一次轻防御
            if kw and not self.match_keyword(f"{title} {it.get('deptName') or ''}", kw):
                continue

            dept = (it.get("deptName") or "").strip()
            company = f"滴滴 · {dept}" if dept else "滴滴"
            pub = _iso_time(it.get("refreshTime") or it.get("createTime"))

            summary_parts: list[str] = []
            label_name = (it.get("labelName") or "").strip()
            if label_name:
                summary_parts.append(label_name)
            job_type_name = (it.get("jobTypeName") or "").strip()
            if job_type_name:
                summary_parts.append(job_type_name)
            if it.get("isUrgent"):
                summary_parts.append("急聘")
            summary = " · ".join(summary_parts) if summary_parts else None

            out.append({
                "id": make_id(self.key, str(jd_id)),
                "source": self.key,
                "source_label": "滴滴",
                "title": title,
                "company": company,
                "location": location,
                "education": None,  # 列表接口未给学历
                "experience": None,
                "publish_time": pub,
                "apply_url": _DETAIL_TMPL.format(jd_id=jd_id),
                "summary": summary,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
