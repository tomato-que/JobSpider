"""前程无忧(51job)平台子智能体 — 浏览器交互版

流程：打开 51job.com → 选城市 → 输关键词 → 点搜索 → 提取职位 → 翻页
选择器来源：probe_dom.py 实测
  - 搜索框: #search-input (.el-input__inner, ph="请输入公司/职位")
  - 搜索按钮: .search-btn (text="搜索")
  - 城市: .location (text="全国"), .change-city (text="[切换]")
"""
import asyncio
import hashlib
import re

from models.job import SearchParams, JobInfo
from agents.base import BaseAgent
from config import gaussian_delay


class Job51Agent(BaseAgent):

    SEARCH_INPUT_SELECTORS = [
        '#search-input',
        '.el-input__inner',
        'input[placeholder*="公司"]',
        'input[placeholder*="职位"]',
        'input[placeholder*="搜索"]',
    ]
    SEARCH_BUTTON_SELECTORS = [
        '.search-btn',
        '[class*="search-btn"]',
        'button[type="submit"]',
    ]
    RESULT_CONTAINER_SELECTOR = '.joblist-box, .j_joblist, .sensorsdata-dig, .job-card-wrapper'
    DIAGNOSE_CARD_SELECTOR = '.joblist-item, .j_joblist .e, .resultList .el'
    CITY_TRIGGERS = ['.change-city', '.location', '[class*="city"]']
    SCROLL_TIMES_FIRST = 3
    PAGE_TURN_DELAY = (3.0, 6.0)

    def __init__(self):
        super().__init__("job51")

    async def _check_login_wall(self, page) -> bool:
        return bool(await self.eval_js(page, """() => {
            const m = document.querySelector(
                '.login-dialog, .login-box, .qs-dialog-login, '
                + '[class*="login-modal"], [class*="login-popup"]'
            );
            if (m && window.getComputedStyle(m).display !== 'none') return true;
            if (location.href.includes('/login') || location.href.includes('/passport'))
                return true;
            return false;
        }"""))

    async def _check_anti_bot(self, page) -> bool:
        try:
            url = await self.engine.current_url(page)
            if any(k in (url or "").lower() for k in ("captcha", "verify", "wapsec")):
                self.log(f"  [前程无忧] 🚨 anti-bot URL detected: {url[:80]}")
                return True
            hit = await self.eval_js(page, """() => {
                const sel = [
                    '.swiper-wrapper.captcha', '.verify-slider',
                    '.yidun_modal', '.yidun-popup', '.verify-wrap',
                    '.captcha-box', '[class*="captcha"]',
                    '.login-dialog.show', '.qs-dialog-login.show',
                ];
                for (const s of sel) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null
                        && el.offsetWidth > 50 && el.offsetHeight > 50) {
                        return el.className || s;
                    }
                }
                const t = document.title || '';
                if (t.includes('验证') || t.includes('拦截')) return 'title:' + t.slice(0, 30);
                const body = (document.body?.innerText || '').slice(0, 4000);
                if (/请完成.{0,4}验证|滑动.{0,4}验证|安全验证|系统检测到|访问过于频繁|访问太频繁/.test(body))
                    return 'text_match';
                return '';
            }""")
            if hit:
                self.log(f"  [前程无忧] 🚨 anti-bot DOM: {hit}")
                return True
        except Exception as e:
            print(f"  [51job anti-bot check] {e}", flush=True)
        return False

    async def apply_to_job(self, page, job, greeting, params) -> bool:
        """前程无忧: 点击对应职位卡片的"申请/投递"按钮

        51job 点击"投递"后弹出结果弹窗，需关闭。按钮变为"已申请"即成功。
        """
        find_js = self.build_card_button_finder(
            job,
            card_selectors=(
                '.sensorsdata-dig, .joblist-item, .j_joblist .e, '
                '.job-card-wrapper, .resultList .el, .joblist .item'
            ),
            title_selectors=(
                '.jname, .job-title, .t1 span a, .job-name, '
                '.title-text, a .name, [class*="job-name"]'
            ),
            company_selectors=(
                '.cname, .company-name, .t2 a, .com-name, [class*="company-name"]'
            ),
            button_selectors=(
                '.btn.apply, .apply-btn, .btn-apply, '
                '[class*="apply"], a[title="申请职位"]'
            ),
            button_texts=['投递', '申请职位', '申请', '立即申请', '立即投递'],
        )
        await asyncio.sleep(gaussian_delay(0.3, 0.6))
        result = await self.human_click(page, find_js)
        if not result.get("clicked"):
            reason = result.get("reason", "unknown")
            print(f"  [前程无忧] apply 失败: {reason}", flush=True)
            return False

        print(f"  [前程无忧] 鼠标点击了按钮: '{result.get('text', '')}'", flush=True)
        await asyncio.sleep(gaussian_delay(2.0, 3.0))

        # 检测投递后的弹窗并关闭
        popup_info = await self.eval_js(page, """() => {
            // 检测所有可能的弹窗/对话框
            const sels = [
                '.el-dialog', '.el-message-box', '.ant-modal',
                '.dialog', '.modal', '.popup',
                '[class*="dialog"]:not([style*="display: none"])',
                '[class*="modal"]:not([style*="display: none"])',
                '[class*="popup"]:not([style*="display: none"])',
            ];
            for (const sel of sels) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (el.offsetParent === null && window.getComputedStyle(el).display === 'none') continue;
                    const text = el.textContent?.trim().slice(0, 100) || '';
                    // 找关闭按钮（先在弹窗内找，再在文档级找）
                    let closeBtn = el.querySelector(
                        '.el-dialog__close, .el-message-box__close, .ant-modal-close, '
                        + '.close, .close-btn, [class*="close"], '
                        + 'button[aria-label="关闭"], button[aria-label="Close"], '
                        + '.icon-close, .btn-close'
                    );
                    if (!closeBtn) {
                        // X 可能在弹窗外层（遮罩层直接子元素）
                        closeBtn = el.parentElement?.querySelector(
                            '[class*="close"], .icon-close, .btn-close'
                        );
                    }
                    if (!closeBtn) {
                        // 文档级：找投递成功弹窗附近的关闭按钮
                        closeBtn = document.querySelector(
                            '.el-dialog__close, .ant-modal-close, '
                            + '.zppp-close, [class*="dialog"] [class*="close"], '
                            + '[class*="modal"] [class*="close"], '
                            + '[class*="popup"] [class*="close"]'
                        );
                    }
                    if (closeBtn) closeBtn.click();
                    const isSuccess = text.includes('成功') || text.includes('已投递')
                        || text.includes('已申请') || text.includes('投递成功');
                    const isError = text.includes('失败') || text.includes('登录')
                        || text.includes('频繁') || text.includes('异常');
                    return { found: true, text: text.slice(0, 60), success: isSuccess, error: isError, closed: !!closeBtn };
                }
            }
            return { found: false };
        }""")

        if popup_info and popup_info.get("found"):
            print(f"  [前程无忧] 弹窗: '{popup_info.get('text', '')}' "
                  f"(closed={popup_info.get('closed')})", flush=True)
            await asyncio.sleep(gaussian_delay(0.3, 0.5))
            if popup_info.get("error"):
                return False
            return True

        # 无弹窗 — 某些岗位可能直接提交无反馈
        # 检查是否有登录弹窗等错误
        has_error = await self.eval_js(page, """() => {
            const loginDlg = document.querySelector(
                '.login-dialog, [class*="login-modal"], [class*="login-pop"]'
            );
            if (loginDlg && loginDlg.offsetParent !== null) return 'login';
            return '';
        }""")
        if has_error:
            print(f"  [前程无忧] 投递后检测到异常: {has_error}", flush=True)
            return False

        print(f"  [前程无忧] 直接投递（无弹窗反馈）", flush=True)
        return True

    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        try:
            await asyncio.sleep(1.5)
            jobs_data = await self.eval_js(page, """() => {
                // 新版 we.51job.com 搜索结果
                let items = document.querySelectorAll(
                    '.sensorsdata-dig, .joblist-item, .j_joblist .e, '
                    + '.job-card-wrapper, .resultList .el'
                );
                if (items.length === 0) {
                    items = document.querySelectorAll('.dw_table .el');
                }
                return Array.from(items).map(el => {
                    const linkEl = el.querySelector(
                        'a[href*="/job/"], a[href*="jobs.51job"]'
                    );
                    const titleEl = el.querySelector(
                        '.jname, .job-title, .t1 span a, .job-name, '
                        + '.title-text, a .name, [class*="job-name"]'
                    );
                    const salaryEl = el.querySelector(
                        '.sal, .salary, .t4, .job-salary, [class*="salary"]'
                    );
                    const companyEl = el.querySelector(
                        '.cname, .company-name, .t2 a, .com-name, [class*="company-name"]'
                    );
                    const cityEl = el.querySelector(
                        '.info span, .city, .work-city, .t3, .job-area, [class*="area"]'
                    );
                    const infoEl = el.querySelector(
                        '.info, .job-info, .job-require, .d_at, [class*="job-info"]'
                    );
                    return {
                        position_id: el.getAttribute('data-jid')
                            || el.getAttribute('data-jobid')
                            || (linkEl?.href?.match(/\\/([a-zA-Z0-9]+)\\.html/)?.[1])
                            || '',
                        job_title: titleEl?.textContent?.trim() || '',
                        salary: salaryEl?.textContent?.trim() || '',
                        company_name: companyEl?.textContent?.trim() || '',
                        job_url: linkEl?.href || '',
                        city: cityEl?.textContent?.trim() || '',
                        info: infoEl?.textContent?.trim() || '',
                        pub_time: (el.querySelector(
                            '.time, .publish-time, .t5, .job-time'
                        ) || {}).textContent?.trim() || '',
                    };
                });
            }""")

            if not jobs_data:
                return []

            jobs = []
            for data in jobs_data:
                if not data["job_title"] or not data["company_name"]:
                    continue
                if data.get("pub_time") == "发布时间" and not data.get("position_id"):
                    continue
                salary_min, salary_max, salary_type = self._parse_51job_salary(
                    data.get("salary", "")
                )
                if salary_type == "skip":
                    continue
                job_id = data.get("position_id") or (
                    f"51job_{hashlib.md5(data['job_url'].encode('utf-8')).hexdigest()[:16]}"
                )
                jobs.append(JobInfo(
                    job_id=job_id, platform=self.platform_key,
                    platform_name=self.platform_cfg["name"],
                    job_url=data.get("job_url") or "",
                    job_title=data["job_title"],
                    company_name=data["company_name"],
                    salary_range=data.get("salary") or None,
                    salary_min=salary_min, salary_max=salary_max,
                    salary_type=salary_type,
                    city=data.get("city") or params.city,
                    experience_required=self._parse_info(data.get("info", ""), "exp"),
                    education_required=self._parse_info(data.get("info", ""), "edu"),
                    publish_time=data.get("pub_time") or None,
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
                    description: g('.job_msg') || g('.bmsg') || g('.job-detail'),
                    company_info: g('.com_msg') || g('.tmsg'),
                    address: g('.bmsg .fp') || g('.work-address'),
                };
            }""") or {}
        except Exception:
            return {}

    def _parse_51job_salary(self, salary_text: str) -> tuple:
        if not salary_text or not salary_text.strip():
            return None, None, None
        text = salary_text.strip()
        if "面议" in text:
            return None, None, "面议"
        if "元/天" in text:
            return None, None, "skip"
        if "万/年" in text:
            nums = [n.strip() for n in text.replace("万/年", "").split("-")]
            if len(nums) >= 2:
                try:
                    return (round(float(nums[0]) * 10 / 12, 1),
                            round(float(nums[1]) * 10 / 12, 1), "月薪")
                except ValueError:
                    pass
            return None, None, None
        if "万/月" in text:
            nums = [n.strip() for n in text.replace("万/月", "").split("-")]
            if len(nums) >= 2:
                try:
                    return (round(float(nums[0]) * 10, 1),
                            round(float(nums[1]) * 10, 1), "月薪")
                except ValueError:
                    pass
            return None, None, None
        if "千/月" in text:
            nums = [n.strip() for n in text.replace("千/月", "").split("-")]
            if len(nums) >= 2:
                try:
                    return float(nums[0]), float(nums[1]), "月薪"
                except ValueError:
                    pass
            return None, None, None
        return self.parse_salary(text)

    def _parse_info(self, text: str, field: str) -> "str | None":
        if not text:
            return None
        parts = text.replace("|", " ").replace("·", " ").split()
        if field == "exp":
            for p in parts:
                if "年" in p or "应届" in p:
                    return p.strip()
        elif field == "edu":
            for level in ["博士", "硕士", "本科", "大专", "高中"]:
                if level in text:
                    return level
        return None
