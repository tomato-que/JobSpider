"""华为社会招聘 (career.huawei.com) — JSON API 抓取

新版站点（2026）接口：
  POST https://apigw-dgg-b0.huawei.com/api/apig/channelhw/recruitmentPosition/pub/getJobPage?X-HW-ID=app_000000035886
  Header 必须带 `x-jalor-tenantalias: hcm`，否则返回
  {"code":"TenantContextError","message":"missing tenant code!!!"}。
  Body: {"curPage":1,"pageSize":100,"jobType":"SR"}
    - jobType=SR 社会招聘
    - keyWord 为服务端关键字过滤（实测有效）
    - pageSize 最大 100，翻页用 curPage
  响应: {"status":"SUCCESS","data":{"pageVO":{...},"result":[...]}}
  旧接口 career.huawei.com/reccampportal/.../page/{size}/{num} 仍可访问，
  但不带查询参数时只返回 2 条历史数据，已废弃。

详情页：https://career.huawei.com/cn/job-details?advertisementId={advertisementId}
  （注意不是 jobId）

字段说明：
  jobName        职位名
  workPlace      中文工作城市（优先）
  jobAddress     'China\\Guangdong-Shenzhen'，作为城市兜底
  jobFamilyName  岗位族 / deptName 部门
  workYear       '5' 表示 5 年以上
  jobRequire     任职要求（含「教育背景要求」「语言要求」）
  mainBusiness   岗位职责
  lastUpdateDate 更新日期 YYYY-MM-DD
"""
from __future__ import annotations

from typing import Optional

from .base import (BaseSource, make_id, parse_education, parse_experience)
from ._http import fetch_json


_API_URL = (
    "https://apigw-dgg-b0.huawei.com/api/apig/channelhw/"
    "recruitmentPosition/pub/getJobPage?X-HW-ID=app_000000035886"
)
_DETAIL_TMPL = (
    "https://career.huawei.com/cn/job-details?advertisementId={advert_id}"
)
# 新版网关要求的租户/语言头；缺 x-jalor-tenantalias 会直接 500 TenantContextError
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://career.huawei.com",
    "Referer": "https://career.huawei.com/",
    "x-alb-gray": "prod",
    "x-csrf-token": "",
    "x-hw-id": "app_000000035886",
    "x-jalor-tenantalias": "hcm",
    "x-language": "zh_CN",
    "x-referer": "https://career.huawei.com/cn",
}


# 英文城市拼音 → 中文（jobAddress 兜底用；新版 API 一般直接给中文 workPlace）
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
    """'China\\Guangdong-Shenzhen,China\\Shanghai-Shanghai' → '深圳/上海'。"""
    if not job_address or not isinstance(job_address, str):
        return None
    seen: list[str] = []
    for seg in job_address.split(","):
        seg = seg.strip()
        if not seg:
            continue
        tail = seg.split("\\")[-1].split("/")[-1]
        city_token = tail.split("-")[-1].strip()
        cn = _CITY_CN.get(city_token, city_token)
        if cn and cn not in seen:
            seen.append(cn)
    return "/".join(seen) if seen else None


def _iso_date(s: Optional[str]) -> Optional[str]:
    """兼容 '2026-03-19T15:13:52.000+0800' 与 '2026-10-16'。"""
    if not s or not isinstance(s, str):
        return None
    head = s.split(".")[0].split("+")[0].strip()
    return head or None


def _experience(work_year) -> Optional[str]:
    s = str(work_year or "").strip()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        if n <= 0:
            return "应届"
        return f"{n}年+"
    return parse_experience(s)


def _location(row: dict) -> Optional[str]:
    place = row.get("workPlace")
    if isinstance(place, str) and place.strip():
        return place.strip()
    return _fmt_location(row.get("jobAddress"))


class HuaweiSource(BaseSource):
    key = "huawei"
    display_name = "华为招聘"
    category = "企业官网"

    async def fetch(self, limit: int = 20,
                    keyword: str = "", city: str = "") -> list[dict]:
        kw = (keyword or "").strip()
        city_key = (city or "").strip().replace("市", "")

        # 服务端 keyWord 有效；无关键词时多翻几页，给城市/客户端过滤留余量
        page_size = 100
        max_pages = 2 if kw else 3
        rows: list[dict] = []
        seen_ids: set = set()

        for page_num in range(1, max_pages + 1):
            body = {"curPage": page_num, "pageSize": page_size,
                    "jobType": "SR"}
            if kw:
                body["keyWord"] = kw
            data = await fetch_json(
                _API_URL,
                method="POST",
                json_body=body,
                headers=_HEADERS,
                ttl=180.0,
            )
            if not isinstance(data, dict) or data.get("status") != "SUCCESS":
                break
            page_data = data.get("data") or {}
            page_rows = page_data.get("result") or []
            page_vo = page_data.get("pageVO") or {}
            new_count = 0
            for r in page_rows:
                if not isinstance(r, dict):
                    continue
                jid = r.get("jobId")
                if jid is None or jid in seen_ids:
                    continue
                seen_ids.add(jid)
                rows.append(r)
                new_count += 1
            if new_count == 0:
                break
            total_pages = page_vo.get("totalPages") or 0
            if total_pages and page_num >= total_pages:
                break
            if len(page_rows) < page_size:
                break

        if not rows:
            return []

        out: list[dict] = []
        for r in rows:
            title = (r.get("jobName") or r.get("jobNameNew")
                     or r.get("name") or "").strip()
            job_id = r.get("jobId")
            if not title or job_id is None:
                continue

            location = _location(r)
            family = (r.get("jobFamilyName") or "").strip() or None
            dept = (r.get("deptName") or "").strip() or None
            require = r.get("jobRequire") or ""
            main_business = r.get("mainBusiness") or ""

            if kw:
                blob = f"{title} {family or ''} {main_business} {require}"
                if not self.match_keyword(blob, kw):
                    continue
            if city_key and not self.match_city(location, city_key):
                continue

            advert_id = r.get("advertisementId")
            apply_url = _DETAIL_TMPL.format(advert_id=advert_id or job_id)

            degree = (r.get("degree") or "").strip() or None
            education = (degree or parse_education(require)
                         or parse_education(main_business))
            experience = _experience(r.get("workYear")) \
                or parse_experience(require)

            sum_parts: list[str] = []
            if family:
                sum_parts.append(family)
            if dept:
                sum_parts.append(dept)
            mb = main_business.replace("\n", " ").replace("\r", " ").strip()
            if mb:
                sum_parts.append(mb[:80] + ("…" if len(mb) > 80 else ""))
            summary = " · ".join(sum_parts)[:160] if sum_parts else None

            out.append({
                "id": make_id(self.key, str(job_id)),
                "source": self.key,
                "source_label": "华为",
                "title": title,
                "company": f"华为 · {dept}" if dept else "华为",
                "location": location,
                "education": education,
                "experience": experience,
                "publish_time": _iso_date(
                    r.get("releaseDate") or r.get("lastUpdateDate")),
                "apply_url": apply_url,
                "summary": summary,
                "fetched_at": self.now_iso(),
            })
            if len(out) >= limit:
                break
        return out
