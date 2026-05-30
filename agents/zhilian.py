"""智联招聘平台子智能体 — 浏览器交互版

流程：打开 zhaopin.com/sou → 输关键词 → 点搜索 → 提取职位 → 翻页
选择器来源：probe_dom.py 实测
"""
import asyncio
import hashlib
import json

from models.job import SearchParams, JobInfo
from agents.base import BaseAgent
from config import gaussian_delay


class ZhilianAgent(BaseAgent):

    SEARCH_INPUT_SELECTORS = [
        '.query-search__content-input',
        'input[placeholder*="输入职位"]',
        'input[placeholder*="搜索"]',
    ]
    SEARCH_BUTTON_SELECTORS = [
        '.query-search__content-button',
        'button.search__button',
    ]
    RESULT_CONTAINER_SELECTOR = '.joblist-box__item'
    DIAGNOSE_CARD_SELECTOR = '.joblist-box__item'
    CITY_TRIGGERS = [
        '.query-location', '.location-text', '[class*="city"]',
        '[class*="location"]',
    ]
    CITY_CONTAINER = '.query-location'
    SCROLL_TIMES_FIRST = 2
    PAGE_TURN_DELAY = (2.0, 4.0)

    def __init__(self):
        super().__init__("zhilian")

    async def _check_login_wall(self, page) -> bool:
        return bool(await self.eval_js(page, """() => {
            const m = document.querySelector(
                '.login-box, .zppp-modal__login, .sign-dialog, '
                + '.zppp-modal-body, [class*="login-dialog"]'
            );
            if (m && window.getComputedStyle(m).display !== 'none') return true;
            if (location.href.includes('/login')) return true;
            return false;
        }"""))

    async def _check_anti_bot(self, page) -> bool:
        try:
            url = await self.engine.current_url(page)
            if any(k in (url or "").lower() for k in ("captcha", "verify", "secure")):
                self.log(f"  [智联招聘] 🚨 anti-bot URL detected: {url[:80]}")
                return True
            hit = await self.eval_js(page, """() => {
                const sel = [
                    '.captcha-mask', '.verify-mask', '.geetest_holder',
                    '.geetest_panel', '.nc_iconfont', '.nc-container',
                    '.captcha-box', '[class*="captcha"]',
                    '.zppp-modal__login.show', '.sign-dialog.show',
                ];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null
                        && el.offsetWidth > 50 && el.offsetHeight > 50) {
                        return el.className || s;
                    }
                }
                const body = (document.body?.innerText || '').slice(0, 4000);
                if (/请完成.{0,4}验证|滑动.{0,4}验证|安全验证|系统检测到|访问过于频繁/.test(body))
                    return 'text_match';
                return '';
            }""")
            if hit:
                self.log(f"  [智联招聘] 🚨 anti-bot DOM: {hit}")
                return True
        except Exception as e:
            print(f"  [智联 anti-bot check] {e}", flush=True)
        return False

    async def _select_city(self, page, city: str) -> bool:
        """智联城市选择：点击'地点'筛选器 → 选择城市"""
        safe_city = city.replace("'", "\\'")
        # 先尝试点击"地点"文本打开城市下拉
        opened = await self.eval_js(page, """() => {
            const els = document.querySelectorAll(
                '.query-location, [class*="location"], [class*="city"], '
                + 'span, div, button, a'
            );
            for (const el of els) {
                const t = el.textContent?.trim();
                if ((t === '地点' || t === '城市' || t === '全国')
                    && el.offsetParent !== null
                    && el.offsetHeight < 60 && el.offsetHeight > 10) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        if not opened:
            opened = await self.engine.try_click(page, self.CITY_TRIGGERS)
        if not opened:
            print(f"  [{self.platform_cfg['name']}] 未找到城市选择器", flush=True)
            return False

        await asyncio.sleep(gaussian_delay(0.5, 1.0))

        # 在展开的面板中找到目标城市
        selected = await self.eval_js(page, f"""() => {{
            const candidates = document.querySelectorAll(
                '.query-location a, .query-location li, .query-location span, '
                + '[class*="location"] a, [class*="location"] li, '
                + '[class*="city"] a, [class*="city"] li, '
                + '.search-dropdown a, .dropdown-menu a, '
                + 'a, li, span, dd, label'
            );
            for (const el of candidates) {{
                const t = el.textContent?.trim();
                if (t === '{safe_city}' && el.offsetParent !== null
                    && el.childElementCount === 0
                    && el.offsetHeight < 50 && el.offsetHeight > 5) {{
                    el.click();
                    return true;
                }}
            }}
            for (const el of candidates) {{
                const t = el.textContent?.trim();
                if (t && t.startsWith('{safe_city}') && t.length < {len(city) + 5}
                    && el.offsetParent !== null
                    && el.offsetHeight < 50 && el.offsetHeight > 5) {{
                    el.click();
                    return true;
                }}
            }}
            return false;
        }}""")
        if selected:
            print(f"  [{self.platform_cfg['name']}] 城市: {city}", flush=True)
            await asyncio.sleep(gaussian_delay(1.0, 2.0))
            return True
        print(f"  [{self.platform_cfg['name']}] 未找到城市 '{city}'", flush=True)
        return False

    async def _do_search(self, params):
        """智联搜索：输关键词 → 点搜索 → 在结果页选城市（'地点'筛选器）"""
        name = self.platform_cfg["name"]
        page = await self.fetch_page(
            self.platform_cfg["search_url"], network_idle=True,
        )
        await asyncio.sleep(gaussian_delay(1, 2))
        pre_err = await self._pre_check(page)
        if pre_err:
            raise RuntimeError(pre_err)
        filled = await self.input_keyword(
            page, params.keyword, self.SEARCH_INPUT_SELECTORS,
        )
        if not filled:
            print(f"  [{name}] search input not found")
        await self.click_search_button(page, self.SEARCH_BUTTON_SELECTORS)
        await asyncio.sleep(gaussian_delay(2, 4))
        # 城市选择在搜索结果页进行（"地点"筛选器）
        if params.city:
            await self._select_city(page, params.city)
        return page

    async def _click_next(self, page) -> bool:
        result = await self.eval_js(page, """() => {
            const btns = document.querySelectorAll('a.soupager__btn');
            for (const btn of btns) {
                if (btn.textContent.trim() === '下一页'
                    && !btn.classList.contains('soupager__btn--disable')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        return bool(result)

    async def apply_to_job(self, page, job, greeting, params) -> bool:
        """智联: 点击对应职位卡片的"立即投递"按钮，然后发送打招呼语"""
        find_js = self.build_card_button_finder(
            job,
            card_selectors='.joblist-box__item',
            title_selectors='.jobinfo__name, .iteminfo__line1__jobname',
            company_selectors='.iteminfo__line1__compname, .companyinfo__name',
            button_selectors='.collect-and-apply__btn, .apply-btn, [class*="apply"]',
            button_texts=['立即投递', '投递', '申请'],
        )
        await asyncio.sleep(gaussian_delay(0.3, 0.6))
        result = await self.human_click(page, find_js)
        if not result.get("clicked"):
            reason = result.get("reason", "unknown")
            print(f"  [智联招聘] apply 失败: {reason}", flush=True)
            return False

        print(f"  [智联招聘] 鼠标点击了按钮: '{result.get('text', '')}'", flush=True)
        await asyncio.sleep(gaussian_delay(1.5, 2.5))

        # 检查弹窗是否出现
        modal_present = await self.eval_js(page, """() => {
            const m = document.querySelector(
                '.zppp-modal, .dialog-content, .ant-modal, [class*="modal"][class*="visible"]'
            );
            return !!(m && m.offsetParent !== null);
        }""")
        if not modal_present:
            # 无弹窗 — 智联部分岗位点击"投递"直接提交，不弹对话框
            # 检查是否有错误
            has_error = await self.eval_js(page, """() => {
                const loginDlg = document.querySelector(
                    '.zppp-modal-login, [class*="login-modal"], [class*="login-pop"]'
                );
                if (loginDlg && loginDlg.offsetParent !== null) return 'login';
                return '';
            }""")
            if has_error:
                print(f"  [智联招聘] 投递后检测到异常: {has_error}", flush=True)
                return False
            print(f"  [智联招聘] 直接投递（无确认弹窗）", flush=True)
            return True

        # 尝试在弹窗中填写打招呼语
        filled = False
        if greeting:
            safe_greeting = json.dumps(greeting)[1:-1]  # strip outer quotes
            filled = await self.eval_js(page, f"""() => {{
                const ta = document.querySelector(
                    '.zppp-modal textarea, .greet-textarea textarea, '
                    + '.dialog-content textarea, textarea[placeholder*="打招呼"], '
                    + 'textarea[placeholder*="沟通"]'
                );
                if (!ta) return false;
                ta.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                ).set;
                setter.call(ta, '{safe_greeting}');
                ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}""")
            if filled:
                print(f"  [智联招聘] 已填打招呼语", flush=True)
            await asyncio.sleep(gaussian_delay(0.5, 1.0))

        # 点击确认/提交按钮 — 用真实鼠标
        confirm_find = self.build_modal_button_finder(
            button_selectors='.zppp-modal .zppp-submit, .dialog-confirm, .btn-primary',
            button_texts=['发送', '确认', '确定', '提交', '立即沟通', '投递'],
        )
        confirm_result = await self.human_click(page, confirm_find)
        confirmed = confirm_result.get("text") if confirm_result.get("clicked") else None
        if confirmed:
            print(f"  [智联招聘] 已确认: '{confirmed}'", flush=True)
        await asyncio.sleep(gaussian_delay(0.8, 1.2))

        # 关闭弹窗
        await self.eval_js(page, """() => {
            const close = document.querySelector(
                '.zppp-modal .zppp-close, .dialog-close, .modal-close, .ant-modal-close'
            );
            if (close) close.click();
        }""")
        await asyncio.sleep(gaussian_delay(0.3, 0.5))
        # 只有在确认或至少填了招呼时才算成功
        return bool(confirmed) or bool(filled)

    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        try:
            await asyncio.sleep(1)
            jobs_data = await self.eval_js(page, """() => {
                const items = document.querySelectorAll('.joblist-box__item');
                return Array.from(items).map(item => {
                    const nameEl = item.querySelector(
                        '.jobinfo__name, .iteminfo__line1__jobname'
                    );
                    const salaryEl = item.querySelector(
                        '.iteminfo__line2__jobdesc__salary, .jobinfo__salary'
                    );
                    const companyEl = item.querySelector(
                        '.iteminfo__line1__compname, .companyinfo__name'
                    );
                    const linkEl = item.querySelector('a[href*="/job/"]')
                        || item.querySelector('a');
                    const cityEl = item.querySelector(
                        '.jobinfo__other-info span:first-child'
                    );
                    const infoEl = item.querySelector(
                        '.jobinfo__other-info, .iteminfo__line2__jobdesc__demand'
                    );
                    const tagsEl = item.querySelectorAll(
                        '.iteminfo__line2__jobdesc__tag span, .jobinfo__tag span'
                    );
                    const href = linkEl?.href || '';
                    const jobId = href.match(/job[\\/]+([a-zA-Z0-9]+)/)?.[1]
                        || item.getAttribute('data-jid') || '';
                    return {
                        job_id: jobId,
                        job_title: nameEl?.textContent?.trim() || '',
                        salary: salaryEl?.textContent?.trim() || '',
                        company_name: companyEl?.textContent?.trim() || '',
                        job_url: href,
                        city: cityEl?.textContent?.trim() || '',
                        info: infoEl?.textContent?.trim() || '',
                        tags: Array.from(tagsEl).map(
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
                jobs.append(JobInfo(
                    job_id=data["job_id"] or (
                        f"zl_{hashlib.md5(data['job_url'].encode('utf-8')).hexdigest()[:16]}"
                    ),
                    platform=self.platform_key,
                    platform_name=self.platform_cfg["name"],
                    job_url=data["job_url"],
                    job_title=data["job_title"],
                    company_name=data["company_name"],
                    salary_range=data["salary"] or None,
                    salary_min=salary_min, salary_max=salary_max,
                    salary_type=salary_type,
                    city=data["city"] or params.city,
                    experience_required=self._parse_info(data["info"], "exp"),
                    education_required=self._parse_info(data["info"], "edu"),
                    tags=data.get("tags") or [],
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
                    description: g('.job-detail__content') || g('.describtion'),
                    benefits: Array.from(document.querySelectorAll(
                        '.welfare-list-item, .welfare-tag span'
                    )).map(e => e.textContent.trim()).filter(Boolean),
                };
            }""") or {}
        except Exception:
            return {}

    def _parse_info(self, text: str, field: str) -> "str | None":
        if not text:
            return None
        parts = text.replace("|", " ").split()
        if field == "exp":
            for p in parts:
                if "年" in p or "应届" in p:
                    return p.strip()
        elif field == "edu":
            for level in ["博士", "硕士", "本科", "大专", "高中"]:
                if level in text:
                    return level
        return None
