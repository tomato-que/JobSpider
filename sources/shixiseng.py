"""实习僧 (shixiseng.com) — JSON API 抓取

接口：GET https://www.shixiseng.com/app/interns
  参数：keyword, page, type=intern, city=全国, salary=-1, degree=-1
  响应：{"msg": [...jobs...], "code": 100}
  实测（2026-10）：
    - 成功 code=100 且 msg 为 list；参数缺失/越界时 code=500、msg 为字符串
    - keyword/keywords 等服务端搜索参数会被忽略，关键词只能客户端过滤
    - city 参数有效（北京/上海能改变结果集）
    - 每页固定 10 条，无 pageSize 参数
  每条：city, maxsal, minsal, cname, name(职位), url(logo), refresh, uuid, ftype, day

详情 URL：https://www.shixiseng.com/intern/{uuid}
"""
from datetime import datetime, timedelta
from typing import Optional

from .base import BaseSource, make_id
from ._http import fetch_json


_API_URL = "https://www.shixiseng.com/app/interns"
_DETAIL_URL = "https://www.shixiseng.com/intern/{uuid}"

# 接口固定每页 10 条
_PAGE_SIZE = 10
# 无过滤时少翻几页；有城市/关键词时多翻几页，给客户端过滤留余量
_MAX_PAGES_PLAIN = 5
_MAX_PAGES_FILTERED = 10


def _fmt_salary(minsal, maxsal) -> Optional[str]:
    try:
        mn = int(float(minsal or 0))
        mx = int(float(maxsal or 0))
    except (TypeError, ValueError):
        return None
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
        kw = (keyword or "").strip()
        city_key = (city or "").replace("市", "").strip()
        max_pages = (_MAX_PAGES_FILTERED if (kw or city_key)
                     else _MAX_PAGES_PLAIN)

        raw: list[dict] = []
        seen_uuids: set[str] = set()
        for page in range(1, max_pages + 1):
            params = {
                "keyword": kw,
                "page": page,
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
                break
            code = data.get("code")
            items = data.get("msg")
            # 实测成功 code=100；500 时 msg 是错误字符串（如「参数错误」）
            if code not in (100, "100") or not isinstance(items, list):
                if code not in (100, "100"):
                    print(f"  [实习僧] 接口返回 code={code} msg={items!r}"[:160],
                          flush=True)
                break
            new_count = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                uuid = (it.get("uuid") or "").strip()
                if not uuid or uuid in seen_uuids:
                    continue
                seen_uuids.add(uuid)
                raw.append(it)
                new_count += 1
            if new_count == 0 or len(items) < _PAGE_SIZE:
                break
            # 城市服务端已过滤，但关键词没有；客户端过滤后够量就提前停
            if kw and len(raw) >= limit * 3:
                break

        out: list[dict] = []
        for it in raw:
            uuid = (it.get("uuid") or "").strip()
            title = (it.get("name") or "").strip()
            if not uuid or not title:
                continue

            company = (it.get("cname") or "").strip() or "未公开"
            loc = (it.get("city") or "").strip() or None
            if city_key and not self.match_city(loc, city_key):
                continue

            salary = _fmt_salary(it.get("minsal"), it.get("maxsal"))
            day = it.get("day")
            ftype = it.get("ftype")

            if kw:
                blob = " ".join(filter(None, [
                    title, company, loc, salary or "",
                    f"每周{day}天" if day else "",
                ]))
                if not self.match_keyword(blob, kw):
                    continue

            refresh = (it.get("refresh") or "").strip()
            pub = None
            if refresh:
                if len(refresh) <= 5:
                    # API 只给 MM-DD：跨年时 12 月岗位不能算成“未来日期”
                    now = datetime.now()
                    try:
                        dt = datetime.strptime(f"{now.year}-{refresh}", "%Y-%m-%d")
                        if dt > now + timedelta(days=1):
                            dt = dt.replace(year=now.year - 1)
                        pub = dt.isoformat(timespec="seconds")
                    except ValueError:
                        pub = None
                else:
                    pub = refresh

            summary_parts = []
            if salary:
                summary_parts.append(salary)
            if day:
                summary_parts.append(f"每周{day}天")
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
