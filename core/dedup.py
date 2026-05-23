"""数据去重模块"""
import re
from collections import defaultdict
from difflib import SequenceMatcher

from models.job import JobInfo
from config import DEDUP_THRESHOLD


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def deduplicate_jobs(all_jobs: list[JobInfo]) -> list[JobInfo]:
    """跨平台去重：基于公司名+职位名的相似度"""
    if not all_jobs:
        return []

    # 第一轮：精确去重（dedup_key 完全匹配）
    seen_keys = set()
    unique = []
    for job in all_jobs:
        key = job.dedup_key
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(job)

    # 第二轮：模糊去重 — 先按归一化公司名分桶，桶内两两比较（避免 O(n^2)）
    buckets: dict[str, list[JobInfo]] = defaultdict(list)
    for job in unique:
        bucket_key = _normalize_text(job.company_name or "")[:10]
        buckets[bucket_key].append(job)

    result: list[JobInfo] = []
    for bucket in buckets.values():
        for job in bucket:
            is_dup = False
            for existing in result:
                # L07: 同平台也做模糊去重（标题空格差异等）
                text_a = _normalize_text(f"{job.company_name}|{job.job_title}")
                text_b = _normalize_text(f"{existing.company_name}|{existing.job_title}")
                if text_a == text_b:
                    is_dup = True
                    if _completeness_score(job) > _completeness_score(existing):
                        result.remove(existing)
                        result.append(job)
                    break
                # 跨平台模糊比较
                if job.platform != existing.platform:
                    similarity = SequenceMatcher(None, text_a, text_b).ratio()
                    if similarity >= DEDUP_THRESHOLD:
                        if _completeness_score(job) > _completeness_score(existing):
                            result.remove(existing)
                            result.append(job)
                        is_dup = True
                        break
            if not is_dup:
                result.append(job)

    return result


def _completeness_score(job: JobInfo) -> int:
    """计算职位信息完整度分数"""
    score = 0
    fields = [
        job.salary_range, job.salary_min, job.city, job.district,
        job.experience_required, job.education_required,
        job.job_description, job.company_size, job.company_industry,
        job.benefits, job.publish_time,
    ]
    for f in fields:
        if f:
            score += 1
    if job.skills_required:
        score += 1
    if job.tags:
        score += 1
    return score


def sort_by_relevance(jobs: list[JobInfo], keyword: str) -> list[JobInfo]:
    """按关键词匹配度排序"""
    keyword_lower = keyword.lower()

    def score(job: JobInfo) -> float:
        s = 0.0
        title = job.job_title.lower()
        if keyword_lower == title:
            s += 100
        elif keyword_lower in title:
            s += 50
        if job.job_description and keyword_lower in job.job_description.lower():
            s += 10
        if job.skills_required:
            for skill in job.skills_required:
                if keyword_lower in skill.lower():
                    s += 5
        # 薪资高的加分
        if job.avg_salary:
            s += job.avg_salary * 0.01
        return s

    return sorted(jobs, key=score, reverse=True)
