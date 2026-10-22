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

from models.job import SearchParams, JobInfo, ApplyOutcome
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
        return await super()._check_anti_bot(page)

    async def apply_to_job(self, page, job, greeting, params) -> ApplyOutcome:
        """前程无忧: 点击对应职位卡片的"申请/投递"按钮。

        51job 无 web 聊天框，招呼语物理上不可注入；这里只判定投递回执。
        """
        draft = (getattr(params, "apply_mode", "auto") or "auto").lower() == "draft"
        if draft:
            self._apply_detail = "草稿未发送"
            return ApplyOutcome.UNCERTAIN

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
            if reason == "already":
                return ApplyOutcome.ALREADY
            self._apply_detail = f"[环境] 未找到投递按钮({reason})"
            print(f"  [前程无忧] apply 失败: {reason}", flush=True)
            return ApplyOutcome.FAILED

        print(f"  [前程无忧] 鼠标点击了按钮: '{result.get('text', '')}'", flush=True)

        popup_sel = (
            '.el-dialog, .el-message-box, .ant-modal, .dialog, .modal, '
            '[class*="dialog"], [class*="popup"]'
        )
        try:
            await page.wait_for_selector(popup_sel, timeout=3000, state="visible")
        except Exception:
            pass

        if await self._check_anti_bot(page):
            return ApplyOutcome.BLOCKED

        popup_info = await self.eval_js(page, """() => {
            const sels = [
                '.el-dialog', '.el-message-box', '.ant-modal',
                '.dialog', '.modal', '.popup',
                '[class*="dialog"]:not([style*="display: none"])',
                '[class*="modal"]:not([style*="display: none"])',
                '[class*="popup"]:not([style*="display: none"])',
            ];
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    if (el.offsetParent === null
                        && window.getComputedStyle(el).display === 'none') continue;
                    const text = (el.textContent || '').trim().slice(0, 200);
                    if (!text) continue;
                    let closeBtn = el.querySelector(
                        '.el-dialog__close, .el-message-box__close, .ant-modal-close, '
                        + '.close, .close-btn, [class*="close"], '
                        + 'button[aria-label="关闭"], button[aria-label="Close"], '
                        + '.icon-close, .btn-close'
                    );
                    if (!closeBtn) {
                        closeBtn = el.parentElement?.querySelector(
                            '[class*="close"], .icon-close, .btn-close'
                        );
                    }
                    if (closeBtn) closeBtn.click();
                    return {found: true, text: text};
                }
            }
            // 无弹窗：检查卡片按钮是否已变成“已申请/已投递”
            const t = document.body ? document.body.innerText : '';
            if (/已申请|已投递/.test(t)) return {found: true, text: '已申请'};
            return {found: false};
        }""")
        text = (popup_info or {}).get("text", "") or ""

        if text:
            print(f"  [前程无忧] 投递回执: '{text[:60]}'", flush=True)
            if any(k in text for k in ("成功", "已投递", "已申请")):
                return ApplyOutcome.SUCCESS
            if any(k in text for k in ("已下线", "停止招聘", "职位不存在")):
                self._apply_detail = f"[下架] {text[:40]}"
                return ApplyOutcome.FAILED
            if "登录" in text:
                self._apply_detail = "[登录] 需要登录"
                return ApplyOutcome.FAILED
            if any(k in text for k in ("频繁", "异常", "验证")):
                self._apply_detail = f"[风控] {text[:40]}"
                return ApplyOutcome.FAILED
            if any(k in text for k in ("简历", "完善", "失败")):
                self._apply_detail = f"[状态] {text[:40]}"
                return ApplyOutcome.FAILED
            self._apply_detail = f"未识别回执: {text[:40]}"
            return ApplyOutcome.UNCERTAIN

        # 无弹窗、也无“已申请”回执
        print(f"  [前程无忧] 直接投递（无明确回执）", flush=True)
        self._apply_detail = "点击后无弹窗/无回执"
        return ApplyOutcome.UNCERTAIN

    async def extract_list_page(self, page, params: SearchParams) -> list[JobInfo]:
        try:
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
