"""任务级投递台账缓存。

MasterAgent 在任务开始时从 DB 预载：
  - 过去 7 天 applied 的 (platform, job_id)
  - 过去 24h uncertain 的 (platform, job_id)
  - 过去 24h 同公司 applied 计数
  - 过去 24h 各平台 applied 计数

四个平台 lane 共享同一个实例；投递成功/待确认后就地更新，
避免每单 1–2 次 SQL。DB 仍是最终权威。
"""
from __future__ import annotations

from typing import Iterable, Optional


class AppliedRegistry:
    def __init__(
        self,
        applied_ids: Optional[Iterable[tuple]] = None,
        uncertain_ids: Optional[Iterable[tuple]] = None,
        company_counts: Optional[dict] = None,
        applied_counts: Optional[dict] = None,
    ):
        self.applied_ids: set = set(applied_ids or ())
        self.uncertain_ids: set = set(uncertain_ids or ())
        self.company_counts: dict = dict(company_counts or {})
        self.applied_counts: dict = dict(applied_counts or {})
        self.failed_attempts: dict = {}

    # ───── 查询 ─────

    def is_applied(self, platform: str, job_id: str) -> bool:
        return bool(platform and job_id
                    and (platform, job_id) in self.applied_ids)

    def is_uncertain(self, platform: str, job_id: str) -> bool:
        return bool(platform and job_id
                    and (platform, job_id) in self.uncertain_ids)

    def company_count(self, company: str) -> int:
        return int(self.company_counts.get((company or "").strip(), 0))

    def applied_count(self, platform: str) -> int:
        return int(self.applied_counts.get(platform, 0))

    def failed_count(self, platform: str, job_id: str) -> int:
        return int(self.failed_attempts.get((platform, job_id), 0))

    # ───── 更新 ─────

    def record_applied(self, platform: str, job_id: str, company: str = ""):
        key = (platform, job_id)
        self.applied_ids.add(key)
        self.uncertain_ids.discard(key)
        self.applied_counts[platform] = self.applied_counts.get(platform, 0) + 1
        company = (company or "").strip()
        if company:
            self.company_counts[company] = self.company_counts.get(company, 0) + 1

    def record_uncertain(self, platform: str, job_id: str):
        self.uncertain_ids.add((platform, job_id))

    def record_failed(self, platform: str, job_id: str):
        key = (platform, job_id)
        self.failed_attempts[key] = self.failed_attempts.get(key, 0) + 1
