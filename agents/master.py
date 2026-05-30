"""主协调代理 (Master Agent) — 支持 LLM 智能调度"""
import asyncio
import json
from datetime import datetime

from models.job import SearchParams, JobInfo, ScrapeResult, ScrapeStatus
from agents.boss import BossAgent
from agents.zhilian import ZhilianAgent
from agents.job51 import Job51Agent
from agents.liepin import LiepinAgent
from core.dedup import deduplicate_jobs, sort_by_relevance
from core.output import save_results_json, save_results_csv, format_summary
from core.llm_extractor import LLMExtractor, llm_message
from config import MAX_CONCURRENT_PLATFORMS


class MasterAgent:
    """主代理：协调所有平台子智能体（LLM加持）"""

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

    def __init__(self, params: SearchParams, progress_callback=None,
                 job_callback=None, log_callback=None):
        self.params = self._normalize_params(params)
        self.results: list[ScrapeResult] = []
        self.all_jobs: list[JobInfo] = []
        self._progress_cb = progress_callback
        self._job_cb = job_callback
        self._log_cb = log_callback
        self._llm: LLMExtractor | None = None
        # 防封号 / 上限 hooks — 由 server 注入，run() 时转发给每个子智能体
        self._stop_check = None
        self._global_limit = None
        self._global_count_fn = None

    def set_stop_check(self, fn):
        """注入"用户是否请求停止"的回调，run() 时转发给所有子智能体。"""
        self._stop_check = fn

    def set_global_limit(self, limit, count_fn):
        """注入任务级全局投递上限 (跨平台)，run() 时转发给所有子智能体。"""
        self._global_limit = limit
        self._global_count_fn = count_fn

    def _normalize_params(self, params: SearchParams) -> SearchParams:
        if not params.keyword or not params.keyword.strip():
            raise ValueError("职位关键词不能为空")
        params.keyword = params.keyword.strip()
        valid_platforms = [p for p in params.platforms if p in self.AGENT_MAP]
        if not valid_platforms:
            valid_platforms = list(self.AGENT_MAP.keys())
        params.platforms = valid_platforms
        return params

    async def _init_llm(self):
        """延迟初始化 LLM — 动态读取环境变量，支持运行时配置"""
        if self._llm is not None:
            return
        # 动态判断是否启用：LLM_ENABLED=true 或 配置了任意 API key
        import os
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
        """LLM 生成抓取计划摘要"""
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

    async def _llm_dedup(self) -> list[int]:
        """LLM 智能去重"""
        await self._init_llm()
        if not self._llm or len(self.all_jobs) < 2:
            return []

        sample = [
            {"idx": i, "title": j.job_title, "company": j.company_name,
             "city": j.city, "platform": j.platform_name}
            for i, j in enumerate(self.all_jobs[:50])
        ]
        try:
            return await self._llm.deduplicate_smart(json.dumps(sample, ensure_ascii=False))
        except Exception as e:
            print(f"  [LLM] _llm_dedup 失败: {e}", flush=True)
            return []

    async def run(self) -> dict:
        """执行自动投递任务（LLM 增强版）"""
        print("=" * 60, flush=True)
        print(f"  [MasterAgent] starting auto-apply...", flush=True)
        print(f"  [MasterAgent] keyword={self.params.keyword} | "
              f"city={self.params.city or 'all'} | "
              f"salary={self.params.salary_min or 0}K-{self.params.salary_max or 'any'}K | "
              f"auto_apply={self.params.auto_apply}",
              flush=True)
        print(f"  [MasterAgent] platforms: {', '.join(self.PLATFORM_NAMES[p] for p in self.params.platforms)}",
              flush=True)

        # LLM 规划
        plan = await self._llm_plan()
        if plan:
            print(f"  [LLM 建议] {plan}")

        print("=" * 60)

        # 并行启动所有子智能体（注入 LLM 提取器）
        tasks = []
        sub_agents = []  # 保留引用，便于 wrap 全局计数函数把实时 _applied_total 算进来
        for platform_key in self.params.platforms:
            agent_cls = self.AGENT_MAP[platform_key]
            agent = agent_cls()
            sub_agents.append(agent)
            if self._progress_cb:
                agent.set_progress_callback(self._progress_cb)
            if self._job_cb:
                agent.set_job_callback(self._job_cb)
            if self._log_cb:
                agent.set_log_callback(self._log_cb)
            # 注入 LLM 提取器
            if self._llm:
                agent.set_llm_extractor(self._llm)
            # 转发防封号 / 上限 hooks 给子智能体
            if self._stop_check and hasattr(agent, "set_stop_check"):
                agent.set_stop_check(self._stop_check)
            if self._global_limit is not None and hasattr(agent, "set_global_limit"):
                # 包装：原 fn 是上一组合结束时的快照；额外把所有正在跑的子 agent
                # 当前 _applied_total 加上，避免单次组合内全局上限漏报。
                base_fn = self._global_count_fn or (lambda: 0)
                def _live_global_count_fn(_agents=sub_agents, _base=base_fn):
                    try:
                        live = sum(getattr(a, "_applied_total", 0) for a in _agents)
                    except Exception:
                        live = 0
                    try:
                        snap = _base()
                    except Exception:
                        snap = 0
                    return snap + live
                agent.set_global_limit(self._global_limit, _live_global_count_fn)
            tasks.append(agent.scrape(self.params))

        sem = asyncio.Semaphore(MAX_CONCURRENT_PLATFORMS)

        async def bounded_scrape(coro):
            async with sem:
                return await coro

        results = await asyncio.gather(
            *[bounded_scrape(t) for t in tasks],
            return_exceptions=True
        )

        for i, platform_key in enumerate(self.params.platforms):
            result = results[i]
            if isinstance(result, Exception):
                self.results.append(ScrapeResult(
                    platform=platform_key,
                    platform_name=self.PLATFORM_NAMES[platform_key],
                    status=ScrapeStatus.FAILED,
                    error_message=str(result),
                ))
            else:
                self.results.append(result)

        for r in self.results:
            if r.status in (ScrapeStatus.SUCCESS, ScrapeStatus.PARTIAL):
                self.all_jobs.extend(r.jobs)

        # 数据处理：规则去重 + LLM 去重
        print(f"\n  [主代理] 数据汇总中...")
        total_before = len(self.all_jobs)
        self.all_jobs = deduplicate_jobs(self.all_jobs)

        # LLM 辅助去重（仅标记，不直接删除，避免误删）
        llm_dups = await self._llm_dedup()
        if llm_dups:
            valid_dups = [idx for idx in llm_dups if 0 <= idx < len(self.all_jobs)]
            if valid_dups:
                for idx in sorted(valid_dups, reverse=True):
                    self.all_jobs.pop(idx)
                print(f"  [LLM去重] 额外识别 {len(valid_dups)} 条重复"
                      f"（LLM 返回 {len(llm_dups)} 条，有效 {len(valid_dups)} 条）")

        print(f"  [主代理] 去重前: {total_before}条 | 去重后: {len(self.all_jobs)}条")

        self.all_jobs = sort_by_relevance(self.all_jobs, self.params.keyword)

        params_dict = self.params.to_dict()
        json_path = save_results_json(self.results, self.all_jobs, params_dict)
        csv_path = save_results_csv(self.all_jobs)

        summary = format_summary(self.results, self.all_jobs, params_dict)
        print(summary)
        print(f"\n  [主代理] JSON结果: {json_path}")
        print(f"  [主代理] CSV结果: {csv_path}")

        # 清理 LLM 连接。BrowserEngine 不在此处关闭——由调用方负责，
        # 避免多组合（keyword×city）任务每轮都把 Chrome 关掉再重启。
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
