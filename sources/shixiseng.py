"""实习僧 (shixiseng.com) — JSON API 抓取

接口：GET https://www.shixiseng.com/app/interns
  参数：keyword, page, type=intern, city=全国, salary=-1, degree=-1
  响应：{"msg": [...jobs...], "code": 200}
  每条：city, maxsal, minsal, cname, name(职位), url(logo), refresh, uuid, ftype, day

详情 URL：https://www.shixiseng.com/intern/{uuid}
"""
from datetime import datetime
from typing import Optional

from .base import BaseSource, make_id
from ._http import fetch_json


_API_URL = "https://www.shixiseng.com/app/interns"
_DETAIL_URL = "https://www.shixiseng.com/intern/{uuid}"


def _fmt_salary(minsal, maxsal) -> Optional[str]:
    mn = int(minsal or 0)
    mx = int(maxsal or 0)
    if not mn and not mx:
        return None
    if mn and mx and mn != mx:
        return f"{mn}-{mx}/天"
    v = mn or mx
    return f"{v}/天"


class ShixisengSource(BaseSource):
    key = "shixiseng"
    display_name = "实习僧"
    category = "求职平台"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        params = {
            "keyword": keyword.strip(),
            "page": 1,
            "type": "intern",
            "city": city.strip() or "全国",
            "salary": -1,
            "degree": -1,
        }
        data = await fetch_json(
            _API_URL,
            params=params,
            ttl=180.0,
        )
        if not data or not isinstance(data, dict):
            return []

        items = data.get("msg")
        if not isinstance(items, list):
            return []

        city_key = (city or "").replace("市", "").strip()
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            uuid = (it.get("uuid") or "").strip()
            title = (it.get("name") or "").strip()
            if not uuid or not title:
                continue

            company = (it.get("cname") or "").strip() or "未公开"
            loc = (it.get("city") or "").strip() or None
            if city_key and not self.match_city(loc, city_key):
                continue

            salary = _fmt_salary(it.get("minsal"), it.get("maxsal"))
            refresh = (it.get("refresh") or "").strip()
            day = it.get("day")
            pub = None
            if refresh:
                pub = f"{datetime.now().year}-{refresh}T00:00:00" if len(refresh) <= 5 else refresh

            summary_parts = []
            if salary:
                summary_parts.append(salary)
            if day:
                summary_parts.append(f"每周{day}天")
            ftype = it.get("ftype")
            if ftype == "1":
                summary_parts.append("实习")

            out.append({
                "id": make_id(self.key, uuid),
                "source": self.key,
                "source_label": "实习僧",
                "title": title,
                "company": company,
                "location": loc,
                "education": None,
                "experience": "实习",
                "publish_time": pub,
                "apply_url": _DETAIL_URL.format(uuid=uuid),
                "summary": " · ".join(summary_parts) if summary_parts else None,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
