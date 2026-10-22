"""主协调代理 (Master Agent) — 平台 lane 工作队列 + LLM 智能调度"""
import asyncio
import json
import os
import random
from typing import Callable, Optional

from models.job import SearchParams, JobInfo, ScrapeResult, ScrapeStatus
from agents.boss import BossAgent
from agents.zhilian import ZhilianAgent
from agents.job51 import Job51Agent
from agents.liepin import LiepinAgent
from core.dedup import deduplicate_jobs, sort_by_relevance
from core.output import save_results_json, save_results_csv, format_summary
from core.llm_extractor import LLMExtractor, llm_message
from core import db
from core.applied_registry import AppliedRegistry


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


class MasterAgent:
    """主代理：每个平台一个 lane，lane 内顺序消费 kw×city 组合。"""

    AGENT_MAP = {
        "boss": BossAgent,
        "zhilian": ZhilianAgent,
        "job51": Job51Agent,
        "liepin": LiepinAgent,
    }

    PLATFORM_NAMES = {
        "boss": "Boss直聘",
        "zhilian": "智联招聘",
        "job51": "前程无忧",
        "liepin": "猎聘",
    }

    def __init__(self, params, progress_callback=None,
                 job_callback=None, log_callback=None):
        # 兼容单组合 (SearchParams) 与多组合 (list[SearchParams])
        plist = list(params) if isinstance(params, (list, tuple)) else [params]
        if not plist:
            raise ValueError("至少需要一个搜索参数")
        self.params_list = [self._normalize_params(p) for p in plist]
        self.params = self.params_list[0]

        self.results: list[ScrapeResult] = []
        self.all_jobs: list[JobInfo] = []
        self._progress_cb = progress_callback
        self._job_cb = job_callback
        self._log_cb = log_callback
        self._llm: Optional[LLMExtractor] = None
        # 平台 key -> agent 实例（lane）
        self._agents: dict = {}
        # 跨 lane 的投递动作限流，避免 4 个平台同一秒各点一次
        self._apply_sem = asyncio.Semaphore(
            max(1, int(_env_float("JOBSPIDER_APPLY_CONCURRENCY", 2)))
        )
        # 防封号 / 上限 hooks — 由 server 注入
        self._stop_check = None
        self._global_limit = None
        self._global_count_fn = None

    def set_stop_check(self, fn):
        self._stop_check = fn

    def set_global_limit(self, limit, count_fn=None):
        self._global_limit = limit
        self._global_count_fn = count_fn

    def _global_count(self) -> int:
        try:
            live = sum(getattr(a, "_applied_total", 0)
                       for a in self._agents.values())
        except Exception:
            live = 0
        if self._global_count_fn:
            try:
                return live + int(self._global_count_fn())
            except Exception:
                return live
        return live

    def _normalize_params(self, params: SearchParams) -> SearchParams:
        if not params.keyword or not params.keyword.strip():
            raise ValueError("职位关键词不能为空")
        params.keyword = params.keyword.strip()
        valid_platforms = [p for p in params.platforms if p in self.AGENT_MAP]
        if not valid_platforms:
            valid_platforms = list(self.AGENT_MAP.keys())
        params.platforms = valid_platforms
        return params

    # ──────── LLM ────────

    async def _init_llm(self):
        if self._llm is not None:
            return
        enabled = os.getenv("LLM_ENABLED", "false").lower() == "true"
        has_key = any(os.getenv(k, "") for k in [
            "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "OPENROUTER_API_KEY", "SILICONFLOW_API_KEY", "LLM_API_KEY",
        ])
        if enabled or has_key:
            self._llm = LLMExtractor()
            print(f"  [MasterAgent] LLM 已启用: provider={os.getenv('LLM_PROVIDER', '')} "
                  f"model={os.getenv('LLM_MODEL', '')}", flush=True)
        else:
            print(f"  [MasterAgent] LLM 未启用（未配置 API Key）", flush=True)

    async def _llm_plan(self) -> str:
        await self._init_llm()
        client = await self._llm._get_client() if self._llm else None
        if not client:
            return ""
        info = {
            "keyword": self.params.keyword,
            "city": self.params.city or "全国",
            "salary": f"{self.params.salary_min or 0}K-{self.params.salary_max or '不限'}K",
            "experience": self.params.experience or "不限",
            "platforms": [self.PLATFORM_NAMES[p] for p in self.params.platforms],
            "combos": len(self.params_list),
        }
        try:
            resp = await client.chat([
                llm_message("user", f"""你是招聘数据抓取系统的总调度。根据以下搜索参数，给出抓取建议（1-2句话）:
{json.dumps(info, ensure_ascii=False)}
建议可包括：推荐优先级、可能的数据量预估、注意事项。""")
            ], temperature=0.3, max_tokens=200)
            return resp.content.strip()
        except Exception as e:
            print(f"  [LLM] _llm_plan 失败: {e}", flush=True)
            return ""

    async def _llm_dedup(self) -> list:
        """LLM 辅助去重（默认关闭，避免误删）。"""
        if os.getenv("JOBSPIDER_LLM_DEDUP", "false").lower() != "true":
            return []
        await self._init_llm()
        if not self._llm or len(self.all_jobs) < 2:
            return []
        sample = [
            {"idx": i, "title": j.job_title, "company": j.company_name,
             "city": j.city, "platform": j.platform_name}
            for i, j in enumerate(self.all_jobs[:50])
        ]
        try:
            return await self._llm.deduplicate_smart(
                json.dumps(sample, ensure_ascii=False))
        except Exception as e:
            print(f"  [LLM] _llm_dedup 失败: {e}", flush=True)
            return []

    async def _build_registry(self) -> AppliedRegistry:
        try:
            applied = await db.get_applied_keys(168)
            uncertain = await db.get_uncertain_keys(24)
            companies = await db.get_company_applied_counts(24)
            counts = await db.get_applied_counts_by_platform(24)
            print(f"  [MasterAgent] registry: applied={len(applied)} "
                  f"uncertain={len(uncertain)} companies={len(companies)}",
                  flush=True)
            return AppliedRegistry(applied, uncertain, companies, counts)
        except Exception as e:
            print(f"  [MasterAgent] registry 预载失败(降级为空): {e}", flush=True)
            return AppliedRegistry()

    # ──────── lane ────────

    def _make_agent(self, platform_key: str):
        agent = self.AGENT_MAP[platform_key]()
        if self._progress_cb:
            agent.set_progress_callback(self._progress_cb)
        if self._job_cb:
            agent.set_job_callback(self._job_cb)
        if self._log_cb:
            agent.set_log_callback(self._log_cb)
        if self._llm:
            agent.set_llm_extractor(self._llm)
        if self._stop_check and hasattr(agent, "set_stop_check"):
            agent.set_stop_check(self._stop_check)
        agent.set_apply_semaphore(self._apply_sem)
        return agent

    async def _run_lane(self, platform_key: str, agent) -> list:
        results = []
        await asyncio.sleep(random.uniform(0, 20))  # 平台错峰，避免同一秒开页
        n = len(self.params_list)
        for i, p in enumerate(self.params_list):
            if self._stop_check and self._stop_check():
                break
            if getattr(agent, "_platform_done", False):
                break
            if self._global_limit is not None and self._global_count() >= self._global_limit:
                break
            try:
                r = await agent.scrape(p)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                r = ScrapeResult(
                    platform=platform_key,
                    platform_name=self.PLATFORM_NAMES[platform_key],
                    status=ScrapeStatus.FAILED,
                    error_message=str(e),
                )
            results.append(r)
            if i < n - 1:
                pause = _env_float("JOBSPIDER_COMBO_PAUSE", 0.0)
                if pause > 0:
                    await asyncio.sleep(pause)
        return results

    def _merge_lane(self, platform_key: str, lane_results: list) -> ScrapeResult:
        name = self.PLATFORM_NAMES[platform_key]
        jobs = []
        total = 0
        statuses = []
        errors = []
        for r in lane_results:
            total += r.total_count
            jobs.extend(r.jobs)
            statuses.append(r.status)
            if r.error_message:
                errors.append(r.error_message)
        if not statuses:
            status = ScrapeStatus.SKIPPED
        elif all(s == ScrapeStatus.SKIPPED for s in statuses):
            status = ScrapeStatus.SKIPPED
        elif any(s == ScrapeStatus.FAILED for s in statuses) and not jobs:
            status = ScrapeStatus.FAILED
        elif any(s in (ScrapeStatus.PARTIAL, ScrapeStatus.FAILED) for s in statuses):
            status = ScrapeStatus.PARTIAL
        else:
            status = ScrapeStatus.SUCCESS
        return ScrapeResult(
            platform=platform_key, platform_name=name, status=status,
            total_count=total, jobs=jobs,
            error_message=("; ".join(errors)[:500] or None),
        )

    # ──────── 主流程 ────────

    async def run(self) -> dict:
        print("=" * 60, flush=True)
        print(f"  [MasterAgent] starting auto-apply...", flush=True)
        print(f"  [MasterAgent] combos={len(self.params_list)} | "
              f"keyword={self.params.keyword} | "
              f"city={self.params.city or 'all'} | "
              f"auto_apply={self.params.auto_apply}", flush=True)
        print(f"  [MasterAgent] platforms: "
              f"{', '.join(self.PLATFORM_NAMES[p] for p in self.params.platforms)}",
              flush=True)

        await self._init_llm()
        plan = await self._llm_plan()
        if plan:
            print(f"  [LLM 建议] {plan}")
        print("=" * 60)

        registry = await self._build_registry()
        from core.risk_guard import RiskGuard
        RiskGuard.get_instance().reset_task_state()
        for platform_key in self.params.platforms:
            self._agents[platform_key] = self._make_agent(platform_key)
        for agent in self._agents.values():
            agent.set_registry(registry)
            if self._global_limit is not None:
                agent.set_global_limit(self._global_limit, self._global_count)

        pairs = list(self._agents.items())
        tasks = [self._run_lane(pk, agent) for pk, agent in pairs]
        lane_results = await asyncio.gather(*tasks, return_exceptions=True)

        for (platform_key, _agent), lane_res in zip(pairs, lane_results):
            if isinstance(lane_res, Exception):
                self.results.append(ScrapeResult(
                    platform=platform_key,
                    platform_name=self.PLATFORM_NAMES[platform_key],
                    status=ScrapeStatus.FAILED,
                    error_message=str(lane_res),
                ))
                continue
            merged = self._merge_lane(platform_key, lane_res)
            self.results.append(merged)
            if merged.status in (ScrapeStatus.SUCCESS, ScrapeStatus.PARTIAL):
                self.all_jobs.extend(merged.jobs)

        print(f"\n  [主代理] 数据汇总中...")
        total_before = len(self.all_jobs)
        self.all_jobs = deduplicate_jobs(self.all_jobs)
        self.all_jobs = sort_by_relevance(self.all_jobs, self.params.keyword)
        print(f"  [主代理] 去重前: {total_before}条 | 去重后: {len(self.all_jobs)}条")

        params_dict = self.params.to_dict()
        params_dict["combos"] = [p.to_dict() for p in self.params_list]
        json_path = save_results_json(self.results, self.all_jobs, params_dict)
        csv_path = save_results_csv(self.all_jobs)
        summary = format_summary(self.results, self.all_jobs, params_dict)
        print(summary)
        print(f"\n  [主代理] JSON结果: {json_path}")
        print(f"  [主代理] CSV结果: {csv_path}")

        # BrowserEngine 不在此关闭 — 由 server 在任务结束时统一关闭
        if self._llm:
            await self._llm.close()

        return {
            "results": [r.to_dict() for r in self.results],
            "jobs": [j.to_dict() for j in self.all_jobs],
            "total": len(self.all_jobs),
            "json_path": str(json_path),
            "csv_path": str(csv_path),
        }

    def get_stats(self) -> dict:
        stats = {
            "total": len(self.all_jobs),
            "platforms": {},
            "cities": {},
            "salary_avg": 0.0,
        }
        for r in self.results:
            stats["platforms"][r.platform_name] = {
                "count": r.total_count,
                "status": r.status.value,
            }
        for j in self.all_jobs:
            if j.city:
                stats["cities"][j.city] = stats["cities"].get(j.city, 0) + 1
        salaries = [j.avg_salary for j in self.all_jobs if j.avg_salary]
        if salaries:
            stats["salary_avg"] = sum(salaries) / len(salaries)
            stats["salary_min"] = min(salaries)
            stats["salary_max"] = max(salaries)
        return stats
