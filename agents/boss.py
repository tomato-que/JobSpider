"""Boss直聘平台子智能体 — 浏览器交互版

流程：打开 zhipin.com → 选城市 → 输关键词 → 点搜索 → 提取职位 → 翻页
注意：Boss 反爬较强，首次使用需在 chrome_profile 中手动登录。
      probe 返回 about:blank 说明未登录时页面直接拦截。
"""
import asyncio
import hashlib
import json
import os
import random
import re

from models.job import SearchParams, JobInfo, ApplyOutcome
from agents.base import BaseAgent
from agents._apply import js_literal
from config import gaussian_delay
from core.risk_guard import RiskSignal


class BossAgent(BaseAgent):

    SEARCH_INPUT_SELECTORS = [
        '.search-input-box input',
        'input.ipt-search',
        'input[ka="header-home-search"]',
        'input[name="query"]',
        'input[placeholder*="搜索职位"]',
        'input[placeholder*="搜索"]',
    ]
    SEARCH_BUTTON_SELECTORS = [
        '.btn-search', '.search-btn',
        '[class*="search-btn"]', 'button[type="submit"]',
    ]
    RESULT_CONTAINER_SELECTOR = '.job-card-wrapper, .job-list-box, .search-job-result'
    DIAGNOSE_CARD_SELECTOR = '.job-card-wrapper, [class*="job-card"]'
    CITY_TRIGGERS = [
        '.nav-city', '.city-label', '.header-city-info',
        '[class*="city-select"]', '[class*="switch-city"]',
    ]
    SCROLL_TIMES_FIRST = 3
    PAGE_TURN_DELAY = (2.0, 5.0)

    def __init__(self):
        super().__init__("boss")
        # Boss 搜索 XHR 旁听
        self._api_batches: list = []
        self._api_has_more: bool = True
        self._api_counter: int = 0
        self._api_batch_seq: int = 0
        self._api_tasks: set = set()
        self._api_risk: str = ""

    async def _pre_check(self, page) -> "str | None":
        current_url = await self.engine.current_url(page)
        if 'about:blank' in current_url or not current_url:
            return "Boss blocked (about:blank). Please login in Chrome first."
        return None

    async def _check_login_wall(self, page) -> bool:
        return bool(await self.eval_js(page, """() => {
            const tips = document.querySelector(
                '.login-tips, .login-register, .dialog-login, '
                + '.sign-form-container'
            );
            if (tips && window.getComputedStyle(tips).display !== 'none')
                return true;
            if (document.title === '' || location.href === 'about:blank')
                return true;
            return false;
        }"""))

    async def _check_anti_bot(self, page) -> bool:
        return await super()._check_anti_bot(page)

    async def apply_to_job(self, page, job, greeting, params) -> ApplyOutcome:
        """Boss直聘: 点击"立即沟通"→ 等待聊天窗口 → 输入打招呼语 → 发送"""
        find_js = self.build_card_button_finder(
            job,
            card_selectors='.job-card-wrapper, .job-list > ul > li, [class*="job-card"]',
            title_selectors='.job-name, .job-title, [class*="job-name"]',
            company_selectors='.company-name a, .company-text, [class*="company-name"]',
            button_selectors=(
                '.btn-startchat, .op-btn-chat, [class*="startchat"], '
                'a[ka="job_list_operate_chat"]'
            ),
            button_texts=['立即沟通', '继续沟通', '沟通', '在线沟通'],
        )
        await asyncio.sleep(gaussian_delay(0.3, 0.6))
        result = await self.human_click(page, find_js)
        if not result.get("clicked"):
            reason = result.get("reason", "unknown")
            if reason == "already":
                return ApplyOutcome.ALREADY
            self._apply_detail = f"[环境] 未找到投递按钮({reason})"
            print(f"  [Boss直聘] apply 失败: {reason}", flush=True)
            return ApplyOutcome.FAILED

        print(f"  [Boss直聘] 鼠标点击了按钮: '{result.get('text', '')}'", flush=True)

        chat_input_sel = (
            '.chat-input textarea, .message-input textarea, '
            '.input-area textarea, [class*="chat-input"] textarea, '
            '.edit-area [contenteditable="true"], '
            '.chat-editor [contenteditable="true"], '
            '.dialog-wrap textarea, .chat-message-area textarea'
        )
        # 用元素等待替代固定 sleep：聊天框出现就继续，没出现再走三态判定
        try:
            await page.wait_for_selector(chat_input_sel, timeout=5000,
                                         state="visible")
        except Exception:
            pass

        if await self._check_anti_bot(page):
            return ApplyOutcome.BLOCKED

        sent = "no_input"
        if greeting:
            sent = await self.eval_js(page, f"""() => {{
                const GREETING = {js_literal(greeting)};
                const input = document.querySelector({js_literal(chat_input_sel)});
                if (!input) return 'no_input';
                if (input.tagName === 'TEXTAREA') {{
                    input.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(input, GREETING);
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }} else {{
                    input.focus();
                    input.innerText = GREETING;
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                return 'filled';
            }}""")
            print(f"  [Boss直聘] 输入框状态: {sent}", flush=True)

        draft = (getattr(params, "apply_mode", "auto") or "auto").lower() == "draft"
        if sent == "filled":
            if draft:
                self._apply_detail = "草稿未发送"
                outcome = ApplyOutcome.UNCERTAIN
            else:
                await asyncio.sleep(gaussian_delay(0.5, 0.9))
                send_find = self.build_modal_button_finder(
                    button_selectors='.btn-send, .send-message, [class*="send-btn"]',
                    button_texts=['发送', '回车发送', 'Send'],
                )
                send_result = await self.human_click(page, send_find)
                if send_result.get("clicked"):
                    print(f"  [Boss直聘] 已发送: '{send_result.get('text', '')}'",
                          flush=True)
                    outcome = ApplyOutcome.SUCCESS
                else:
                    print(f"  [Boss直聘] 未找到发送按钮，尝试 Enter", flush=True)
                    try:
                        await self.engine.press_key(page, "Enter")
                        outcome = ApplyOutcome.SUCCESS
                    except Exception:
                        self._apply_detail = "已填写但未找到发送按钮"
                        outcome = ApplyOutcome.UNCERTAIN
                await asyncio.sleep(gaussian_delay(0.5, 0.9))
        else:
            chat_open = await self.eval_js(page, """() => !!(document.querySelector(
                '.chat-content, .chat-dialog, .dialog-wrap, .chat-container, '
                + '.chat-message-area'
            ) || location.href.includes('/chat/'))""")
            if chat_open:
                self._apply_detail = (
                    "聊天窗已打开但未找到输入框（可能已自动打招呼）")
                outcome = ApplyOutcome.UNCERTAIN
            else:
                self._apply_detail = "[状态] 点击后未出现聊天输入框"
                outcome = ApplyOutcome.FAILED

        # 关闭聊天弹窗
        await self.eval_js(page, """() => {
            const close = document.querySelector(
                '.dialog-close, .chat-close, .zppp-close, '
                + '.close-btn, [class*="dialog"] .close, .ant-modal-close'
            );
            if (close) close.click();
        }""")
        await asyncio.sleep(gaussian_delay(0.3, 0.6))

        # 返回列表页（如果导航到了聊天页面）
        await self.eval_js(page, """() => {
            if (window.location.href.includes('/chat/')) {
                window.history.back();
            }
        }""")
        await asyncio.sleep(gaussian_delay(0.5, 1.0))
        return outcome

    # ──────── Boss 搜索 XHR 旁听 ────────

    async def _do_search(self, params):
        """先挂上响应监听，再输入关键词/点搜索。"""
        name = self.platform_cfg["name"]
        page = await self.fetch_page(
            self.platform_cfg["search_url"], network_idle=True)
        self._attach_api_capture(page)
        await asyncio.sleep(gaussian_delay(0.5, 1.2))
        pre_err = await self._pre_check(page)
        if pre_err:
            raise RuntimeError(pre_err)
        if params.city:
            self.log(f"  [{name}] 🌐 正在筛选城市: {params.city} ...")
            city_ok = await self._select_city(page, params.city)
            if city_ok:
                self.log(f"  [{name}] ✓ 城市已选: {params.city}")
            else:
                self.log(f"  [{name}] ⚠ 城市筛选失败（可能 UI 变更或不支持）")
        filled = await self.input_keyword(
            page, params.keyword, self.SEARCH_INPUT_SELECTORS)
        if not filled:
            print(f"  [{name}] search input not found", flush=True)
        await self.click_search_button(page, self.SEARCH_BUTTON_SELECTORS)
        return page

    def _attach_api_capture(self, page):
        if os.getenv("JOBSPIDER_BOSS_API", "auto").strip().lower() == "off":
            return
        self._api_batches = []
        self._api_has_more = True
        self._api_counter = 0
        self._api_batch_seq = 0
        self._api_risk = ""

        def _on_resp(resp):
            try:
                url = resp.url or ""
                if "/wapi/zpgeek/search/joblist.json" not in url:
                    return
                self._api_counter += 1
                if self._api_counter >= 500:
                    self._platform_done = True
                    self.log("  [Boss直聘] ⚠ 搜索请求预算 500 已用尽，停止翻页")
                elif self._api_counter == 400:
                    self.log("  [Boss直聘] ⚠ 搜索请求已用 400/500")

                async def _read():
                    try:
                        data = await resp.json()
                    except Exception:
                        return
                    self._on_api_payload(data)

                t = asyncio.create_task(_read())
                self._api_tasks.add(t)
                t.add_done_callback(self._api_tasks.discard)
            except Exception:
                pass

        page.on("response", _on_resp)

    def _on_api_payload(self, data):
        if not isinstance(data, dict):
            return
        code = data.get("code")
        if code in (31, 37, "31", "37"):
            self._api_risk = (
                f"Boss API code={code}: "
                f"{data.get('message') or data.get('msg') or ''}"
            )
            self._platform_done = True
            self.log(f"  [Boss直聘] 🚨 {self._api_risk}")
            try:
                asyncio.create_task(self.risk.trip(
                    self.platform_key, RiskSignal.RATE_LIMITED,
                    self._api_risk))
            except Exception:
                pass
            return

        zp = data.get("zpData") or {}
        jobs = zp.get("jobList")
        if isinstance(jobs, list) and jobs:
            self._api_batches.append(jobs)
            self._api_batch_seq += 1
            # 登录态失效的可靠信号：jobList 有数据但 salaryDesc 全空
            if all(not (j.get("salaryDesc") or "")
                   for j in jobs if isinstance(j, dict)):
                self._api_risk = "jobList salaryDesc 全空（登录态失效）"
                self._platform_done = True
                self.log(f"  [Boss直聘] 🚨 {self._api_risk}")
                try:
                    asyncio.create_task(self.risk.trip(
                        self.platform_key, RiskSignal.LOGIN_REQUIRED,
                        self._api_risk))
                except Exception:
                    pass
        has_more = zp.get("hasMore")
        if has_more is not None:
            if isinstance(has_more, str):
                self._api_has_more = has_more.strip().lower() not in (
                    "false", "0", "no", "")
            else:
                self._api_has_more = bool(has_more)

    def _jobs_from_api(self, batches) -> list[JobInfo]:
        jobs: list[JobInfo] = []
        seen: set = set()
        for batch in batches:
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                jid = str(raw.get("encryptJobId") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                title = (raw.get("jobName") or "").strip()
                company = (raw.get("brandName") or "").strip()
                if not title or not company:
                    continue
                salary = (raw.get("salaryDesc") or "").strip()
                smin, smax, stype = self.parse_salary(salary)
                labels = (list(raw.get("jobLabels") or [])
                          + list(raw.get("skills") or []))
                jobs.append(JobInfo(
                    job_id=jid,
                    platform=self.platform_key,
                    platform_name=self.platform_cfg["name"],
                    job_url=f"https://www.zhipin.com/job_detail/{jid}.html",
                    job_title=title,
                    company_name=company,
                    salary_range=salary or None,
                    salary_min=smin, salary_max=smax, salary_type=stype,
                    city=(raw.get("cityName") or None),
                    district=(raw.get("areaDistrict") or None),
                    experience_required=(raw.get("jobExperience") or None),
                    education_required=(raw.get("jobDegree") or None),
                    company_industry=(raw.get("brandIndustry") or None),
                    company_size=(raw.get("brandScaleName") or None),
                    company_logo=(raw.get("brandLogo") or None),
                    tags=[str(x) for x in labels][:12],
                    hr_name=(raw.get("bossName") or None),
                    hr_position=(raw.get("bossTitle") or None),
                ))
        return jobs

    async def _click_next(self, page) -> bool:
        """Boss 是 SPA 无限滚动：滚到底等新的 joblist XHR，而不是点下一页。"""
        if os.getenv("JOBSPIDER_BOSS_API", "auto").strip().lower() == "off":
            return await super()._click_next(page)
        if not self._api_has_more or self._platform_done:
            return False
        before_seq = self._api_batch_seq
        for _ in range(3):
            try:
                await page.mouse.wheel(0, random.randint(1800, 3200))
            except Exception:
                await self.eval_js(
                    page, "window.scrollTo(0, document.body.scrollHeight)")
            for _ in range(20):  # 最多等 ~6s
                await asyncio.sleep(0.3)
                if self._api_batch_seq > before_seq:
                    return True
        # 没有新批次时回退到通用下一页按钮（某些页面仍有分页器）
        return await super()._click_next(page)

    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        batches = self._api_batches
        self._api_batches = []
        if batches:
            api_jobs = self._jobs_from_api(batches)
            if api_jobs:
                print(f"  [Boss直聘] API 旁听: {len(api_jobs)} 条", flush=True)
                return api_jobs
        try:
            jobs_data = await self.eval_js(page, """() => {
                const cards = document.querySelectorAll(
                    '.job-card-wrapper, .job-list > ul > li, [class*="job-card"]'
                );
                return Array.from(cards).map(card => {
                    const titleEl = card.querySelector(
                        '.job-name, .job-title, [class*="job-name"]'
                    );
                    const salaryEl = card.querySelector(
                        '.salary, .red, [class*="salary"]'
                    );
                    const companyEl = card.querySelector(
                        '.company-name a, .company-text, [class*="company-name"] a'
                    );
                    const linkEl = card.querySelector('a[href*="job_detail"]');
                    const hrEl = card.querySelector('.boss-name, .hr-name');
                    const logoEl = card.querySelector(
                        '.company-logo img, img[src*="logo"]'
                    );
                    const tagEls = card.querySelectorAll(
                        '.tag-list li, [class*="tag"] span'
                    );
                    return {
                        job_id: (linkEl?.href || '').match(
                            /job_detail\\/([^.?/]+)/
                        )?.[1] || card.querySelector(
                            '[data-jobid]'
                        )?.getAttribute('data-jobid') || '',
                        job_title: titleEl?.textContent?.trim() || '',
                        salary: salaryEl?.textContent?.trim() || '',
                        company_name: companyEl?.textContent?.trim() || '',
                        job_url: linkEl?.href || '',
                        info_text: (() => {
                            const sels = [
                                '.info-primary p', '.job-info',
                                '.job-card-footer', '.tag-list',
                                '.info-desc', '.job-limit',
                            ];
                            for (const sel of sels) {
                                const el = card.querySelector(sel);
                                if (el && el.textContent?.trim().length > 3)
                                    return el.textContent.trim();
                            }
                            // 回退：找包含"年"和"本科/大专/硕士"的文本节点
                            const allEls = card.querySelectorAll('p, span, div, li');
                            for (const el of allEls) {
                                const t = el.textContent?.trim() || '';
                                if (t.length > 3 && t.length < 60
                                    && (/年/.test(t) || /应届/.test(t))
                                    && (/本科|大专|硕士|博士|学历/.test(t) || /·/.test(t))) {
                                    return t;
                                }
                            }
                            return '';
                        })(),
                        company_info: card.querySelector(
                            '.info-company p, .company-info'
                        )?.textContent?.trim() || '',
                        hr_name: hrEl?.textContent?.trim() || '',
                        company_logo: logoEl?.src || '',
                        tags: Array.from(tagEls).map(
                            t => t.textContent?.trim()
                        ).filter(Boolean),
                    };
                });
            }""")

            if not jobs_data:
                return []

            jobs = []
            for data in jobs_data:
                if not data["job_title"] or not data["company_name"]:
                    continue
                salary_min, salary_max, salary_type = self.parse_salary(
                    data["salary"]
                )
                city, exp, edu = self._parse_info_text(data["info_text"])
                industry, stage, scale = self._parse_company_info(
                    data["company_info"]
                )
                job_id = data["job_id"] or (
                    f"boss_{hashlib.md5(data['job_url'].encode('utf-8')).hexdigest()[:16]}"
                )
                jobs.append(JobInfo(
                    job_id=job_id, platform=self.platform_key,
                    platform_name=self.platform_cfg["name"],
                    job_url=data["job_url"]
                        or f"https://www.zhipin.com/job_detail/{job_id}.html",
                    job_title=data["job_title"],
                    company_name=data["company_name"],
                    salary_range=data["salary"] or None,
                    salary_min=salary_min, salary_max=salary_max,
                    salary_type=salary_type,
                    city=city or params.city,
                    experience_required=exp,
                    education_required=edu,
                    company_industry=industry, company_size=scale,
                    tags=data.get("tags") or [],
                    hr_name=data.get("hr_name") or None,
                    company_logo=data.get("company_logo") or None,
                ))
            return jobs
        except Exception as e:
            print(f"  [extract_list_page/{self.platform_key}] {type(e).__name__}: {e}", flush=True)
            return []

    async def extract_detail_page(self, page, job_id: str) -> dict:
        try:
            detail_url = self.platform_cfg["detail_url"].format(job_id=job_id)
            p = await self.fetch_page(detail_url, network_idle=True)
            return await self.eval_js(p, """() => {
                const g = s => document.querySelector(s)?.textContent?.trim() || '';
                return {
                    description: g('.job-sec-text') || g('.job-detail'),
                    company_industry: g('.company-industry'),
                    company_size: g('.company-scale'),
                    hr_name: g('.boss-name'),
                    hr_position: g('.boss-title'),
                };
            }""") or {}
        except Exception:
            return {}

    def _parse_info_text(self, text: str) -> tuple:
        if not text:
            return None, None, None
        parts = [p.strip() for p in text.replace("/", " ").replace("·", " ").replace("|", " ").split() if p.strip()]
        city, exp, edu = None, None, None
        for p in parts:
            if not exp and ("年" in p or "应届" in p or "经验" in p):
                exp = p
            elif not edu and any(d in p for d in ["大专", "本科", "硕士", "博士", "高中", "中专", "学历不限"]):
                edu = p
            elif not city and len(p) >= 2 and not any(d in p for d in ["年", "应届", "大专", "本科", "硕士", "博士"]):
                if any(c in p for c in ["市", "区", "省", "州", "京", "沪", "津", "渝"]):
                    city = p
                elif re.search(r'^[一-鿿]{2,5}$', p):
                    city = p
        return city, exp, edu

    def _parse_company_info(self, text: str) -> tuple:
        if not text:
            return None, None, None
        parts = [p.strip() for p in text.split() if p.strip()]
        industry, stage, scale = None, None, None
        for p in parts:
            if "人" in p and ("-" in p or "以" in p):
                scale = p
            elif any(s in p for s in ["轮","上市","融资","天使"]):
                stage = p
            elif not industry:
                industry = p
        return industry, stage, scale
