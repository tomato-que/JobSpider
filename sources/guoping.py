"""国聘网 (iguopin.com) — JSON API 抓取

接口：POST https://gp-api.iguopin.com/api/jobs/v1/recom-job
  Headers:
    Content-Type: application/json
    Origin: https://www.iguopin.com    (不带 total 极少，带上 total ~ 真实数量)
    Referer: https://www.iguopin.com/
  Body:
    {
      "search": {
        "page": 1,
        "page_size": 20,
        "keyword": "Python",
        "district": ["000000.110000"]    (可选，层级 area_code)
      },
      "recom": {}
    }

响应 (data.list[N])：
  job_id, job_name, company_name, contents
  min_wage / max_wage / is_negotiable
  education_cn / experience_cn / nature_cn / recruitment_type_cn
  district_list[].area_cn
  start_time / refresh_time

详情 URL：https://www.iguopin.com/job/detail?id={job_id}
"""
import re
from datetime import datetime
from typing import Optional

from .base import BaseSource, make_id
from ._http import fetch_json


_API_URL = "https://gp-api.iguopin.com/api/jobs/v1/recom-job"
_DETAIL_URL = "https://www.iguopin.com/job/detail?id={id}"


# 城市名 → iguopin area_code（层级码，前 6 位 000000 是国家）
# 仅覆盖主要城市，未命中则不过滤
_CITY_CODE: dict[str, str] = {
    "北京": "000000.110000",
    "上海": "000000.310000",
    "天津": "000000.120000",
    "重庆": "000000.500000",
    "广州": "000000.440000.440100",
    "深圳": "000000.440000.440300",
    "杭州": "000000.330000.330100",
    "成都": "000000.510000.510100",
    "武汉": "000000.420000.420100",
    "南京": "000000.320000.320100",
    "西安": "000000.610000.610100",
    "苏州": "000000.320000.320500",
    "长沙": "000000.430000.430100",
    "郑州": "000000.410000.410100",
    "济南": "000000.370000.370100",
    "青岛": "000000.370000.370200",
    "厦门": "000000.350000.350200",
    "合肥": "000000.340000.340100",
    "宁波": "000000.330000.330200",
    "无锡": "000000.320000.320200",
}


def _fmt_salary(it: dict) -> Optional[str]:
    if it.get("is_negotiable"):
        return "面议"
    mn = it.get("min_wage") or 0
    mx = it.get("max_wage") or 0
    if not mn and not mx:
        return None
    unit = it.get("wage_unit_cn") or "元/月"
    if mn and mx and mn != mx:
        return f"{mn//1000}-{mx//1000}K" if mn >= 1000 else f"{mn}-{mx}{unit}"
    v = mn or mx
    return f"{v//1000}K" if v >= 1000 else f"{v}{unit}"


def _iso_time(ts) -> Optional[str]:
    """iguopin 字段可能是字符串 "YYYY-MM-DD HH:MM:SS" 或 unix 秒"""
    if not ts:
        return None
    # 字符串："2026-05-21 17:46:45" 之类
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # 标准化成 ISO："YYYY-MM-DDTHH:MM:SS"
        if " " in s:
            return s.replace(" ", "T")
        return s
    # 整数：unix 秒
    try:
        return datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
    except Exception:
        return None


class GuopingSource(BaseSource):
    key = "guoping"
    display_name = "国聘网"
    category = "求职平台"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        search: dict = {
            "page": 1,
            "page_size": max(limit, 20),
            "keyword": keyword.strip(),
        }
        city_key = (city or "").strip().replace("市", "")
        if city_key and city_key in _CITY_CODE:
            search["district"] = [_CITY_CODE[city_key]]

        headers = {
            "Origin": "https://www.iguopin.com",
            "Referer": "https://www.iguopin.com/",
            "Content-Type": "application/json",
        }

        data = await fetch_json(
            _API_URL,
            method="POST",
            json_body={"search": search, "recom": {}},
            headers=headers,
            ttl=180.0,
        )
        if not data or not isinstance(data, dict):
            return []
        body = data.get("data") or {}
        items = body.get("list") or []
        if not items:
            return []

        # 国聘的 keyword 匹配偶尔会发散（命中 contents 里的某个词），加一层保险：
        # 有 keyword 时强制要求 title / contents / company 至少命中一个 token。
        kw_terms = [t for t in (keyword or "").lower().split() if t]

        out: list[dict] = []
        for it in items:
            job_id = it.get("job_id")
            title = (it.get("job_name") or "").strip()
            if not job_id or not title:
                continue
            company = (it.get("company_name") or "").strip() or "未公开"
            if kw_terms:
                blob = f"{title} {company} {it.get('contents') or ''}".lower()
                if not any(t in blob for t in kw_terms):
                    continue

            # district_list 通常是 list[dict]，挑第一个的 area_cn
            location = None
            dl = it.get("district_list") or []
            if dl and isinstance(dl, list):
                first = dl[0]
                if isinstance(first, dict):
                    location = first.get("area_cn") or first.get("area_name")
            # 客户端兜底 city 过滤（API 已按 district 过滤过一次，这里只做保险）
            if city_key and not self.match_city(location, city_key):
                continue

            salary = _fmt_salary(it)
            edu = (it.get("education_cn") or "").strip() or None
            exp = (it.get("experience_cn") or "").strip() or None
            pub = _iso_time(
                it.get("refresh_time") or it.get("update_time")
                or it.get("create_time") or it.get("start_time")
            )
            deadline = _iso_time(it.get("end_time"))

            summary_parts = []
            if salary:
                summary_parts.append(salary)
            rt = (it.get("recruitment_type_cn") or "").strip()
            if rt:
                summary_parts.append(rt)
            nature = (it.get("nature_cn") or "").strip()
            if nature:
                summary_parts.append(nature)
            # 正文也放进 summary：服务端关键词二次过滤时不会把正文命中的岗位误杀
            contents = it.get("contents") or it.get("content") or ""
            if isinstance(contents, str) and contents.strip():
                summary_parts.append(
                    re.sub(r"\s+", " ", contents).strip()[:160])
            summary = " · ".join(summary_parts) if summary_parts else None

            apply_url = _DETAIL_URL.format(id=job_id)
            out.append({
                "id": make_id(self.key, str(job_id)),
                "source": self.key,
                "source_label": "国聘网",
                "title": title,
                "company": company,
                "location": location,
                "education": edu,
                "experience": exp,
                "publish_time": pub,
                "deadline": deadline,
                "apply_url": apply_url,
                "summary": summary,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
