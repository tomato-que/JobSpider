"""投递混入 — 招呼语生成、按钮查找 JS 构建器、单条/批量投递编排

从 base.py 拆出。保持方法签名、签名顺序、函数体与原实现一致。
"""
import asyncio
import json
import os

from models.job import SearchParams, JobInfo, ApplyOutcome
from config import gaussian_delay
from core import db as _db
from core.risk_guard import RiskSignal


def js_literal(value) -> str:
    """把 Python 值安全地嵌入 JS 表达式（含引号/撇号/反斜杠/换行/中文）。

    不要在 JS 里再套一层单引号：json.dumps 已经带了双引号，
    再塞进 '...' 会让英文撇号变成语法错误。
    """
    return json.dumps(value, ensure_ascii=False)


class ApplyMixin:
    """投递相关方法集合 (供 BaseAgent 继承)。"""

    # ──────── 投递：获取完整 JD ────────

    async def _fetch_jd_text(self, job: JobInfo) -> str:
        """打开职位详情页提取完整JD文本，然后关闭标签页。

        用于投递前让 LLM 读取完整 JD 生成个性化打招呼语。
        """
        if not job.job_url:
            return ""
        detail_page = None
        name = self.platform_cfg["name"]
        try:
            detail_page = await self.fetch_page(job.job_url, network_idle=True)
            await asyncio.sleep(gaussian_delay(1.0, 2.0))
            jd_text = await self.eval_js(detail_page, """() => {
                const sels = [
                    '.job-sec-text', '.job-detail__content', '.job_msg',
                    '.bmsg', '.describtion', '.job-intro-container',
                    '.job-description', '.job-detail', '.position-content',
                    '.des', '[class*="job-desc"]', '[class*="job-detail"]',
                ];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim().length > 20) {
                        return el.textContent.trim().slice(0, 2000);
                    }
                }
                const main = document.querySelector('main, .main, .content, article');
                if (main) return main.textContent.trim().slice(0, 2000);
                return '';
            }""")
            if jd_text:
                print(f"  [{name}] 获取JD: {len(jd_text)}字", flush=True)
            return jd_text or ""
        except Exception as e:
            print(f"  [{name}] 获取JD失败: {e}", flush=True)
            return ""
        finally:
            if detail_page:
                try:
                    await detail_page.close()
                except Exception:
                    pass

    # ──────── 投递：生成打招呼语 ────────

    def _greeting_cache_key(self, job: JobInfo) -> tuple:
        """招呼语缓存 key；简历摘要变化时自动失效。"""
        from core.resume_store import get_resume_version
        return (
            self.platform_key,
            job.job_id or f"{job.company_name}|{job.job_title}",
            get_resume_version(),
        )

    async def generate_greeting(self, job: JobInfo, jd_text: str,
                                params: SearchParams) -> str:
        """生成打招呼语：优先 LLM（每个岗位个性化），无 LLM 时回退到模板。

        params.greeting_template 不是直接的招呼语，而是给 LLM 的"风格提示"。
        只有当 LLM 不可用时，才把它当字面招呼语用。
        如果 LLM 界面已上传简历，会把简历摘要一并交给 LLM。
        """
        cache_key = self._greeting_cache_key(job)
        cached = self._greeting_cache.get(cache_key)
        if cached:
            return cached

        if self._llm:
            try:
                from core.resume_store import get_profile_text
                resume_text = get_profile_text()
                greeting = await asyncio.wait_for(
                    self._llm.generate_greeting(
                        job.job_title, job.company_name, jd_text or "",
                        user_template=params.greeting_template,
                        resume_text=resume_text,
                    ),
                    timeout=12.0,
                )
                if greeting and len(greeting.strip()) > 5:
                    self._greeting_cache[cache_key] = greeting
                    return greeting
            except asyncio.TimeoutError:
                print(f"  [{self.platform_cfg['name']}] LLM 生成招呼超时(>12s)，回退模板",
                      flush=True)
            except Exception as e:
                print(f"  [{self.platform_cfg['name']}] LLM 生成招呼失败: {e}",
                      flush=True)

        # LLM 不可用：优先用用户的模板，再回退到默认模板
        if params.greeting_template:
            greeting = params.greeting_template
        else:
            from core.llm_extractor import LLMExtractor
            greeting = LLMExtractor._fallback_greeting(
                job.job_title, job.company_name)
        self._greeting_cache[cache_key] = greeting
        return greeting

    # ──────── 投递：准备（JD / 规则 / 缓存） ────────

    def _should_fetch_jd(self, params: SearchParams) -> bool:
        """auto=有 LLM 才抓 JD；always=总是；never=完全不额外开详情页。"""
        mode = os.getenv("JOBSPIDER_JD_FETCH", "auto").strip().lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        return bool(self._llm)

    async def _prepare_job(self, job: JobInfo, params: SearchParams) -> str:
        """预取：JD（可选）+ 补充字段 + 招呼语。结果按 job 缓存。"""
        cache_key = self._greeting_cache_key(job)
        cached = self._greeting_cache.get(cache_key)
        if cached:
            return cached

        jd_text = (job.job_description or "").strip()
        if self._should_fetch_jd(params):
            full_jd = await self._fetch_jd_text(job)
            if full_jd:
                job.job_description = full_jd
                jd_text = full_jd

        supplements = []
        if job.salary_range:
            supplements.append(f"薪资: {job.salary_range}")
        if job.city:
            supplements.append(f"城市: {job.city}")
        if job.experience_required:
            supplements.append(f"经验: {job.experience_required}")
        if job.education_required:
            supplements.append(f"学历: {job.education_required}")
        if job.tags:
            supplements.append(f"标签: {', '.join(job.tags[:8])}")
        if job.company_industry:
            supplements.append(f"行业: {job.company_industry}")
        if job.company_size:
            supplements.append(f"规模: {job.company_size}")
        if supplements:
            extra = " | ".join(supplements)
            jd_text = f"{jd_text}\n\n{extra}" if jd_text else extra
        if not jd_text:
            jd_text = job.job_title

        return await self.generate_greeting(job, jd_text, params)

    async def _rule_skip(self, job: JobInfo, params: SearchParams) -> tuple:
        """LLM 规则判断，按 (公司,标题,薪资) 缓存，超时放行。"""
        if not params.exclude_rules or not self._llm:
            return False, ""
        key = (job.company_name or "", job.job_title or "",
               job.salary_range or "")
        if key in self._rule_cache:
            return self._rule_cache[key]

        brief = (
            f"岗位: {job.job_title} | 公司: {job.company_name} | "
            f"薪资: {job.salary_range or '-'} | "
            f"城市: {job.city or '-'} | "
            f"经验: {job.experience_required or '-'} | "
            f"学历: {job.education_required or '-'} | "
            f"标签: {', '.join(job.tags[:8]) if job.tags else '-'} | "
            f"行业: {job.company_industry or '-'} | "
            f"规模: {job.company_size or '-'}"
        )
        try:
            result = await asyncio.wait_for(
                self._llm.should_skip_by_rule(brief, params.exclude_rules),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            print(f"  [{self.platform_cfg['name']}] LLM 规则判断超时(>8s) — 放行: "
                  f"{job.job_title}", flush=True)
            result = (False, "")
        except Exception as e:
            print(f"  [{self.platform_cfg['name']}] LLM 规则判断失败 (放行): {e}",
                  flush=True)
            result = (False, "")
        self._rule_cache[key] = result
        return result

    # ──────── 投递：任务级台账 ────────

    def _reg_is_applied(self, job: JobInfo) -> bool:
        if not self._registry or not job.job_id:
            return False
        return self._registry.is_applied(self.platform_key, job.job_id)

    def _reg_is_uncertain(self, job: JobInfo) -> bool:
        if not self._registry or not job.job_id:
            return False
        return self._registry.is_uncertain(self.platform_key, job.job_id)

    def _reg_company_count(self, company: str) -> int:
        if not self._registry:
            return 0
        return self._registry.company_count(company)

    def _reg_failed_count(self, job: JobInfo) -> int:
        if not self._registry or not job.job_id:
            return 0
        return self._registry.failed_count(self.platform_key, job.job_id)

    def _reg_record(self, job: JobInfo, status: str):
        if not self._registry or not job.job_id:
            return
        if status == "applied":
            self._registry.record_applied(
                self.platform_key, job.job_id, job.company_name or "")
        elif status == "uncertain":
            self._registry.record_uncertain(self.platform_key, job.job_id)
        elif status == "failed":
            self._registry.record_failed(self.platform_key, job.job_id)

    # ──────── 投递：按钮查找 JS 构建器 ────────

    def build_card_button_finder(
        self, job: JobInfo, card_selectors: str,
        title_selectors: str, company_selectors: str,
        button_selectors: str, button_texts: list,
    ) -> str:
        """构建 human_click 用的 find_js。

        先按 job_id / job_url 精确匹配卡片（同公司同标题多张卡时不会点错），
        找不到再用标题+公司前缀兜底。按钮已是“已沟通/已投递”时返回
        reason='already'，由调用方记 skipped。
        """
        return f"""() => {{
            const JOB_ID = {js_literal(job.job_id or '')};
            const JOB_URL = {js_literal(job.job_url or '')};
            const TITLE = {js_literal((job.job_title or '')[:20])};
            const COMPANY = {js_literal((job.company_name or '')[:20])};
            const TEXTS = {json.dumps(list(button_texts), ensure_ascii=False)};
            const DONE_TEXTS = ['继续沟通', '已沟通', '已投递', '已申请', '已发送', '沟通中'];
            const cards = Array.from(document.querySelectorAll({card_selectors!r}));
            const norm = (u) => {{
                try {{ return new URL(u, location.href).pathname; }} catch (e) {{ return ''; }}
            }};
            const jobPath = JOB_URL ? norm(JOB_URL) : '';
            const titleOf = (c) => (c.querySelector({title_selectors!r})?.textContent || '').trim();
            const companyOf = (c) => (c.querySelector({company_selectors!r})?.textContent || '').trim();

            const idMatch = (card) => {{
                if (!JOB_ID && !jobPath) return false;
                for (const a of card.querySelectorAll('a[href]')) {{
                    const h = a.getAttribute('href') || '';
                    if (JOB_ID && h.includes(JOB_ID)) return true;
                    if (jobPath && norm(h) === jobPath) return true;
                }}
                for (const attr of ['data-jobid', 'data-job-id', 'data-jid', 'data-encrypt-job-id']) {{
                    const v = card.getAttribute(attr);
                    if (JOB_ID && v && String(v).includes(JOB_ID)) return true;
                }}
                return false;
            }};
            const textMatch = (card) => {{
                if (!TITLE || !COMPANY) return false;
                return titleOf(card).includes(TITLE) && companyOf(card).includes(COMPANY);
            }};

            let pool = cards.filter(idMatch);
            if (!pool.length) pool = cards.filter(textMatch);
            if (!pool.length) return {{ found: false, reason: 'no_card' }};

            for (const card of pool) {{
                let btn = card.querySelector({button_selectors!r});
                if (!btn) {{
                    for (const el of card.querySelectorAll('a, button, span, div')) {{
                        const t = (el.textContent || '').trim();
                        if (TEXTS.includes(t)
                            && el.offsetParent !== null
                            && el.offsetHeight > 0 && el.offsetHeight < 80) {{
                            btn = el; break;
                        }}
                    }}
                }}
                if (!btn) return {{ found: false, reason: 'no_btn',
                                    title: titleOf(card), company: companyOf(card) }};
                const btnText = (btn.textContent || '').trim();
                if (DONE_TEXTS.includes(btnText)) {{
                    return {{ found: false, reason: 'already',
                              title: titleOf(card), company: companyOf(card) }};
                }}
                btn.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                if (btn.tagName === 'A' && btn.getAttribute('target')) {{
                    btn.removeAttribute('target');
                }}
                let p = btn.parentElement;
                while (p && p !== card) {{
                    if (p.tagName === 'A' && p.getAttribute('target')) {{
                        p.removeAttribute('target');
                    }}
                    p = p.parentElement;
                }}
                const r = btn.getBoundingClientRect();
                return {{
                    found: true, x: r.left, y: r.top, w: r.width, h: r.height,
                    text: btnText.slice(0, 20),
                    title: titleOf(card), company: companyOf(card),
                }};
            }}
            return {{ found: false, reason: 'no_btn' }};
        }}"""

    def build_modal_button_finder(
        self, button_selectors: str, button_texts: list,
    ) -> str:
        """构建 human_click 用的 find_js：优先在可见弹窗内找确认/发送按钮，
        再回退到全文档。"""
        return f"""() => {{
            const TEXTS = {json.dumps(list(button_texts), ensure_ascii=False)};
            const scopes = [];
            for (const sel of ['.ant-modal', '.ant-drawer', '.el-dialog',
                               '.el-message-box', '[role="dialog"]',
                               '.zppp-modal', '.dialog-content', '.modal']) {{
                for (const el of document.querySelectorAll(sel)) {{
                    if (el.offsetParent !== null
                        && el.getBoundingClientRect().height > 30) {{
                        scopes.push(el);
                    }}
                }}
            }}
            scopes.push(document);
            for (const scope of scopes) {{
                let btn = scope.querySelector({button_selectors!r});
                if (!btn) {{
                    for (const el of scope.querySelectorAll('button, a, span, div')) {{
                        const t = (el.textContent || '').trim();
                        if (TEXTS.includes(t)
                            && el.offsetParent !== null
                            && el.offsetHeight > 0 && el.offsetHeight < 80) {{
                            btn = el; break;
                        }}
                    }}
                }}
                if (!btn) continue;
                btn.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                const r = btn.getBoundingClientRect();
                return {{
                    found: true, x: r.left, y: r.top, w: r.width, h: r.height,
                    text: (btn.textContent || '').trim().slice(0, 20),
                }};
            }}
            return {{ found: false, reason: 'no_confirm' }};
        }}"""

    # ──────── 投递：点击投递按钮（子类可覆盖）────────

    async def apply_to_job(self, page, job: JobInfo, greeting: str,
                           params: SearchParams) -> ApplyOutcome:
        """在当前页面上点击指定职位的投递按钮 — 子类实现具体逻辑"""
        return ApplyOutcome.FAILED

    # ──────── 投递：批量处理当前页 ────────

    async def apply_to_page_jobs(self, page, jobs: list[JobInfo],
                                 params: SearchParams) -> int:
        """对当前页面的职位列表执行投递，返回成功投递数。

        结果三态：SUCCESS -> applied；UNCERTAIN -> uncertain；
        FAILED -> failed；BLOCKED -> 停本平台。UNCERTAIN 不计 applied、
        不占 24h 配额、不自动重投。
        """
        if not params.auto_apply:
            print(f"  [{self.platform_cfg['name']}] auto_apply=False，跳过投递",
                  flush=True)
            return 0

        name = self.platform_cfg["name"]

        # ── 工作时间窗 ──
        try:
            from config import is_within_active_hours
            active_hours = getattr(params, "active_hours", None)
            if active_hours and not is_within_active_hours(active_hours):
                from datetime import datetime as _dt
                h = _dt.now().hour
                self.log(f"  [{name}] ⏸ 工作时间外暂停 "
                         f"(now={h}h, window={active_hours[0]}-{active_hours[1]}h)")
                for job in jobs:
                    job.apply_status = "skipped"
                    job.apply_skip_reason = "工作时间外"
                    self.emit_job(job, "update")
                self._platform_done = True
                return 0
        except Exception as _e:
            print(f"  [active_hours check] {_e}", flush=True)

        if not hasattr(self, "_applied_total"):
            self._applied_total = 0
        if not hasattr(self, "_applied_burst_count"):
            self._applied_burst_count = 0

        # 平台任务级上限：None=不限；0=本任务不投
        platform_limit = None
        if params.platform_limits and self.platform_key in params.platform_limits:
            raw = params.platform_limits[self.platform_key]
            if raw is not None:
                try:
                    platform_limit = max(0, int(raw))
                except (TypeError, ValueError):
                    platform_limit = None

        _RATE_DELAY = {
            "slow":   (8.0, 15.0),
            "normal": (4.0, 8.0),
            "fast":   (3.0, 5.0),
            "turbo":  (3.0, 4.0),
        }
        rate_lo, rate_hi = _RATE_DELAY.get(
            (params.apply_rate or "normal").lower(), (4.0, 8.0)
        )

        applied_count = 0
        skipped_count = 0
        failed_count = 0
        uncertain_count = 0
        rate_label = (params.apply_rate or "normal").lower()

        # ── 24h 日上限：用户值优先；留空用平台硬顶；显式 0 表示不投 ──
        from config import PLATFORM_DAILY_HARD_CAP
        daily_caps = getattr(params, "daily_limit_per_platform", {}) or {}
        raw_daily = daily_caps.get(self.platform_key) if isinstance(daily_caps, dict) else None
        if raw_daily is not None:
            try:
                daily_cap_for_me = max(0, int(raw_daily))
            except (TypeError, ValueError):
                daily_cap_for_me = PLATFORM_DAILY_HARD_CAP.get(self.platform_key)
        else:
            daily_cap_for_me = PLATFORM_DAILY_HARD_CAP.get(self.platform_key)

        if self._registry:
            db_applied_today = self._registry.applied_count(self.platform_key)
        else:
            try:
                db_applied_today = await _db.count_applied_in_window(
                    self.platform_key, 24)
            except Exception as _e:
                print(f"  [daily_cap fetch] {_e}", flush=True)
                db_applied_today = 0

        if platform_limit:
            remain = max(0, platform_limit - self._applied_total)
            self.log(f"  [{name}] 开始投递本页 {len(jobs)} 个职位 "
                     f"(本平台已投 {self._applied_total}/{platform_limit}, 剩余 {remain}, "
                     f"速率={rate_label} {rate_lo:.0f}-{rate_hi:.0f}s, "
                     f"24h DB {db_applied_today}/{daily_cap_for_me})...")
        else:
            self.log(f"  [{name}] 开始投递本页 {len(jobs)} 个职位 "
                     f"(速率={rate_label} {rate_lo:.0f}-{rate_hi:.0f}s, "
                     f"24h DB {db_applied_today}/{daily_cap_for_me})...")

        if daily_cap_for_me is not None and db_applied_today >= daily_cap_for_me:
            reason = (f"今日累计已达日上限(DB {db_applied_today}/"
                      f"{daily_cap_for_me} · 24h)")
            self.log(f"  [{name}] ⏸ {reason}，停止本平台后续投递")
            for j in jobs[:5]:
                j.apply_status = "skipped"
                j.apply_skip_reason = reason
                self.emit_job(j, "update")
            self._platform_done = True
            return 0

        burst_after = getattr(params, "burst_pause_after", None)
        burst_minutes = getattr(params, "burst_pause_minutes", (30, 90))
        max_per_company = getattr(params, "max_per_company_per_day", None)
        dry_run = bool(getattr(params, "dry_run", False))
        skip_applied = bool(getattr(params, "skip_applied", True))

        def _next_prefetchable(start_idx):
            """下一个可能真正投递的岗位（只做静态/台账判断，不查 DB）。"""
            for j in jobs[start_idx:]:
                matched, _why = self.job_matches_filters(j, params)
                if not matched:
                    continue
                if skip_applied and (self._reg_is_applied(j)
                                     or self._reg_is_uncertain(j)):
                    continue
                if self._reg_failed_count(j) >= 2:
                    continue
                if (max_per_company and j.company_name
                        and self._reg_company_count(j.company_name) >= max_per_company):
                    continue
                return j
            return None

        prep_job = None
        prep_task = None
        should_stop_fn = getattr(self, "should_stop", None)
        try:
            for idx, job in enumerate(jobs):
                if callable(should_stop_fn):
                    try:
                        if should_stop_fn():
                            job.apply_status = "skipped"
                            job.apply_skip_reason = "用户停止"
                            skipped_count += 1
                            self.emit_job(job, "update")
                            break
                    except Exception:
                        pass

                current_daily = (self._registry.applied_count(self.platform_key)
                                 if self._registry
                                 else db_applied_today + applied_count)
                hit_platform = (platform_limit is not None
                                and self._applied_total >= platform_limit)
                hit_daily = (daily_cap_for_me is not None
                             and current_daily >= daily_cap_for_me)
                if hit_platform or hit_daily:
                    reason = (
                        f"本任务平台投递上限({self._applied_total}/{platform_limit})"
                        if hit_platform else
                        f"今日累计已达日上限(DB {current_daily}/{daily_cap_for_me} · 24h)"
                    )
                    self.log(f"  [{name}] ⏸ {reason}，停止本平台后续投递")
                    job.apply_status = "skipped"
                    job.apply_skip_reason = reason
                    skipped_count += 1
                    self.emit_job(job, "update")
                    self._platform_done = True
                    break

                matched, why = self.job_matches_filters(job, params)
                if not matched:
                    job.apply_status = "skipped"
                    job.apply_skip_reason = why
                    skipped_count += 1
                    self.emit_job(job, "update")
                    self.log(f"  [{name}] ⊘ 筛选跳过: {job.job_title} @ "
                             f"{job.company_name} ({why})")
                    continue

                if max_per_company and job.company_name:
                    try:
                        if self._registry:
                            co_count = self._reg_company_count(job.company_name)
                        else:
                            co_count = await _db.count_company_applied_today(
                                job.company_name, 24)
                        if co_count >= max_per_company:
                            job.apply_status = "skipped"
                            job.apply_skip_reason = f"同公司今日已投{co_count}次"
                            skipped_count += 1
                            self.emit_job(job, "update")
                            self.log(f"  [{name}] ⊘ 同公司限额: {job.job_title} @ "
                                     f"{job.company_name}")
                            continue
                    except Exception as _e:
                        print(f"  [max_per_company] {_e}", flush=True)

                if skip_applied and job.job_id:
                    try:
                        if self._registry:
                            already = self._reg_is_applied(job)
                            pending = self._reg_is_uncertain(job)
                        else:
                            already = await _db.has_applied_before(
                                self.platform_key, job.job_id, 168)
                            pending = await _db.has_uncertain_before(
                                self.platform_key, job.job_id, 24)
                        if already or pending:
                            job.apply_status = "skipped"
                            job.apply_skip_reason = (
                                "历史已投(7天内)" if already
                                else "[未确认] 24h内待确认"
                            )
                            skipped_count += 1
                            self.emit_job(job, "update")
                            self.log(f"  [{name}] ⊘ {job.apply_skip_reason}: "
                                     f"{job.job_title} @ {job.company_name}")
                            continue
                        if self._reg_failed_count(job) >= 2:
                            job.apply_status = "skipped"
                            job.apply_skip_reason = "本任务已失败2次"
                            skipped_count += 1
                            self.emit_job(job, "update")
                            continue
                    except Exception as _e:
                        print(f"  [skip_applied] {_e}", flush=True)

                should_skip, rule_reason = await self._rule_skip(job, params)
                if should_skip:
                    job.apply_status = "skipped"
                    job.apply_skip_reason = f"规则: {rule_reason or '匹配排除规则'}"
                    skipped_count += 1
                    self.emit_job(job, "update")
                    self.log(f"  [{name}] ⊘ 规则跳过: {job.job_title} @ "
                             f"{job.company_name} ({rule_reason})")
                    continue

                # 全链路风控检查（每条投递前）
                if await self._check_anti_bot(page):
                    job.apply_status = "skipped"
                    job.apply_skip_reason = "[风控] 页面触发风控，停止本平台"
                    skipped_count += 1
                    self.emit_job(job, "update")
                    break

                if dry_run:
                    greeting_dr = await self.generate_greeting(
                        job, job.job_title or "", params)
                    job.apply_status = "dry_run"
                    job.greeting_sent = greeting_dr
                    job.applied = False
                    self._applied_total += 1
                    applied_count += 1
                    self.log(f"  [{name}] ◐ dry_run ({self._applied_total}): "
                             f"{job.job_title} @ {job.company_name}")
                    self.emit_job(job, "update")
                    if platform_limit is not None and self._applied_total >= platform_limit:
                        self.log(f"  [{name}] dry_run 已达上限 {platform_limit}，停止")
                        break
                    await asyncio.sleep(gaussian_delay(rate_lo, rate_hi))
                    continue

                # 招呼语：优先使用预取结果
                if prep_job is job and prep_task is not None:
                    try:
                        greeting = await prep_task
                    except Exception as e:
                        print(f"  [{name}] 预取失败，改为同步生成: {e}", flush=True)
                        greeting = await self._prepare_job(job, params)
                    prep_job = None
                    prep_task = None
                else:
                    greeting = await self._prepare_job(job, params)

                preview = (greeting or "")[:50].replace("\n", " ")
                self.log(f"  [{name}] 招呼语: {preview}"
                         f"{'...' if len(greeting or '') > 50 else ''}")

                self._apply_detail = None
                sem = getattr(self, "_apply_sem", None)
                if sem is not None:
                    async with sem:
                        outcome = await self.apply_to_job(
                            page, job, greeting, params)
                else:
                    outcome = await self.apply_to_job(
                        page, job, greeting, params)
                detail = getattr(self, "_apply_detail", "") or ""

                if outcome == ApplyOutcome.SUCCESS:
                    job.applied = True
                    job.apply_status = "applied"
                    job.greeting_sent = greeting
                    applied_count += 1
                    self._applied_total += 1
                    self._applied_burst_count += 1
                    self.risk.note_apply_success(self.platform_key)
                    self._reg_record(job, "applied")
                    self.log(f"  [{name}] ✓ applied ({self._applied_total}): "
                             f"{job.job_title} @ {job.company_name}")
                    self.emit_job(job, "update")
                    if platform_limit is not None and self._applied_total >= platform_limit:
                        self.log(f"  [{name}] 已达平台投递上限 {platform_limit}，停止投递")
                        break
                    if burst_after and self._applied_burst_count >= int(burst_after):
                        try:
                            from config import gaussian_minutes
                            lo, hi = (burst_minutes
                                      if isinstance(burst_minutes, (tuple, list))
                                      and len(burst_minutes) == 2 else (30, 90))
                            pause_min = gaussian_minutes(float(lo), float(hi))
                            self.log(f"  [{name}] ⏳ Burst pause: 已投 "
                                     f"{self._applied_burst_count} 条，"
                                     f"长休 {pause_min:.1f} 分钟（防封号）")
                            self._applied_burst_count = 0
                            await asyncio.sleep(pause_min * 60)
                        except Exception as _e:
                            print(f"  [burst_pause] {_e}", flush=True)

                elif outcome == ApplyOutcome.ALREADY:
                    job.apply_status = "skipped"
                    job.apply_skip_reason = "[状态] 平台已沟通"
                    skipped_count += 1
                    self.emit_job(job, "update")
                    self.log(f"  [{name}] ⊘ 平台已沟通: {job.job_title} @ "
                             f"{job.company_name}")

                elif outcome == ApplyOutcome.UNCERTAIN:
                    job.apply_status = "uncertain"
                    job.greeting_sent = greeting
                    job.apply_skip_reason = (
                        f"[未确认] {detail or '已点击但未拿到明确回执'}")
                    uncertain_count += 1
                    self._reg_record(job, "uncertain")
                    self.emit_job(job, "update")
                    self.log(f"  [{name}] ⚠ 待确认: {job.job_title} @ "
                             f"{job.company_name}（{job.apply_skip_reason}）")

                elif outcome == ApplyOutcome.BLOCKED:
                    job.apply_status = "skipped"
                    job.apply_skip_reason = f"[风控] {detail or '触发风控'}"
                    skipped_count += 1
                    self.emit_job(job, "update")
                    self._platform_done = True
                    self.log(f"  [{name}] 🚨 风控阻断，停止本平台: "
                             f"{job.job_title} @ {job.company_name}")
                    break

                else:  # FAILED
                    job.apply_status = "failed"
                    prefix = detail if detail else "投递失败"
                    if not prefix.startswith("["):
                        prefix = f"[环境] {prefix}"
                    job.apply_skip_reason = prefix
                    failed_count += 1
                    self._reg_record(job, "failed")
                    self.emit_job(job, "update")
                    self.log(f"  [{name}] ✗ apply_failed: {job.job_title} @ "
                             f"{job.company_name}（{prefix}）")
                    # 下架/状态类失败不是 bot 信号，不累计冷却
                    if not (prefix.startswith("[下架]") or prefix.startswith("[状态]")):
                        n = self.risk.note_apply_failure(self.platform_key)
                        if n >= 3:
                            until = await self.risk.trip(
                                self.platform_key, RiskSignal.RATE_LIMITED,
                                "连续投递失败")
                            self.log(f"  [{name}] ⚠ 连续 {n} 次投递失败，"
                                     f"冷却至 {until}")
                            self._platform_done = True
                            break

                # 预取下一单：与最后的 rate sleep 重叠
                nxt = _next_prefetchable(idx + 1)
                if nxt is not None:
                    prep_job = nxt
                    prep_task = asyncio.create_task(
                        self._prepare_job(nxt, params))

                await asyncio.sleep(gaussian_delay(rate_lo, rate_hi))

        except Exception as e:
            self.log(f"  [{name}] 投递循环异常: {e}")
        finally:
            if prep_task is not None and not prep_task.done():
                prep_task.cancel()
                try:
                    await prep_task
                except Exception:
                    pass
            self._apply_detail = None

        self.log(f"  [{name}] 本页投递完成: 成功 {applied_count}, "
                 f"待确认 {uncertain_count}, 跳过 {skipped_count}, "
                 f"失败 {failed_count}")
        return applied_count
