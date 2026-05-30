"""Boss直聘平台子智能体 — 浏览器交互版

流程：打开 zhipin.com → 选城市 → 输关键词 → 点搜索 → 提取职位 → 翻页
注意：Boss 反爬较强，首次使用需在 chrome_profile 中手动登录。
      probe 返回 about:blank 说明未登录时页面直接拦截。
"""
import asyncio
import hashlib
import json
import re

from models.job import SearchParams, JobInfo
from agents.base import BaseAgent
from config import gaussian_delay


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
        try:
            url = await self.engine.current_url(page)
            if any(k in (url or "").lower() for k in ("safe", "captcha", "verify", "geetest")):
                self.log(f"  [Boss直聘] 🚨 anti-bot URL detected: {url[:80]}")
                return True
            hit = await self.eval_js(page, """() => {
                const sel = [
                    '.geetest_panel', '.geetest_holder', '.geetest_box',
                    '.verify-wrap', '.verify-box', '.captcha-box',
                    '[class*="captcha"]', '[class*="risk"]',
                    '.dialog-login.show', '.login-required-modal',
                ];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null
                        && el.offsetWidth > 50 && el.offsetHeight > 50) {
                        return el.className || s;
                    }
                }
                // 文本兜底
                const body = (document.body?.innerText || '').slice(0, 4000);
                if (/请完成.{0,4}验证|滑动.{0,4}验证|安全验证|系统检测到|访问过于频繁/.test(body))
                    return 'text_match';
                return '';
            }""")
            if hit:
                self.log(f"  [Boss直聘] 🚨 anti-bot DOM: {hit}")
                return True
        except Exception as e:
            print(f"  [Boss anti-bot check] {e}", flush=True)
        return False

    async def apply_to_job(self, page, job, greeting, params) -> bool:
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
            print(f"  [Boss直聘] apply 失败: {reason}", flush=True)
            return False

        print(f"  [Boss直聘] 鼠标点击了按钮: '{result.get('text', '')}'", flush=True)
        await asyncio.sleep(gaussian_delay(2.0, 3.5))

        # 检查是否打开了聊天窗口/对话框
        sent_ok = False
        if greeting:
            safe_greeting = json.dumps(greeting)[1:-1]  # strip outer quotes
            sent = await self.eval_js(page, f"""() => {{
                // Boss 聊天输入框：textarea 或 contenteditable
                const input = document.querySelector(
                    '.chat-input textarea, .message-input textarea, '
                    + '.input-area textarea, [class*="chat-input"] textarea, '
                    + '.edit-area [contenteditable="true"], '
                    + '.chat-editor [contenteditable="true"], '
                    + '.dialog-wrap textarea, .chat-message-area textarea'
                );
                if (!input) return 'no_input';
                if (input.tagName === 'TEXTAREA') {{
                    input.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(input, '{safe_greeting}');
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }} else {{
                    input.focus();
                    input.innerText = '{safe_greeting}';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                return 'filled';
            }}""")
            print(f"  [Boss直聘] 输入框状态: {sent}", flush=True)
            if sent == 'filled':
                await asyncio.sleep(gaussian_delay(0.8, 1.2))
                send_find = self.build_modal_button_finder(
                    button_selectors='.btn-send, .send-message, [class*="send-btn"]',
                    button_texts=['发送', '回车发送', 'Send'],
                )
                send_result = await self.human_click(page, send_find)
                if send_result.get("clicked"):
                    print(f"  [Boss直聘] 已发送: '{send_result.get('text', '')}'", flush=True)
                    sent_ok = True
                else:
                    print(f"  [Boss直聘] 未找到发送按钮，尝试 Enter", flush=True)
                    try:
                        await self.engine.press_key(page, "Enter")
                        sent_ok = True
                    except Exception:
                        pass
                await asyncio.sleep(gaussian_delay(0.8, 1.2))

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
        return sent_ok

    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        try:
            await asyncio.sleep(1.5)
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
