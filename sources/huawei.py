"""华为社会招聘 (career.huawei.com) — JSON API 抓取

接口：GET https://career.huawei.com/reccampportal/services/portal/portalpub/getJob/newHr/page/{pageSize}/{pageNum}
  注意：URL 路径里 pageSize 在前、pageNum 在后（实测）。
  Referer 推荐：https://career.huawei.com/reccampportal/portal5/social-recruitment.html
  无 keyWord / keyword 服务端过滤（实测两种参数都不过滤），关键词只能客户端再筛。

响应顶层：
  {
    "pageVO": {"totalRows":..., "curPage":..., "pageSize":..., ...},
    "result": [ {jobname, jobAddress, jobFamilyName, degree, jobId, releaseDate, ...}, ... ]
  }

jobAddress 是 "China\\Guangdong-Shenzhen,China\\Shanghai-Shanghai,..." 这种英文逗号串，
需要拆开取 "City" 段并映射回中文（华为列表大部分城市拼音确定，做个常见城市表即可）。

详情页：https://career.huawei.com/reccampportal/portal5/social-recruitment-detail.html?jobId={jobId}
"""
from __future__ import annotations

from typing import Optional

from .base import BaseSource, make_id
from ._http import fetch_json


_API_TMPL = (
    "https://career.huawei.com/reccampportal/services/portal/portalpub/"
    "getJob/newHr/page/{page_size}/{page_num}"
)
_DETAIL_TMPL = (
    "https://career.huawei.com/reccampportal/portal5/"
    "social-recruitment-detail.html?jobId={job_id}"
)
_REFERER = (
    "https://career.huawei.com/reccampportal/portal5/social-recruitment.html"
)


# 英文城市拼音 → 中文（华为 jobAddress 段固定走 China\Province-City）
_CITY_CN: dict[str, str] = {
    "Beijing": "北京", "Shanghai": "上海", "Tianjin": "天津",
    "Chongqing": "重庆", "Shenzhen": "深圳", "Guangzhou": "广州",
    "Dongguan": "东莞", "Hangzhou": "杭州", "Nanjing": "南京",
    "Suzhou": "苏州", "Wuhan": "武汉", "Chengdu": "成都",
    "Xi'an": "西安", "Xian": "西安", "Hefei": "合肥",
    "Changsha": "长沙", "Jinan": "济南", "Qingdao": "青岛",
    "Tianfu": "成都", "Shenyang": "沈阳", "Dalian": "大连",
    "Xiamen": "厦门", "Fuzhou": "福州", "Zhengzhou": "郑州",
    "Ningbo": "宁波", "Wuxi": "无锡", "Changchun": "长春",
    "Harbin": "哈尔滨", "Kunming": "昆明", "Nanchang": "南昌",
    "Lanzhou": "兰州", "Guiyang": "贵阳", "Nanning": "南宁",
    "Taiyuan": "太原", "Hohhot": "呼和浩特", "Yinchuan": "银川",
    "Urumqi": "乌鲁木齐", "Lhasa": "拉萨", "Haikou": "海口",
    "Sanya": "三亚",
}


def _fmt_location(job_address: Optional[str]) -> Optional[str]:
    """华为 jobAddress 形如 'China\\Guangdong-Shenzhen,China\\Shanghai-Shanghai'。
    拆分逗号取每段最后的 City，映射到中文，去重保序。多城市用「/」连接。
    """
    if not job_address:
        return None
    seen: list[str] = []
    for seg in job_address.split(","):
        seg = seg.strip()
        if not seg:
            continue
        # 取最后一段：'Province-City' → 'City'；或 'Country\X' → 'X'
        tail = seg.split("\\")[-1].split("/")[-1]
        city_token = tail.split("-")[-1].strip()
        cn = _CITY_CN.get(city_token, city_token)
        if cn and cn not in seen:
            seen.append(cn)
    if not seen:
        return None
    return "/".join(seen[:4])  # 最多展示 4 个，避免 location 字段巨长


def _iso_date(s: Optional[str]) -> Optional[str]:
    """华为 releaseDate 形如 '2026-03-19T15:13:52.000+0800'，截到秒。"""
    if not s or not isinstance(s, str):
        return None
    # 去掉毫秒与时区，保留 YYYY-MM-DDTHH:MM:SS
    head = s.split(".")[0].split("+")[0].strip()
    return head or None


class HuaweiSource(BaseSource):
    key = "huawei"
    display_name = "华为招聘"
    category = "企业官网"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        # 华为 API 不支持服务端关键词过滤，先按 limit*3 抓再客户端筛
        page_size = max(limit * 3, 30)
        page_size = min(page_size, 100)
        url = _API_TMPL.format(page_size=page_size, page_num=1)
        data = await fetch_json(
            url,
            headers={"Referer": _REFERER},
            ttl=180.0,
        )
        if not data or not isinstance(data, dict):
            return []
        rows = data.get("result") or []
        if not rows:
            return []

        kw = (keyword or "").strip()
        city_key = (city or "").strip().replace("市", "")
        out: list[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            title = (r.get("jobname") or "").strip()
            job_id = r.get("jobId")
            if not title or job_id is None:
                continue

            location = _fmt_location(r.get("jobAddress"))
            family = (r.get("jobFamilyName") or "").strip() or None
            degree = (r.get("nameCn") and None) or (r.get("degree") or "").strip() or None
            # 关键词过滤：在 title / family / 业务描述里搜
            if kw:
                blob = f"{title} {family or ''} {r.get('mainBusiness') or ''}"
                if not self.match_keyword(blob, kw):
                    continue
            if city_key and not self.match_city(location, city_key):
                continue

            apply_url = _DETAIL_TMPL.format(job_id=job_id)
            pub = _iso_date(r.get("releaseDate") or r.get("creationDate"))

            # summary：拼 family / degree / 简短业务说明
            sum_parts: list[str] = []
            if family:
                sum_parts.append(family)
            if degree:
                sum_parts.append(degree)
            mb = (r.get("mainBusiness") or "").strip()
            if mb:
                mb1 = mb.replace("\n", " ").replace("\r", " ")
                if len(mb1) > 80:
                    mb1 = mb1[:80] + "…"
                sum_parts.append(mb1)
            summary = " · ".join(sum_parts)[:160] if sum_parts else None

            out.append({
                "id": make_id(self.key, str(job_id)),
                "source": self.key,
                "source_label": "华为",
                "title": title,
                "company": "华为",
                "location": location,
                "education": degree,
                "experience": None,  # API 未单独给经验字段
                "publish_time": pub,
                "apply_url": apply_url,
                "summary": summary,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
