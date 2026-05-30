"""筛选与薪资解析混入 — 经验/学历区间、薪资文本解析

从 base.py 拆出。保持方法签名、字典定义、解析逻辑一致。
"""
import re

from models.job import SearchParams, JobInfo


# 经验区间 → (用户能匹配的最低年限, 最高年限) — 模块级常量，便于跨模块复用
_EXP_RANGE = {
    "应届生": (0, 1),
    "1年以下": (0, 1),
    "1-3年": (1, 3),
    "3-5年": (3, 5),
    "5-10年": (5, 10),
    "10年以上": (10, 99),
}

# 学历等级 — 模块级常量
_EDU_LEVEL = {
    "不限": 0, "经验不限": 0, "学历不限": 0,
    "中专": 1, "高中": 1,
    "大专": 2, "专科": 2,
    "本科": 3, "学士": 3,
    "硕士": 4, "研究生": 4, "mba": 4, "MBA": 4,
    "博士": 5,
}


class FilterMixin:
    """职位筛选 + 薪资文本解析 (供 BaseAgent 继承)。"""

    # 类属性别名，保持原 base.py 的 cls._EXP_RANGE / cls._EDU_LEVEL 访问方式
    _EXP_RANGE = _EXP_RANGE
    _EDU_LEVEL = _EDU_LEVEL

    @classmethod
    def _parse_job_exp_range(cls, text: str) -> tuple:
        """从岗位经验文本中解析出 (最低年限, 最高年限)；不限/无 → (0, 99)"""
        if not text:
            return (0, 99)
        t = text.strip().lower().replace(" ", "")
        if "不限" in t or "应届" in t or "在校" in t or "无经验" in t:
            return (0, 1) if ("应届" in t or "在校" in t) else (0, 99)
        # 形如 "1-3年" / "3-5年" / "5-10年"
        m = re.search(r"(\d+)\s*[-~到]\s*(\d+)", t)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        # "10年以上" / "5年+"
        m = re.search(r"(\d+)\s*年?\s*(以上|\+)", t)
        if m:
            return (int(m.group(1)), 99)
        # "3年" 单值
        m = re.search(r"(\d+)\s*年", t)
        if m:
            n = int(m.group(1))
            return (n, n)
        return (0, 99)

    @classmethod
    def _edu_level(cls, text: str) -> int:
        """学历文本 → 数字等级；未识别返回 0（不限）"""
        if not text:
            return 0
        t = text.strip().lower().replace(" ", "")
        for k, v in cls._EDU_LEVEL.items():
            if k.lower() in t:
                return v
        return 0

    def job_matches_filters(self, job: JobInfo, params: SearchParams) -> tuple:
        """检查职位是否满足筛选条件。
        返回 (是否匹配, 不匹配原因)
        """
        # 标题黑名单关键词
        # 「广告」一刀切会误杀广告投放工程师 / 广告平台开发 / 广告算法等技术岗，
        # 因此黑名单只在标题不含技术关键词时才生效。
        _TITLE_BLACKLIST = ["广告"]
        _TECH_WHITELIST = ("工程师", "开发", "算法", "架构", "技术", "研发",
                           "platform", "developer", "engineer")
        title = job.job_title or ""
        title_lower = title.lower()
        for kw in _TITLE_BLACKLIST:
            if kw in title and not any(t in title_lower for t in _TECH_WHITELIST):
                return False, f"标题含屏蔽词: {kw}"

        # 城市
        if params.city and job.city:
            from core.city import normalize_city
            user_city = normalize_city(params.city)
            job_city = normalize_city(job.city)
            if user_city and job_city and user_city != job_city:
                if user_city not in job_city and job_city not in user_city:
                    return False, f"城市不符({job.city} ≠ {params.city})"

        # 薪资
        if params.salary_min and job.salary_max:
            if job.salary_max < params.salary_min:
                return False, f"薪资不符(最高{job.salary_max:g}K<{params.salary_min}K)"
        if params.salary_max and job.salary_min:
            if job.salary_min > params.salary_max:
                return False, f"薪资不符(最低{job.salary_min:g}K>{params.salary_max}K)"

        # 经验：多选 OR 语义 — 岗位区间与任一所选区间相交即通过
        exp_selected = [e for e in (getattr(params, "experiences", None) or []) if e and e != "不限"]
        if not exp_selected and params.experience and params.experience != "不限":
            exp_selected = [params.experience]
        if exp_selected:
            job_lo, job_hi = self._parse_job_exp_range(job.experience_required)
            if not any(
                not (job_hi < self._EXP_RANGE.get(e, (0, 99))[0] or job_lo > self._EXP_RANGE.get(e, (0, 99))[1])
                for e in exp_selected
            ):
                return False, f"经验不符(岗位:{job.experience_required or '?'} vs 用户:{'/'.join(exp_selected)})"

        # 学历：多选 OR 语义 — 任一所选学历达到或超过岗位要求即通过
        edu_selected = [e for e in (getattr(params, "educations", None) or []) if e and e != "不限"]
        if not edu_selected and params.education and params.education != "不限":
            edu_selected = [params.education]
        if edu_selected:
            job_lv = self._edu_level(job.education_required)
            if job_lv and not any(self._edu_level(e) >= job_lv for e in edu_selected):
                return False, f"学历不符(岗位:{job.education_required} > 用户:{'/'.join(edu_selected)})"

        return True, ""

    # ──────── 薪资解析 ────────

    def parse_salary(self, salary_text: str) -> tuple:
        if not salary_text:
            return None, None, None
        text = salary_text.strip().lower()
        if "面议" in text:
            return None, None, "面议"

        # 提取多薪月数（14薪、15薪等），默认 12
        months = 12
        m_months = re.search(r"(\d+)\s*薪", text)
        if m_months:
            months = int(m_months.group(1))

        if "年" in text:
            m = re.search(r"(\d+(?:\.\d+)?)\s*万?\s*[-~到]\s*(\d+(?:\.\d+)?)\s*万", text)
            if m:
                return float(m.group(1)), float(m.group(2)), "年薪"
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*万", text)
            if len(nums) >= 2:
                return float(nums[0]), float(nums[1]), "年薪"
            return None, None, "年薪"
        # "20-35k" / "20k-35k" — 先尝试匹配区间
        m = re.search(r"(\d+(?:\.\d+)?)\s*k?\s*[-~到]\s*(\d+(?:\.\d+)?)\s*k", text)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            if months != 12:
                lo = lo * months / 12
                hi = hi * months / 12
            return lo, hi, "月薪"
        nums_k = re.findall(r"(\d+(?:\.\d+)?)\s*k", text)
        if nums_k:
            nums = [float(n) * months / 12 for n in nums_k]
            return min(nums), max(nums), "月薪"
        # "30-60万" — 区间匹配
        m = re.search(r"(\d+(?:\.\d+)?)\s*万?\s*[-~到]\s*(\d+(?:\.\d+)?)\s*万", text)
        if m:
            return float(m.group(1)) * 10, float(m.group(2)) * 10, "月薪"
        nums_w = re.findall(r"(\d+(?:\.\d+)?)\s*万", text)
        if nums_w:
            nums = [float(n) * 10 for n in nums_w]
            return min(nums), max(nums), "月薪"
        nums_raw = re.findall(r"(\d{4,6})", text)
        if nums_raw:
            nums = [float(n) / 1000 for n in nums_raw]
            return min(nums), max(nums), "月薪"
        return None, None, None
