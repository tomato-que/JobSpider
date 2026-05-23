"""猎聘平台子智能体 — 浏览器交互版

流程：打开 liepin.com/zhaopin → 输关键词 → 点搜索 → 提取职位 → 翻页
选择器来源：probe_dom.py 实测
  - 搜索框: input[placeholder="搜索职位、公司"] (无 class/id, w=676)
  - 职位卡片: .job-list-box > div (42 items)
  - 翻页: .ant-pagination-next
  - 城市: 页面内联城市链接 (全国 北京 上海...)
"""
import asyncio
import hashlib
import json

from models.job import SearchParams, JobInfo
from agents.base import BaseAgent
from config import gaussian_delay


class LiepinAgent(BaseAgent):

    SEARCH_INPUT_SELECTORS = [
        'input[placeholder="搜索职位、公司"]',
        'input[placeholder*="搜索职位"]',
        'input[placeholder*="搜索"]',
    ]
    SEARCH_BUTTON_SELECTORS = [
        '.search-btn',
        '[class*="search-btn"]',
        'button[class*="search"]',
    ]
    RESULT_CONTAINER_SELECTOR = '.job-list-box, .job-card-pc-container, [class*="job-list"], .content-left-section'
    DIAGNOSE_CARD_SELECTOR = '.job-list-box > div, .job-list-box > a, .job-card-pc-container, [class*="job-card"]'
    CITY_TRIGGERS = [
        '.city-wrapper .title', '[class*="city"]', '.switch-city-btn',
    ]
    SCROLL_TIMES_FIRST = 2
    PAGE_TURN_DELAY = (2.0, 4.0)

    def __init__(self):
        super().__init__("liepin")
        self._pending_detail_page = None

    async def _check_login_wall(self, page) -> bool:
        return bool(await self.eval_js(page, """() => {
            const m = document.querySelector(
                '.login-modal, .login-pop, .com-login'
            );
            return m !== null
                && window.getComputedStyle(m).display !== 'none';
        }"""))

    async def _select_city(self, page, city: str) -> bool:
        """猎聘城市选择：点击搜索页上方的城市链接（全国/北京/上海...）"""
        safe_city = city.replace("'", "\\'")
        selected = await self.eval_js(page, f"""() => {{
            // 猎聘城市链接在搜索页上方，与"城市"标签在同一行
            const links = document.querySelectorAll('a');
            for (const a of links) {{
                const t = a.textContent?.trim();
                if (t === '{safe_city}' && a.offsetParent !== null
                    && a.offsetHeight > 5 && a.offsetHeight < 50) {{
                    a.click();
                    return true;
                }}
            }}
            return false;
        }}""")
        if selected:
            print(f"  [{self.platform_cfg['name']}] city: {city}", flush=True)
            await asyncio.sleep(gaussian_delay(2, 4))
            return True
        print(f"  [{self.platform_cfg['name']}] 未找到城市 '{city}'", flush=True)
        return False

    async def _do_search(self, params):
        """猎聘搜索：URL 预填关键词 → JS 关闭自动补全 → JS 点击搜索按钮"""
        from urllib.parse import quote as _quote
        url = f"https://www.liepin.com/zhaopin/?key={_quote(params.keyword)}"
        page = await self.fetch_page(url, network_idle=True)
        await asyncio.sleep(gaussian_delay(1.0, 2.0))

        # 用 JS 关闭自动补全 + 点击搜索按钮（搜索按钮不是反爬检测目标，JS click 安全）
        search_ok = await self.eval_js(page, """() => {
            // 1. blur 输入框，关闭自动补全下拉
            const input = document.querySelector(
                'input[placeholder*="搜索职位"], input[placeholder*="搜索"]'
            );
            if (input) input.blur();

            // 2. 关闭可能残留的下拉面板
            const drops = document.querySelectorAll(
                '[class*="suggest"], [class*="autocomplete"], '
                + '[class*="dropdown-menu"], [class*="search-dropdown"]'
            );
            drops.forEach(d => { d.style.display = 'none'; });

            // 3. 找到搜索按钮并点击
            const els = document.querySelectorAll('button, a, div, span');
            for (const el of els) {
                const t = el.textContent?.trim();
                const cls = (el.className || '').toString().toLowerCase();
                if ((t === '搜索' || cls.includes('search-btn'))
                    && el.offsetParent !== null
                    && el.offsetHeight > 20 && el.offsetHeight < 100
                    && el.offsetWidth > 40) {
                    el.click();
                    return 'clicked';
                }
            }
            return 'no_btn';
        }""")
        print(f"  [猎聘] 搜索按钮: {search_ok}", flush=True)

        if search_ok != 'clicked':
            # 回退：聚焦搜索框 → Escape 关闭补全 → Enter 提交
            try:
                await self.engine.press_key(page, "Escape")
                await asyncio.sleep(0.3)
                for sel in self.SEARCH_INPUT_SELECTORS:
                    focused = await self.eval_js(page, f"""() => {{
                        const el = document.querySelector('{sel}');
                        if (el) {{ el.focus(); return true; }}
                        return false;
                    }}""")
                    if focused:
                        break
                await asyncio.sleep(0.2)
                await self.engine.press_key(page, "Escape")
                await asyncio.sleep(0.2)
                await self.engine.press_key(page, "Enter")
            except Exception:
                pass

        await asyncio.sleep(gaussian_delay(2, 4))

        # 验证搜索结果是否加载
        card_count = await self.eval_js(page, """() => {
            const sels = [
                '.job-list-box > div', '.job-card-pc-container',
                '.content-left-section .job-card', '.job-list-box > a',
                '.job-list-box [class*="job-card"]', '[class*="job-list"] > div',
                '[class*="job-list"] > a', '.job-list-item',
            ];
            for (const sel of sels) {
                const n = document.querySelectorAll(sel).length;
                if (n > 0) return { sel, n };
            }
            // 回退：检查 .job-list-box 的直接子元素
            const box = document.querySelector('.job-list-box, [class*="job-list"]');
            if (box) return { sel: 'container.children', n: box.children.length };
            return { sel: 'none', n: 0 };
        }""") or {}
        cur_url = await self.engine.current_url(page)
        print(f"  [猎聘] 搜索后: URL={cur_url}, 卡片={card_count}", flush=True)

        if params.city:
            await self._select_city(page, params.city)
            await asyncio.sleep(gaussian_delay(1, 2))
        return page

    async def _click_next(self, page) -> bool:
        """猎聘翻页：.ant-pagination-next"""
        result = await self.eval_js(page, """() => {
            const next = document.querySelector(
                '.ant-pagination-next:not(.ant-pagination-disabled)'
            );
            if (next && next.offsetParent !== null) {
                next.querySelector('button, a')?.click() || next.click();
                return true;
            }
            return false;
        }""")
        return bool(result)

    async def _fetch_jd_text(self, job: JobInfo) -> str:
        """Override: 保留详情页供 apply_to_job 复用，避免重复打开。"""
        if self._pending_detail_page:
            try:
                await self._pending_detail_page.close()
            except Exception:
                pass
            self._pending_detail_page = None

        if not job.job_url:
            return ""
        try:
            self._pending_detail_page = await self.fetch_page(
                job.job_url, network_idle=True,
            )
            await asyncio.sleep(gaussian_delay(1.0, 2.0))
            jd_text = await self.eval_js(self._pending_detail_page, """() => {
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
                print(f"  [猎聘] 获取JD: {len(jd_text)}字", flush=True)
            return jd_text or ""
        except Exception as e:
            print(f"  [猎聘] 获取JD失败: {e}", flush=True)
            if self._pending_detail_page:
                try:
                    await self._pending_detail_page.close()
                except Exception:
                    pass
                self._pending_detail_page = None
            return ""

    async def apply_to_job(self, page, job, greeting, params) -> bool:
        """猎聘: 打开详情页 → 点击 a.btn-chat → 填写打招呼语 → 发送

        搜索结果页的"聊一聊"需要 hover 触发且不可靠。
        详情页有始终可见的 a.btn-chat 按钮（section.recruiter-container 内）。
        """
        if not job.job_url:
            print(f"  [猎聘] apply 失败: 无 job_url", flush=True)
            return False

        detail_page = self._pending_detail_page
        self._pending_detail_page = None
        try:
            if not detail_page:
                detail_page = await self.fetch_page(job.job_url, network_idle=True)
                await asyncio.sleep(gaussian_delay(1.0, 2.0))

            # Step 1: 找到并点击 a.btn-chat
            chat_btn_js = """() => {
                let btn = document.querySelector('a.btn-chat');
                if (!btn) btn = document.querySelector('[data-selector="chat-chat"]');
                if (!btn) {
                    const els = document.querySelectorAll('a, button, span, div');
                    const texts = ['聊一聊', '立即沟通', '继续沟通'];
                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        if (texts.includes(t) && el.offsetParent !== null
                            && el.offsetHeight > 20 && el.offsetHeight < 80
                            && el.offsetWidth > 30) {
                            btn = el; break;
                        }
                    }
                }
                if (!btn || !btn.offsetParent) {
                    return { found: false, reason: 'no_btn_chat' };
                }
                btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                if (btn.getAttribute('target')) btn.removeAttribute('target');
                let p = btn.parentElement;
                while (p) {
                    if (p.tagName === 'A' && p.getAttribute('target'))
                        p.removeAttribute('target');
                    p = p.parentElement;
                }
                const r = btn.getBoundingClientRect();
                return {
                    found: true, x: r.left, y: r.top, w: r.width, h: r.height,
                    text: (btn.textContent || '').trim().slice(0, 20),
                };
            }"""

            result = await self.human_click(detail_page, chat_btn_js)
            if not result.get("clicked"):
                reason = result.get("reason", "unknown")
                print(f"  [猎聘] 详情页未找到聊一聊: {reason}", flush=True)
                return False

            print(f"  [猎聘] 点击了: '{result.get('text', '')}'", flush=True)
            await asyncio.sleep(gaussian_delay(2.0, 3.0))

            # Step 2: 检测点击后状态
            post_state = await self.eval_js(detail_page, """() => {
                const modals = document.querySelectorAll(
                    '.ant-modal, .ant-drawer, [class*="modal"], [class*="dialog"], [class*="greet"]'
                );
                for (const m of modals) {
                    if (m.offsetParent !== null && m.getBoundingClientRect().height > 50) {
                        const ta = m.querySelector('textarea, [contenteditable="true"], input[type="text"]');
                        return { state: 'modal', hasInput: !!ta };
                    }
                }
                const tas = document.querySelectorAll('textarea, [contenteditable="true"]');
                for (const ta of tas) {
                    if (ta.offsetParent !== null && ta.getBoundingClientRect().height > 20) {
                        return { state: 'input_visible' };
                    }
                }
                if (location.href.includes('im.liepin') || location.href.includes('/im/')
                    || location.href.includes('/chat')) {
                    return { state: 'im_page', url: location.href };
                }
                const body = document.body?.textContent || '';
                if (body.includes('已沟通') || body.includes('已投递')) {
                    return { state: 'already_applied' };
                }
                return { state: 'unknown', url: location.href };
            }""") or {}

            state = post_state.get('state', 'unknown')
            print(f"  [猎聘] 点击后状态: {state}", flush=True)

            if state == 'already_applied':
                return True

            # Step 3: 填写打招呼语
            filled = False
            if greeting:
                safe_greeting = json.dumps(greeting)[1:-1]  # strip outer quotes
                filled = await self.eval_js(detail_page, f"""() => {{
                    const sels = [
                        '.ant-modal textarea', '.ant-drawer textarea',
                        '.greet-textarea', 'textarea[placeholder*="打招呼"]',
                        'textarea[placeholder*="沟通"]', 'textarea[placeholder*="聊一聊"]',
                        'textarea[placeholder*="发送"]', '.chat-input textarea',
                        '.dialog-content textarea', 'textarea',
                        '[contenteditable="true"]',
                    ];
                    for (const sel of sels) {{
                        const ta = document.querySelector(sel);
                        if (!ta || !ta.offsetParent) continue;
                        ta.focus();
                        if (ta.tagName === 'TEXTAREA') {{
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value'
                            ).set;
                            setter.call(ta, '{safe_greeting}');
                        }} else if (ta.tagName === 'INPUT') {{
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(ta, '{safe_greeting}');
                        }} else {{
                            ta.textContent = '{safe_greeting}';
                        }}
                        ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        ta.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }}""")
                if filled:
                    print(f"  [猎聘] 已填写打招呼语", flush=True)
                await asyncio.sleep(gaussian_delay(0.5, 1.0))

            # Step 4: 点击发送/确认
            confirm_find = self.build_modal_button_finder(
                button_selectors=(
                    '.ant-modal .ant-btn-primary, .ant-drawer .ant-btn-primary, '
                    '.dialog-confirm, .btn-primary, .submit-btn, '
                    '.send-btn, .btn-send, [class*="send"], [class*="submit"]'
                ),
                button_texts=['发送', '确认', '确定', '提交', '立即沟通', '开始沟通'],
            )
            confirm_result = await self.human_click(detail_page, confirm_find)
            confirmed = confirm_result.get("text") if confirm_result.get("clicked") else None
            if confirmed:
                print(f"  [猎聘] 已发送: '{confirmed}'", flush=True)
            await asyncio.sleep(gaussian_delay(0.8, 1.2))

            # 关闭弹窗
            await self.eval_js(detail_page, """() => {
                const close = document.querySelector(
                    '.ant-modal-close, .ant-drawer-close, .dialog-close, .modal-close'
                );
                if (close) close.click();
            }""")
            await asyncio.sleep(gaussian_delay(0.3, 0.5))
            return bool(confirmed) or bool(filled)

        except Exception as e:
            print(f"  [猎聘] apply 异常: {e}", flush=True)
            return False
        finally:
            if detail_page:
                try:
                    await detail_page.close()
                except Exception:
                    pass

    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        """猎聘搜索结果提取 — 基于 2026-05 实测 DOM 结构

        实际结构：
          .job-list-box > div(无class) > .job-card-pc-container
            ├─ .job-detail-box
            │   ├─ <a href="/job/xxx.shtml">  (职位链接)
            │   │   ├─ DIV.ellipsis-1           → 职位名
            │   │   ├─ SPAN.ellipsis-1           → 城市 (在【】内)
            │   │   └─ SPAN(hashed class)        → 薪资 (如"20-40k")
            │   └─ (公司区域，在 <a> 外面)
            │       ├─ IMG.company-logo-white-bg → 公司logo
            │       └─ SPAN.ellipsis-1           → 公司名
            └─ .job-card-right-box
                └─ .recruiter-info-box
                    ├─ IMG(40x40)                → HR头像
                    └─ DIV.ellipsis-1            → HR姓名
        """
        try:
            await asyncio.sleep(1)
            jobs_data = await self.eval_js(page, """() => {
                const cards = document.querySelectorAll('.job-card-pc-container');
                if (!cards.length) {
                    // 回退选择器
                    const fallbackSels = [
                        '.job-list-box > div', '[class*="job-list"] > div',
                    ];
                    for (const sel of fallbackSels) {
                        const found = document.querySelectorAll(sel);
                        if (found.length > 0)
                            return { matchedSel: sel, count: found.length, items: [] };
                    }
                    return null;
                }

                const items = Array.from(cards).map(card => {
                    const linkEl = card.querySelector('a[href*="/job/"]')
                        || card.querySelector('a[href*="/a/"]');
                    const href = linkEl?.href || '';

                    // 标题：job-detail-box 内链接中的第一个 .ellipsis-1
                    const detailBox = card.querySelector('.job-detail-box') || card;
                    const linkArea = detailBox.querySelector('a[href*="/job/"]')
                        || detailBox.querySelector('a[href*="/a/"]');
                    let title = '';
                    if (linkArea) {
                        const titleEl = linkArea.querySelector('div.ellipsis-1');
                        title = titleEl?.textContent?.trim() || '';
                    }
                    if (!title) {
                        const firstE = card.querySelector('.ellipsis-1');
                        title = firstE?.textContent?.trim() || '';
                    }

                    // 城市：链接内【】括号中的 span.ellipsis-1
                    let city = '';
                    if (linkArea) {
                        const spans = linkArea.querySelectorAll('span.ellipsis-1');
                        for (const sp of spans) {
                            const prev = sp.previousElementSibling;
                            if (prev) {
                                const pChar = (prev.textContent || '').charCodeAt(0);
                                // U+3010 = 【  U+005B = [
                                if (pChar === 0x3010 || pChar === 0x5B) {
                                    city = sp.textContent?.trim() || '';
                                    break;
                                }
                            }
                        }
                    }
                    if (!city) {
                        const cm = card.textContent?.match(/[\\u3010\\[]([^\\u3011\\]]+)[\\u3011\\]]/);
                        if (cm) city = cm[1];
                    }

                    // 薪资：链接内匹配 数字-数字k 模式的 span
                    let salary = '';
                    if (linkArea) {
                        const allSpans = linkArea.querySelectorAll('span');
                        for (const sp of allSpans) {
                            const t = sp.textContent?.trim() || '';
                            if (/\\d+.*[kK万]/.test(t) || t === '面议') {
                                salary = t;
                                break;
                            }
                        }
                    }
                    if (!salary) {
                        const m = card.textContent?.match(/(\\d{1,3}-\\d{1,3}[kK][^\\s]*|面议)/);
                        if (m) salary = m[1];
                    }

                    // 公司名：logo 旁边的 span.ellipsis-1（在 <a> 之外）
                    let company = '';
                    const logoImg = card.querySelector('.company-logo-white-bg, img[class*="logo"]');
                    if (logoImg) {
                        const logoParent = logoImg.closest('div');
                        const wrapper = logoParent?.parentElement;
                        if (wrapper) {
                            const compSpan = wrapper.querySelector('span.ellipsis-1');
                            company = compSpan?.textContent?.trim() || '';
                        }
                    }
                    if (!company) {
                        const compEl = card.querySelector('[class*="company"]');
                        if (compEl) {
                            const sp = compEl.querySelector('span.ellipsis-1')
                                || compEl.querySelector('.ellipsis-1');
                            company = sp?.textContent?.trim()
                                || compEl.textContent?.trim().split(/\\s/)[0] || '';
                        }
                    }

                    // 经验 + 学历：直接子 span（hashed class），不在 div 内
                    let info = '';
                    if (linkArea) {
                        const spans = linkArea.querySelectorAll(':scope > span');
                        const infoTexts = [];
                        for (const sp of spans) {
                            const t = sp.textContent?.trim() || '';
                            if (/\d+.*年|应届|经验/.test(t)) infoTexts.push(t);
                            else if (/博士|硕士|本科|大专|学历/.test(t)) infoTexts.push(t);
                        }
                        if (infoTexts.length) info = infoTexts.join(' / ');
                    }
                    if (!info) {
                        // 回退：在整个 linkArea 文本中查找
                        const full = linkArea?.textContent || '';
                        const expM = full.match(/(\d+-?\d*年[^\s]*|应届[^\s]*)/);
                        const eduM = full.match(/(博士|硕士|本科|大专)/);
                        const parts = [];
                        if (expM) parts.push(expM[1]);
                        if (eduM) parts.push(eduM[1]);
                        if (parts.length) info = parts.join(' / ');
                    }

                    // 公司详情（行业/规模/融资）
                    const compDetailEl = card.querySelector('div.ellipsis-1:not(a div.ellipsis-1)');
                    let companyInfo = '';
                    if (compDetailEl && !compDetailEl.closest('a')) {
                        companyInfo = compDetailEl.textContent?.trim() || '';
                    }

                    // logo
                    const logoSrc = logoImg?.src || '';

                    // job_id
                    const jobId = (href.match(/(?:job|a)\\/(\\d+)\\.shtml/)?.[1])
                        || card.closest('[data-tlg-ext]')
                            ?.getAttribute('data-tlg-ext')
                            ?.match(/"jobId":"(\\d+)"/)?.[1]
                        || '';

                    return {
                        job_id: jobId,
                        job_title: title,
                        salary: salary,
                        company_name: company,
                        job_url: href,
                        city: city,
                        info: info,
                        company_info: companyInfo,
                        tags: [],
                        company_logo: logoSrc,
                    };
                });

                return { matchedSel: '.job-card-pc-container', count: cards.length, items };
            }""")

            if not jobs_data:
                print(f"  [猎聘] extract: 页面无职位卡片", flush=True)
                return []

            if isinstance(jobs_data, dict) and "items" in jobs_data:
                matched_sel = jobs_data.get("matchedSel", "?")
                count = jobs_data.get("count", 0)
                print(f"  [猎聘] 匹配选择器: {matched_sel} ({count} cards)", flush=True)
                items = jobs_data["items"]
            else:
                items = jobs_data if isinstance(jobs_data, list) else []

            jobs = []
            skipped = 0
            for data in items:
                if not data.get("job_title"):
                    skipped += 1
                    continue
                salary_min, salary_max, salary_type = self.parse_salary(
                    data["salary"]
                )
                job_id = data["job_id"] or (
                    f"liepin_{hashlib.md5(data['job_url'].encode('utf-8')).hexdigest()[:16]}"
                )
                jobs.append(JobInfo(
                    job_id=job_id, platform=self.platform_key,
                    platform_name=self.platform_cfg["name"],
                    job_url=data.get("job_url") or "",
                    job_title=data["job_title"],
                    company_name=data["company_name"] or "未知公司",
                    salary_range=data.get("salary") or None,
                    salary_min=salary_min, salary_max=salary_max,
                    salary_type=salary_type,
                    city=data.get("city") or params.city,
                    experience_required=self._parse_info(data.get("info", ""), "exp"),
                    education_required=self._parse_info(data.get("info", ""), "edu"),
                    tags=data.get("tags") or [],
                    company_logo=data.get("company_logo") or None,
                ))
            if skipped:
                print(f"  [猎聘] 提取: {len(jobs)} 条有效, {skipped} 条跳过(无标题)", flush=True)
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
                    description: g('.job-intro-container') || g('.job-description'),
                    company_intro: g('.company-intro') || g('.company-description'),
                    address: g('.job-address') || g('.work-address'),
                };
            }""") or {}
        except Exception:
            return {}

    def _parse_info(self, text: str, field: str) -> "str | None":
        if not text:
            return None
        parts = text.replace("/", " ").replace("|", " ").split()
        if field == "exp":
            for p in parts:
                if "年" in p or "应届" in p:
                    return p.strip()
        elif field == "edu":
            for level in ["博士", "硕士", "本科", "大专"]:
                if level in text:
                    return level
        return None
