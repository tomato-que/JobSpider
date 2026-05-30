"""投递混入 — 招呼语生成、按钮查找 JS 构建器、单条/批量投递编排

从 base.py 拆出。保持方法签名、签名顺序、函数体与原实现一致。
"""
import asyncio
import json

from models.job import SearchParams, JobInfo
from config import gaussian_delay


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

    async def generate_greeting(self, job: JobInfo, jd_text: str,
                                params: SearchParams) -> str:
        """生成打招呼语：优先 LLM（每个岗位个性化），无 LLM 时回退到模板。

        params.greeting_template 不是直接的招呼语，而是给 LLM 的"风格提示"。
        只有当 LLM 不可用时，才把它当字面招呼语用。
        """
        if self._llm:
            try:
                greeting = await asyncio.wait_for(
                    self._llm.generate_greeting(
                        job.job_title, job.company_name, jd_text or "",
                        user_template=params.greeting_template,
                    ),
                    timeout=20.0,
                )
                if greeting and len(greeting.strip()) > 5:
                    return greeting
            except asyncio.TimeoutError:
                print(f"  [{self.platform_cfg['name']}] LLM 生成招呼超时(>20s)，回退模板",
                      flush=True)
            except Exception as e:
                print(f"  [{self.platform_cfg['name']}] LLM 生成招呼失败: {e}",
                      flush=True)

        # LLM 不可用：优先用用户的模板，再回退到默认模板
        if params.greeting_template:
            return params.greeting_template

        from core.llm_extractor import LLMExtractor
        return LLMExtractor._fallback_greeting(job.job_title, job.company_name)

    # ──────── 投递：按钮查找 JS 构建器 ────────

    def build_card_button_finder(
        self, job: JobInfo, card_selectors: str,
        title_selectors: str, company_selectors: str,
        button_selectors: str, button_texts: list,
    ) -> str:
        """构建 human_click 用的 find_js：在卡片列表中匹配 job.title+company,
        然后定位投递/沟通按钮（优先选择器，回退按文本匹配），返回视口坐标。
        """
        safe_title = json.dumps(job.job_title[:20])[1:-1]
        safe_company = json.dumps(job.company_name[:20])[1:-1]
        texts_js = "[" + ", ".join(f"'{t}'" for t in button_texts) + "]"
        return f"""() => {{
            const cards = document.querySelectorAll({card_selectors!r});
            for (const card of cards) {{
                const title = card.querySelector({title_selectors!r})?.textContent?.trim() || '';
                const company = card.querySelector({company_selectors!r})?.textContent?.trim() || '';
                if (!title.includes('{safe_title}') || !company.includes('{safe_company}')) continue;
                let btn = card.querySelector({button_selectors!r});
                if (!btn) {{
                    const buttonTexts = {texts_js};
                    const els = card.querySelectorAll('a, button, span, div');
                    for (const el of els) {{
                        const t = (el.textContent || '').trim();
                        if (buttonTexts.includes(t)
                            && el.offsetParent !== null
                            && el.offsetHeight > 0 && el.offsetHeight < 80) {{
                            btn = el; break;
                        }}
                    }}
                }}
                if (!btn) return {{ found: false, reason: 'no_btn', title, company }};
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
                    text: (btn.textContent || '').trim().slice(0, 20),
                }};
            }}
            return {{ found: false, reason: 'no_card' }};
        }}"""

    def build_modal_button_finder(
        self, button_selectors: str, button_texts: list,
    ) -> str:
        """构建 human_click 用的 find_js：在全文档范围（弹窗/对话框）内
        查找确认 / 发送类按钮。"""
        texts_js = "[" + ", ".join(f"'{t}'" for t in button_texts) + "]"
        return f"""() => {{
            let btn = document.querySelector({button_selectors!r});
            if (!btn) {{
                const buttonTexts = {texts_js};
                const els = document.querySelectorAll('button, a, span, div');
                for (const el of els) {{
                    const t = (el.textContent || '').trim();
                    if (buttonTexts.includes(t)
                        && el.offsetParent !== null
                        && el.offsetHeight > 0 && el.offsetHeight < 80) {{
                        btn = el; break;
                    }}
                }}
            }}
            if (!btn) return {{ found: false, reason: 'no_confirm' }};
            btn.scrollIntoView({{ behavior: 'instant', block: 'center' }});
            const r = btn.getBoundingClientRect();
            return {{
                found: true, x: r.left, y: r.top, w: r.width, h: r.height,
                text: (btn.textContent || '').trim().slice(0, 20),
            }};
        }}"""

    # ──────── 投递：点击投递按钮（子类可覆盖）────────

    async def apply_to_job(self, page, job: JobInfo, greeting: str,
                           params: SearchParams) -> bool:
        """在当前页面上点击指定职位的投递按钮 — 子类实现具体逻辑"""
        return False

    # ──────── 投递：批量处理当前页 ────────

    async def apply_to_page_jobs(self, page, jobs: list[JobInfo],
                                 params: SearchParams) -> int:
        """对当前页面的职位列表执行投递，返回成功投递数"""
        if not params.auto_apply:
            print(f"  [{self.platform_cfg['name']}] auto_apply=False，跳过投递",
                  flush=True)
            return 0

        # ── Anti-ban guard: active_hours ──
        try:
            from config import is_within_active_hours
            active_hours = getattr(params, "active_hours", None)
            if active_hours and not is_within_active_hours(active_hours):
                from datetime import datetime as _dt
                h = _dt.now().hour
                self.log(f"  [{self.platform_cfg['name']}] ⏸ 工作时间外暂停 "
                         f"(now={h}h, window={active_hours[0]}-{active_hours[1]}h)")
                for job in jobs:
                    job.apply_status = "skipped"
                    job.apply_skip_reason = "工作时间外"
                    self.emit_job(job, "update")
                return 0
        except Exception as _e:
            print(f"  [active_hours check] {_e}", flush=True)

        # 平台投递上限（保存在实例上累计）
        if not hasattr(self, "_applied_total"):
            self._applied_total = 0
        # 仅 None 表示不限；0 或正整数都会被严格遵守（0 = 已用完，全部跳过）
        platform_limit = None
        if params.platform_limits and self.platform_key in params.platform_limits:
            raw = params.platform_limits[self.platform_key]
            if raw is not None:
                try:
                    platform_limit = max(0, int(raw))
                except (TypeError, ValueError):
                    platform_limit = None

        # 投递速率 → 每条投递后的 sleep 区间（秒）
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
        name = self.platform_cfg["name"]
        rate_label = (params.apply_rate or "normal").lower()
        if platform_limit:
            remain = platform_limit - self._applied_total
            self.log(f"  [{name}] 开始投递本页 {len(jobs)} 个职位 "
                  f"(本平台已投 {self._applied_total}/{platform_limit}, 剩余 {remain}, "
                  f"速率={rate_label} {rate_lo:.0f}-{rate_hi:.0f}s)...")
        else:
            self.log(f"  [{name}] 开始投递本页 {len(jobs)} 个职位 "
                  f"(速率={rate_label} {rate_lo:.0f}-{rate_hi:.0f}s)...")

        # ── Anti-ban guard: daily DB cap & burst init ──
        _db_applied_today = 0
        daily_caps = getattr(params, "daily_limit_per_platform", {}) or {}
        daily_cap_for_me = None
        try:
            raw_daily = daily_caps.get(self.platform_key) if isinstance(daily_caps, dict) else None
            if raw_daily is not None:
                daily_cap_for_me = max(0, int(raw_daily))
        except Exception:
            daily_cap_for_me = None

        if daily_cap_for_me is not None:
            try:
                from core import db as _db
                _db_applied_today = await _db.count_applied_in_window(self.platform_key, 24)
            except Exception as _e:
                print(f"  [daily_cap fetch] {_e}", flush=True)
                _db_applied_today = 0
            self.log(f"  [{name}] DB 历史: 24h 已投 {_db_applied_today}/{daily_cap_for_me}")
            if _db_applied_today >= daily_cap_for_me:
                reason = f"今日累计已达日上限(DB {_db_applied_today}/{daily_cap_for_me} · 24h)"
                self.log(f"  [{name}] ⏸ {reason}，停止本平台后续投递")
                # 标记前 N 条便于用户看到原因；其余 job 直接退出，不再污染统计
                MARK_LIMIT = 5
                for j in jobs[:MARK_LIMIT]:
                    j.apply_status = "skipped"
                    j.apply_skip_reason = reason
                    self.emit_job(j, "update")
                self._platform_done = True
                return 0

        # Burst & consecutive-fail counters (instance-level, persist across pages)
        if not hasattr(self, "_applied_burst_count"):
            self._applied_burst_count = 0
        if not hasattr(self, "_consecutive_apply_failures"):
            self._consecutive_apply_failures = 0

        burst_after = getattr(params, "burst_pause_after", None)
        burst_minutes = getattr(params, "burst_pause_minutes", (30, 90))
        max_per_company = getattr(params, "max_per_company_per_day", None)
        dry_run = bool(getattr(params, "dry_run", False))
        skip_applied = bool(getattr(params, "skip_applied", True))

        for job in jobs:
            # ── Stop responsiveness (top of loop) ──
            should_stop_fn = getattr(self, "should_stop", None)
            if callable(should_stop_fn):
                try:
                    if should_stop_fn():
                        job.apply_status = "skipped"
                        job.apply_skip_reason = "用户停止"
                        skipped_count += 1
                        self.emit_job(job, "update")
                        continue
                except Exception:
                    pass

            # 平台上限检查 — 命中即停止本平台后续页（不再继续标剩余 job 为 skipped）
            db_remaining = (daily_cap_for_me - _db_applied_today - applied_count) if daily_cap_for_me is not None else None
            hit_platform = platform_limit is not None and self._applied_total >= platform_limit
            hit_daily_db = db_remaining is not None and db_remaining <= 0
            if hit_platform or hit_daily_db:
                reason = (f"本任务平台投递上限({self._applied_total}/{platform_limit})" if hit_platform else
                          f"今日累计已达日上限(DB {_db_applied_today + applied_count}/{daily_cap_for_me} · 24h)")
                self.log(f"  [{name}] ⏸ {reason}，停止本平台后续投递")
                # 标记当前这条 job 为 skipped 以便用户看到原因，剩余 job 直接 break
                job.apply_status = "skipped"
                job.apply_skip_reason = reason
                skipped_count += 1
                self.emit_job(job, "update")
                # 设置平台终止标志，让外层 scrape() 翻页循环也能看到
                self._platform_done = True
                break

            matched, why = self.job_matches_filters(job, params)
            if not matched:
                job.apply_status = "skipped"
                job.apply_skip_reason = why
                skipped_count += 1
                self.emit_job(job, "update")
                self.log(f"  [{name}] ⊘ 筛选跳过: {job.job_title} @ {job.company_name} ({why})")
                continue

            # ── Anti-ban guard: max_per_company_per_day ──
            if max_per_company and job.company_name:
                try:
                    from core import db as _db
                    co_count = await _db.count_company_applied_today(job.company_name, 24)
                    if co_count >= max_per_company:
                        job.apply_status = "skipped"
                        job.apply_skip_reason = f"同公司今日已投{co_count}次"
                        skipped_count += 1
                        self.emit_job(job, "update")
                        self.log(f"  [{name}] ⊘ 同公司限额: {job.job_title} @ {job.company_name}")
                        continue
                except Exception as _e:
                    print(f"  [max_per_company] {_e}", flush=True)

            # ── Anti-ban guard: 跨任务去重（7天内真实投递过的同一岗位不再投，防重复打招呼）──
            if skip_applied and job.job_id:
                try:
                    from core import db as _db
                    if await _db.has_applied_before(self.platform_key, job.job_id, 168):
                        job.apply_status = "skipped"
                        job.apply_skip_reason = "历史已投(7天内)"
                        skipped_count += 1
                        self.emit_job(job, "update")
                        self.log(f"  [{name}] ⊘ 历史已投: {job.job_title} @ {job.company_name}")
                        continue
                except Exception as _e:
                    print(f"  [skip_applied] {_e}", flush=True)

            # LLM 规则筛选
            if params.exclude_rules and self._llm:
                try:
                    brief = (f"岗位: {job.job_title} | 公司: {job.company_name} | "
                             f"薪资: {job.salary_range or '-'} | "
                             f"城市: {job.city or '-'} | "
                             f"经验: {job.experience_required or '-'} | "
                             f"学历: {job.education_required or '-'} | "
                             f"标签: {', '.join(job.tags[:8]) if job.tags else '-'} | "
                             f"行业: {job.company_industry or '-'} | "
                             f"规模: {job.company_size or '-'}")
                    should_skip, reason = await asyncio.wait_for(
                        self._llm.should_skip_by_rule(
                            brief, params.exclude_rules,
                        ),
                        timeout=15.0,
                    )
                    if should_skip:
                        job.apply_status = "skipped"
                        job.apply_skip_reason = f"规则: {reason or '匹配排除规则'}"
                        skipped_count += 1
                        self.emit_job(job, "update")
                        self.log(f"  [{name}] ⊘ 规则跳过: {job.job_title} @ {job.company_name} ({reason})")
                        continue
                except asyncio.TimeoutError:
                    print(f"  [{name}] LLM 规则判断超时(>15s) — 放行: {job.job_title}",
                          flush=True)
                except Exception as e:
                    print(f"  [{name}] LLM 规则判断失败 (放行): {e}", flush=True)

            try:
                # ── Stop responsiveness (right before apply) ──
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

                # ── Dry-run short-circuit ──
                if dry_run:
                    greeting_dr = await self.generate_greeting(job, job.job_title or "", params)
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

                # 获取完整 JD：LLM 可用时打开详情页提取，否则跳过直接投递
                jd_text = ""
                if self._llm:
                    full_jd = await self._fetch_jd_text(job)
                    if full_jd:
                        job.job_description = full_jd
                        jd_text = full_jd
                    # 补充列表页的结构化信息作为上下文
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
                        jd_text = jd_text + "\n\n" + " | ".join(supplements) if jd_text else " | ".join(supplements)
                    if not jd_text:
                        jd_text = job.job_title

                greeting = await self.generate_greeting(job, jd_text, params)
                preview = (greeting or "")[:50].replace("\n", " ")
                self.log(f"  [{name}] 招呼语: {preview}{'...' if len(greeting or '') > 50 else ''}")
                ok = await self.apply_to_job(page, job, greeting, params)
                if ok:
                    job.applied = True
                    job.apply_status = "applied"
                    job.greeting_sent = greeting
                    applied_count += 1
                    self._applied_total += 1
                    self._applied_burst_count += 1
                    self._consecutive_apply_failures = 0
                    self.log(f"  [{name}] ✓ applied ({self._applied_total}): "
                          f"{job.job_title} @ {job.company_name}")
                    self.emit_job(job, "update")
                    if platform_limit is not None and self._applied_total >= platform_limit:
                        self.log(f"  [{name}] 已达平台投递上限 {platform_limit}，停止投递")
                        break
                    # Burst pause
                    if burst_after and self._applied_burst_count >= int(burst_after):
                        try:
                            from config import gaussian_minutes
                            lo, hi = burst_minutes if isinstance(burst_minutes, (tuple, list)) and len(burst_minutes) == 2 else (30, 90)
                            pause_min = gaussian_minutes(float(lo), float(hi))
                            self.log(f"  [{name}] ⏳ Burst pause: 已投 {self._applied_burst_count} 条，"
                                     f"长休 {pause_min:.1f} 分钟（防封号）")
                            self._applied_burst_count = 0
                            await asyncio.sleep(pause_min * 60)
                        except Exception as _e:
                            print(f"  [burst_pause] {_e}", flush=True)
                else:
                    job.apply_status = "failed"
                    failed_count += 1
                    self.log(f"  [{name}] ✗ apply_failed: {job.job_title} @ {job.company_name}")
                    self.emit_job(job, "update")
                    self._consecutive_apply_failures += 1
                    if self._consecutive_apply_failures >= 3:
                        self.log(f"  [{name}] ⚠ 连续 {self._consecutive_apply_failures} 次投递失败，触发冷却")
                        cooldown_fn = getattr(self, "_cooldown_after_failure", None)
                        if callable(cooldown_fn):
                            try:
                                await cooldown_fn(reason="apply_failed_streak")
                            except Exception as _e:
                                print(f"  [cooldown_after_failure] {_e}", flush=True)
                        break  # 停止本页投递
                await asyncio.sleep(gaussian_delay(rate_lo, rate_hi))
            except Exception as e:
                job.apply_status = "failed"
                failed_count += 1
                print(f"  [{name}] ✗ error: {job.job_title} - {e}", flush=True)
                self.emit_job(job, "update")

        self.log(f"  [{name}] 本页投递完成: 成功 {applied_count}, 跳过 {skipped_count}, 失败 {failed_count}")
        return applied_count
