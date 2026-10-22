"""子智能体基类 — 浏览器交互版

核心理念：像真人操作浏览器
打开搜索页 → 选城市 → 输关键词 → 点搜索 → 提取结果 → 翻页

本文件只保留：模板方法 (scrape 主循环) + 流程 hooks + 抽象方法 +
进度回调 + 反爬冷却 + 页面操作快捷封装。

其他职责拆到了 mixin：
- agents/_interactions.py : 浏览器交互 (human_click / select_city / 翻页等)
- agents/_apply.py        : 投递编排 (apply_to_page_jobs / generate_greeting / find 构造器)
- agents/_filters.py      : 筛选 + 薪资解析
"""
import asyncio
import hashlib
import json
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from models.job import SearchParams, JobInfo, ScrapeResult, ScrapeStatus
from core.scrapling_engine import ScraplingEngine
from core.risk_guard import RiskGuard, RiskSignal
from config import MAX_PAGES_PER_PLATFORM, gaussian_delay

from ._interactions import BrowserInteractionsMixin
from ._apply import ApplyMixin
from ._filters import FilterMixin


class BaseAgent(BrowserInteractionsMixin, ApplyMixin, FilterMixin, ABC):
    """模板方法骨架；具体浏览器交互/投递/筛选拆到 mixin。"""

    # ──────── 模板配置（子类覆盖） ────────
    # 搜索框 / 搜索按钮的候选选择器（按优先级降序）
    SEARCH_INPUT_SELECTORS: list = []
    SEARCH_BUTTON_SELECTORS: list = []
    # 等待搜索结果加载用的容器选择器
    RESULT_CONTAINER_SELECTOR: str = ""
    # diagnose_buttons 用的卡片选择器
    DIAGNOSE_CARD_SELECTOR: str = ""
    # 城市选择器触发器候选（None = 平台没有标准城市切换 UI）
    CITY_TRIGGERS: Optional[list] = None
    CITY_CONTAINER: Optional[str] = None
    # 翻页/滚动节奏
    SCROLL_TIMES_FIRST: int = 3
    SCROLL_TIMES_NEXT: int = 2
    PAGE_TURN_DELAY: tuple = (2.0, 5.0)

    def __init__(self, platform_key: str):
        from config import PLATFORM_CONFIG
        self.platform_key = platform_key
        self.platform_cfg = PLATFORM_CONFIG[platform_key]
        self._progress_cb: Optional[Callable] = None
        self._job_cb: Optional[Callable] = None
        self._log_cb: Optional[Callable] = None
        self._llm = None
        self.engine = ScraplingEngine.get_instance()
        self.risk = RiskGuard.get_instance()
        # 反爬 / 选择器漂移导致的连续失败计数（成功 1 页即清零）
        self._consecutive_failures = 0
        # 跨组合累计（lane 模式下由同一 agent 消费多个 kw×city）
        self._lane_total = 0
        # 任务级共享的已投/待确认/同公司计数
        self._registry = None
        # 跨 lane 投递动作限流（由 MasterAgent 注入）
        self._apply_sem = None
        # 本 agent 内的 LLM 缓存，避免跨组合重复调用
        self._greeting_cache: dict = {}
        self._rule_cache: dict = {}
        # Stop & global-limit hooks (injected by server / master agent)
        self._stop_check = None        # Callable[[], bool] — returns True if user requested stop
        self._global_limit = None      # int | None — task-level total cap across all platforms
        self._global_count_fn = None   # Callable[[], int] — returns current global applied total

    def set_progress_callback(self, cb: Callable):
        self._progress_cb = cb

    def set_job_callback(self, cb: Callable):
        """注入逐条 job 回调：cb(job_dict, event) — event 为 'new' / 'update'"""
        self._job_cb = cb

    def set_log_callback(self, cb: Callable):
        """注入日志回调：cb(message: str) — 投递决策、招呼语等实时推到前端"""
        self._log_cb = cb

    def set_stop_check(self, fn):
        """Inject a callable returning True when the user wants the task to stop.
        Called sync from inside the per-job loop — must be cheap and non-blocking.
        """
        self._stop_check = fn

    def set_global_limit(self, limit, count_fn):
        """Inject task-level global cap (across all platforms/combos).
        limit: int (None = unlimited)
        count_fn: () -> int returning current global applied total
        """
        self._global_limit = limit
        self._global_count_fn = count_fn

    def set_registry(self, registry):
        """注入任务级 AppliedRegistry（预载的已投/待确认/同公司计数）。"""
        self._registry = registry

    def set_apply_semaphore(self, sem):
        """注入跨 lane 的投递动作信号量。"""
        self._apply_sem = sem

    def should_stop(self) -> bool:
        """Sync helper: True if user requested stop OR global cap reached.
        Safe to call from anywhere in the apply/scrape loops. Returns False on any error.
        """
        try:
            if self._stop_check and self._stop_check():
                return True
            if self._global_limit is not None and self._global_count_fn:
                if self._global_count_fn() >= self._global_limit:
                    return True
        except Exception:
            return False
        return False

    def log(self, msg: str):
        """打印 + 推送到前端日志面板"""
        print(msg, flush=True)
        if self._log_cb:
            try:
                self._log_cb(msg)
            except Exception:
                pass

    def emit_job(self, job: JobInfo, event: str = "update"):
        """向上层推送单条职位（新增 / 投递状态变更）"""
        if not self._job_cb:
            return
        try:
            self._job_cb(job.to_dict(), event)
        except Exception as e:
            print(f"  [{self.platform_cfg['name']}] job_cb error: {e}", flush=True)

    def set_llm_extractor(self, llm):
        self._llm = llm

    # ──────── 实时推送：抓到即推、命中上限即停 ────────

    def stream_new_jobs(self, jobs: list):
        """抓到本页职位后立即推送到前端（pending 状态），与投递解耦"""
        if not self._job_cb or not jobs:
            return
        for j in jobs:
            self.emit_job(j, "new")

    def get_platform_limit(self, params) -> "int | None":
        """读取本平台投递上限：仅 None 表示不限；0 视为已达上限"""
        if not params.platform_limits:
            return None
        raw = params.platform_limits.get(self.platform_key)
        if raw is None:
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    def limit_reached(self, params) -> bool:
        """检查是否已达投递上限：
        - 平台级 platform_limits[self.platform_key]
        - 任务级 global_limit (跨平台总上限)
        - 平台 24h DB 日上限 daily_limit_per_platform (由 apply_to_page_jobs 命中时设 _platform_done)
        任一命中即返回 True，让 scrape 主循环提前结束翻页。
        """
        # 由 apply_to_page_jobs 设置：本平台所有上限闸已命中（含 DB 24h 日上限）
        if getattr(self, "_platform_done", False):
            return True
        # 全局上限
        if self._global_limit is not None and self._global_count_fn:
            try:
                if self._global_count_fn() >= self._global_limit:
                    return True
            except Exception:
                pass
        # 平台上限
        limit = self.get_platform_limit(params)
        if limit is None:
            return False
        return getattr(self, "_applied_total", 0) >= limit

    # ──────── 反爬冷却 ────────

    def _get_cooldown_list(self) -> list:
        """读取 PLATFORM_CONFIG[平台].rate_limit_cooldown 列表，缺省 [10,20,40,60]"""
        try:
            from config import PLATFORM_CONFIG
            return PLATFORM_CONFIG[self.platform_key].get(
                "rate_limit_cooldown", [10, 20, 40, 60]
            )
        except Exception:
            return [10, 20, 40, 60]

    async def _cooldown_after_failure(self, reason: str = "") -> None:
        """连续失败计数 +1，然后按 cooldown 列表 sleep。"""
        cooldown = self._get_cooldown_list() or [10, 20, 40, 60]
        idx = min(self._consecutive_failures, len(cooldown) - 1)
        secs = cooldown[idx]
        name = self.platform_cfg["name"]
        print(f"  [{name}] 冷却 {secs}s (连续失败 #{self._consecutive_failures + 1}, {reason})",
              flush=True)
        self._consecutive_failures += 1
        try:
            await asyncio.sleep(secs)
        except Exception:
            pass

    # ──────── 模板方法：抓取流程 ────────

    async def scrape(self, params: SearchParams) -> ScrapeResult:
        """通用抓取流程：打开搜索页 → 选城市 → 输关键词 → 提取 → 翻页

        子类通过覆盖 hook (_pre_check / _check_login_wall / _check_anti_bot /
        _select_city / _click_next) 实现平台差异；常规情况下子类只需配置类属性
        SEARCH_INPUT_SELECTORS 等并实现 extract_list_page / apply_to_job。
        """
        name = self.platform_cfg["name"]
        all_jobs: list[JobInfo] = []
        error_msg = None
        page = None
        status = ScrapeStatus.SUCCESS

        blocked = await self.risk.is_blocked(self.platform_key)
        if blocked:
            until = blocked.get("blocked_until") or "?"
            reason = blocked.get("reason") or blocked.get("state") or ""
            msg = f"风控冷却中（至 {until}，{reason}）"
            self.log(f"  [{name}] ⏸ {msg}")
            self.emit_progress(0, "blocked")
            return ScrapeResult(
                platform=self.platform_key, platform_name=name,
                status=ScrapeStatus.SKIPPED, total_count=0,
                jobs=[], error_message=msg,
            )

        self.log(f"  [{name}] opening search page...")
        try:
            if self.should_stop():
                self.log(f"  [{name}] 用户停止（启动前）")
                return ScrapeResult(
                    platform=self.platform_key, platform_name=name,
                    status=ScrapeStatus.SKIPPED, total_count=0,
                    jobs=[], error_message="用户停止",
                )
            page = await self._do_search(params)

            await asyncio.sleep(gaussian_delay(0.5, 1.2))

            # 先判登录墙/风控，再等列表容器：挑战页永远不会出现容器
            if await self._check_login_wall(page):
                error_msg = f"Need to login {name} first"
                status = ScrapeStatus.FAILED
                await self.risk.trip(self.platform_key,
                                     RiskSignal.LOGIN_REQUIRED, error_msg)
                self.emit_progress(error_msg, "error")
                return ScrapeResult(
                    platform=self.platform_key, platform_name=name,
                    status=status, total_count=0,
                    jobs=[], error_message=error_msg,
                )

            if await self._check_anti_bot(page):
                return ScrapeResult(
                    platform=self.platform_key, platform_name=name,
                    status=ScrapeStatus.SKIPPED, total_count=0,
                    jobs=[], error_message="风控暂停",
                )

            container_ok = False
            if self.RESULT_CONTAINER_SELECTOR:
                try:
                    await self.engine.wait_for(
                        page, self.RESULT_CONTAINER_SELECTOR, timeout=15000,
                    )
                    container_ok = True
                except Exception:
                    url = await self.engine.current_url(page)
                    print(f"  [{name}] wait timeout (URL: {url})")
            else:
                # 平台未配置容器选择器，视为已就绪
                container_ok = True

            if not await self._list_ready(page):
                await self.scroll_page(page, times=self.SCROLL_TIMES_FIRST)
            page_jobs = await self.extract_list_page(page, params)
            if page_jobs:
                self._consecutive_failures = 0
                self._log_extraction_quality(page_jobs)
                all_jobs.extend(page_jobs)
                self.stream_new_jobs(page_jobs)
                self._lane_total += len(page_jobs)
                self.emit_progress(self._lane_total)
                if params.auto_apply and self.DIAGNOSE_CARD_SELECTOR:
                    await self.diagnose_buttons(page, self.DIAGNOSE_CARD_SELECTOR)
                await self.apply_to_page_jobs(page, page_jobs, params)
                if self.limit_reached(params):
                    print(f"  [{name}] 已达投递上限，停止翻页", flush=True)
            else:
                # 第一页就 0 条：若容器明明等到了 → 选择器可能漂移，标记 PARTIAL 并截图诊断
                if container_ok:
                    status = ScrapeStatus.PARTIAL
                    error_msg = (
                        f"容器存在但 0 条记录被解析 — 可能选择器漂移 "
                        f"(page=1, container={self.RESULT_CONTAINER_SELECTOR})"
                    )
                    print(f"  [{name}] {error_msg}", flush=True)
                    await self._diagnose_selectors(page)
                    # M17: LLM 选择器降级 — 让 LLM 分析 DOM 生成新选择器重试
                    page_jobs = await self._llm_fallback_extract(page, params)
                    if page_jobs:
                        self._consecutive_failures = 0
                        status = ScrapeStatus.SUCCESS
                        error_msg = None
                        self._log_extraction_quality(page_jobs)
                        all_jobs.extend(page_jobs)
                        self.stream_new_jobs(page_jobs)
                        self._lane_total += len(page_jobs)
                        self.emit_progress(self._lane_total)
                        await self.apply_to_page_jobs(page, page_jobs, params)
                    else:
                        try:
                            from config import ROOT_DIR
                            debug_dir = ROOT_DIR / "output" / "debug"
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            ts = time.strftime("%Y%m%d_%H%M%S")
                            shot_path = str(debug_dir / f"{self.platform_key}_{ts}_empty.png")
                            await self.engine.screenshot(page, path=shot_path)
                        except Exception as _e:
                            print(f"  [{name}] 诊断截图失败: {_e}", flush=True)
                        await self._cooldown_after_failure(reason="first_page_empty")

            for _ in range(2, MAX_PAGES_PER_PLATFORM + 1):
                if not page_jobs:
                    break
                if self.should_stop():
                    self.log(f"  [{name}] 用户停止，结束抓取")
                    break
                if self.limit_reached(params):
                    break
                if await self._check_anti_bot(page):
                    print(f"  [{name}] anti-bot triggered, stopping")
                    await self._cooldown_after_failure(reason="anti_bot")
                    break

                has_next = await self._click_next(page)
                if not has_next:
                    break
                await asyncio.sleep(gaussian_delay(*self.PAGE_TURN_DELAY))
                if not await self._list_ready(page):
                    await self.scroll_page(page, times=self.SCROLL_TIMES_NEXT)
                page_jobs = await self.extract_list_page(page, params)
                if page_jobs:
                    self._consecutive_failures = 0
                    all_jobs.extend(page_jobs)
                    self.stream_new_jobs(page_jobs)
                    self._lane_total += len(page_jobs)
                    self.emit_progress(self._lane_total)
                    await self.apply_to_page_jobs(page, page_jobs, params)
                    if self.limit_reached(params):
                        print(f"  [{name}] 已达投递上限，停止翻页", flush=True)
                        break
                else:
                    await self._cooldown_after_failure(reason="next_page_empty")

            self.emit_progress(self._lane_total, "done")

        except Exception as e:
            error_msg = str(e)
            status = ScrapeStatus.FAILED if not all_jobs else ScrapeStatus.PARTIAL
            self.emit_progress(error_msg, "error")
        finally:
            # 每个组合结束都清理：搜索页 + 平台残留的详情页。
            try:
                await self._cleanup_after_scrape()
            except Exception:
                pass
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

        return ScrapeResult(
            platform=self.platform_key, platform_name=name,
            status=status, total_count=len(all_jobs),
            jobs=all_jobs, error_message=error_msg,
        )

    # ──────── 流程 hooks（子类可覆盖） ────────

    async def _pre_check(self, page) -> "str | None":
        """搜索页加载后的拦截早退。返回错误字符串则中止；返回 None 继续。"""
        return None

    async def _select_city(self, page, city: str) -> bool:
        """默认基于 CITY_TRIGGERS 调用通用城市选择器；子类可覆盖更复杂流程。"""
        if not self.CITY_TRIGGERS:
            return False
        return await self.select_city_by_text(
            page, city,
            trigger_selectors=self.CITY_TRIGGERS,
            container_selector=self.CITY_CONTAINER,
        )

    async def _check_login_wall(self, page) -> bool:
        """搜索后是否被登录墙拦截。默认不检查。"""
        return False

    async def _check_anti_bot(self, page) -> bool:
        """全链路风控检测：URL / DOM / 网络响应 / 登录态。

        命中后写入平台级冷却并停止本平台后续动作；其他平台不受影响。
        """
        signal, evidence = await self.risk.inspect(page, self.platform_key)
        if signal == RiskSignal.OK:
            return False
        until = await self.risk.trip(self.platform_key, signal, evidence)
        self._platform_done = True
        self.log(
            f"  [{self.platform_cfg['name']}] 🚨 风控信号 {signal.value}: "
            f"{evidence} → 暂停至 {until}（可调用 /api/risk/clear 手动解除）"
        )
        self.emit_progress(0, "blocked")
        return True

    async def _click_next(self, page) -> bool:
        """翻页：默认使用通用 click_next_page，子类可针对自家分页器覆盖。"""
        return await self.click_next_page(page)

    async def _do_search(self, params):
        """打开搜索页 → 选城市 → 输关键词 → 点搜索。子类可覆盖以使用 URL 直达等方式。

        返回已到达搜索结果的 page 对象。若需中止，抛出 RuntimeError(原因)。
        """
        name = self.platform_cfg["name"]
        page = await self.fetch_page(
            self.platform_cfg["search_url"], network_idle=True,
        )
        await asyncio.sleep(gaussian_delay(1, 2))
        pre_err = await self._pre_check(page)
        if pre_err:
            raise RuntimeError(pre_err)
        if params.city:
            self.log(f"  [{self.platform_cfg['name']}] 🌐 正在筛选城市: {params.city} ...")
            city_ok = await self._select_city(page, params.city)
            if city_ok:
                self.log(f"  [{self.platform_cfg['name']}] ✓ 城市已选: {params.city}")
            else:
                self.log(f"  [{self.platform_cfg['name']}] ⚠ 城市筛选失败（可能 UI 变更或不支持），将使用平台默认城市抓取")
        filled = await self.input_keyword(
            page, params.keyword, self.SEARCH_INPUT_SELECTORS,
        )
        if not filled:
            print(f"  [{name}] search input not found")
        await self.click_search_button(page, self.SEARCH_BUTTON_SELECTORS)
        return page

    async def _cleanup_after_scrape(self):
        """组合结束后的平台清理钩子（关闭 pending 详情页等）。"""
        return None

    # ──────── 抽象方法 ────────

    @abstractmethod
    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        ...

    @abstractmethod
    async def extract_detail_page(self, page, job_id: str) -> dict:
        ...

    # ──────── 页面操作快捷方法 ────────

    async def fetch_page(self, url: str, wait_selector: str = None,
                         network_idle: bool = True):
        return await self.engine.fetch_page(
            url=url, network_idle=network_idle,
            wait_selector=wait_selector,
        )

    async def eval_js(self, page, script: str):
        return await self.engine.eval_js(page, script)

    async def scroll_page(self, page, times: int = 3):
        await self.engine.scroll_page(page, times)

    async def _list_ready(self, page, threshold: int = 25) -> bool:
        """列表卡片已足够多时跳过滚动（省 2–3s/页）。"""
        sel = self.DIAGNOSE_CARD_SELECTOR or self.RESULT_CONTAINER_SELECTOR
        if not sel:
            return False
        try:
            n = await self.eval_js(
                page, f"() => document.querySelectorAll({sel!r}).length")
            return bool(n and int(n) >= threshold)
        except Exception:
            return False

    async def random_delay(self, min_s: float = None, max_s: float = None):
        await asyncio.sleep(gaussian_delay(min_s or 2, max_s or 5))

    # ──────── LLM 选择器降级提取（M17） ────────

    async def _llm_fallback_extract(self, page, params: SearchParams) -> list[JobInfo]:
        """当硬编码选择器全部失效时，用 LLM 分析 DOM 生成新选择器并重试提取"""
        if not self._llm:
            return []
        name = self.platform_cfg["name"]
        self.log(f"  [{name}] 尝试 LLM 选择器降级…")

        try:
            dom_text = await self.eval_js(page, """() => {
                const el = document.querySelector(
                    '.job-list-box, .search-result, .result-list, '
                    + '[class*="job-list"], [class*="search-result"], main, '
                    + '#app, body'
                );
                return (el || document.body).outerHTML.slice(0, 8000);
            }""")
            if not dom_text or len(dom_text) < 100:
                return []
        except Exception:
            return []

        sel_map = await self._llm.generate_selectors(dom_text, name)
        list_sel = sel_map.get("list_selector", "")
        if not list_sel:
            self.log(f"  [{name}] LLM 未返回有效 list_selector")
            return []

        self.log(f"  [{name}] LLM 生成选择器: list={list_sel}")

        sel_json = json.dumps(sel_map, ensure_ascii=False)
        jobs_data = await self.eval_js(page, f"""() => {{
            const S = {sel_json};
            const cards = document.querySelectorAll(S.list_selector);
            if (!cards.length) return [];
            return Array.from(cards).slice(0, 60).map(card => {{
                const txt = (sel) => {{
                    if (!sel) return '';
                    const el = card.querySelector(sel);
                    return el ? el.textContent.trim() : '';
                }};
                const href = (sel) => {{
                    if (!sel) return '';
                    const el = card.querySelector(sel);
                    return el ? (el.href || el.getAttribute('href') || '') : '';
                }};
                return {{
                    job_title: txt(S.title),
                    salary: txt(S.salary),
                    company_name: txt(S.company),
                    city: txt(S.city),
                    experience: txt(S.experience),
                    education: txt(S.education),
                    job_url: href(S.job_url),
                    publish_time: txt(S.publish_time),
                    tags: S.tags ? Array.from(
                        card.querySelectorAll(S.tags)
                    ).map(t => t.textContent.trim()).filter(Boolean) : [],
                }};
            }});
        }}""")
        if not jobs_data:
            self.log(f"  [{name}] LLM 选择器未匹配到任何卡片")
            return []

        jobs = []
        for d in jobs_data:
            title = d.get("job_title", "")
            company = d.get("company_name", "")
            if not title or not company:
                continue
            salary_min, salary_max, salary_type = self.parse_salary(
                d.get("salary", "")
            )
            job_url = d.get("job_url", "")
            job_id = (
                hashlib.md5((job_url or f"{company}_{title}").encode()).hexdigest()[:16]
            )
            jobs.append(JobInfo(
                job_id=f"{self.platform_key}_{job_id}",
                platform=self.platform_key,
                platform_name=name,
                job_url=job_url,
                job_title=title,
                company_name=company,
                salary_range=d.get("salary") or None,
                salary_min=salary_min, salary_max=salary_max,
                salary_type=salary_type,
                city=d.get("city") or params.city,
                experience_required=d.get("experience") or None,
                education_required=d.get("education") or None,
                tags=d.get("tags") or [],
            ))
        self.log(f"  [{name}] LLM 降级提取到 {len(jobs)} 条")
        return jobs

    # ──────── 提取质量诊断 ────────

    async def _diagnose_selectors(self, page) -> str:
        """容器找到但 0 条结果时，逐一探测关键选择器并输出诊断报告"""
        name = self.platform_cfg["name"]
        lines = [f"[{name}] === 选择器诊断 ==="]

        probe_map = {
            "RESULT_CONTAINER": self.RESULT_CONTAINER_SELECTOR,
            "DIAGNOSE_CARD": self.DIAGNOSE_CARD_SELECTOR,
        }
        for sel_list in (self.SEARCH_INPUT_SELECTORS or []):
            probe_map[f"SEARCH_INPUT({sel_list[:40]})"] = sel_list
        for sel_list in (self.SEARCH_BUTTON_SELECTORS or []):
            probe_map[f"SEARCH_BTN({sel_list[:40]})"] = sel_list

        for label, sel in probe_map.items():
            if not sel:
                continue
            try:
                count = await self.eval_js(page, f"""() => {{
                    return document.querySelectorAll({repr(sel)}).length;
                }}""")
                status = f"matched={count}" if count else "MISS"
                lines.append(f"  {label}: {sel[:60]} → {status}")
            except Exception as e:
                lines.append(f"  {label}: {sel[:60]} → ERROR({e})")

        url = await self.engine.current_url(page)
        lines.append(f"  URL: {url[:120]}")

        title = await self.eval_js(page, "() => document.title") or ""
        lines.append(f"  Title: {title[:80]}")

        body_len = await self.eval_js(page, "() => document.body?.innerText?.length || 0") or 0
        lines.append(f"  Body text length: {body_len}")

        report = "\n".join(lines)
        self.log(report)
        return report

    def _log_extraction_quality(self, jobs: list):
        n = len(jobs)
        if not n:
            return
        name = self.platform_cfg["name"]
        has_city = sum(1 for j in jobs if j.city)
        has_edu = sum(1 for j in jobs if j.education_required)
        has_exp = sum(1 for j in jobs if j.experience_required)
        has_sal = sum(1 for j in jobs if j.salary_min is not None)
        self.log(f"  [{name}] 提取质量: {n}条 | "
              f"城市={has_city}/{n} 学历={has_edu}/{n} "
              f"经验={has_exp}/{n} 薪资={has_sal}/{n}")

    # ──────── 进度汇报 ────────

    def emit_progress(self, count, status: str = "working"):
        name = self.platform_cfg["name"]
        if status == "done":
            self.log(f"  [{name}] 完成，获取 {count} 条")
        elif status == "error":
            self.log(f"  [{name}] 出错: {count}")
        elif status == "blocked":
            self.log(f"  [{name}] ⏸ 风控暂停，等待人工处理/冷却结束")
        else:
            self.log(f"  [{name}] 正在抓取... 已获取 {count} 条")
        if self._progress_cb:
            try:
                self._progress_cb(self.platform_key, count, status)
            except Exception:
                pass
