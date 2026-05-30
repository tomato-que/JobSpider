"""标准化数据模型"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class ScrapeStatus(Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SearchParams:
    """标准化搜索参数"""
    keyword: str                          # 职位关键词
    city: Optional[str] = None            # 城市
    salary_min: Optional[int] = None      # 最低薪资(K)
    salary_max: Optional[int] = None      # 最高薪资(K)
    experience: Optional[str] = None      # 经验要求: 应届生/1-3年/3-5年/5-10年/不限
    education: Optional[str] = None       # 学历要求: 大专/本科/硕士/博士/不限
    industry: Optional[str] = None        # 行业
    company_type: Optional[str] = None    # 公司类型
    platforms: list[str] = field(default_factory=lambda: ["boss", "zhilian", "job51", "liepin"])
    auto_apply: bool = True                   # 自动投递开关（默认开启）
    greeting_template: Optional[str] = None   # 自定义打招呼语模板（None则用LLM生成）
    # 新增：投递规则（自然语言，由 LLM 解析判断是否跳过某个岗位）
    exclude_rules: Optional[str] = None       # 不投递条件，如"外包、需要出差、薪资<15K"
    # 新增：每个平台的投递上限（None 表示不限）
    platform_limits: dict = field(default_factory=dict)  # {"boss": 10, "zhilian": 5, ...}
    # 投递速率：slow / normal / fast / turbo
    apply_rate: str = "normal"
    # 全局/每日上限（防封号）
    global_limit: Optional[int] = None              # 整个任务全局投递上限（跨平台跨组合），None=不限
    daily_limit_per_platform: dict = field(default_factory=dict)
                                                    # {"boss": 100, ...} — 每个平台每 24h 滚动窗口的投递上限
                                                    # 与 platform_limits 不同：daily_limit 由 DB 历史聚合得出，跨任务/跨重启生效
    # 工作时间窗（24h制，闭区间）— 默认 (9, 22) 即 9:00-22:00 投递；None=全天
    active_hours: Optional[tuple] = None
    # Burst 长休：累计投递 N 条后强制歇 30-90 分钟
    burst_pause_after: Optional[int] = None         # None=禁用
    burst_pause_minutes: tuple = (30, 90)           # (lo, hi) 分钟，gaussian 抽样
    # 同公司每日最多投几个不同岗位（避免对同一公司刷屏）
    max_per_company_per_day: Optional[int] = None   # None=不限
    # 预演模式：跑完全流程但不点真按钮，apply_status 标 "dry_run"
    dry_run: bool = False
    # 经验/学历多选（OR 语义：命中任一即放行）。空列表=不限。
    # 与旧的单值 experience/education 共存：_filters.py 会优先用列表，列表为空时回退到单值。
    experiences: list = field(default_factory=list)   # 如 ["1-3年", "5-10年"]
    educations: list = field(default_factory=list)    # 如 ["本科", "硕士"]
    # 跨任务去重：True=跳过 7 天内已真实投递过的同一 job_id（防重复打招呼/防配额浪费）
    skip_applied: bool = True

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != {}}


@dataclass
class JobInfo:
    """标准化职位信息"""
    # 核心标识
    job_id: str                                       # 平台内部职位ID
    platform: str                                     # 来源平台
    platform_name: str                                # 平台中文名
    job_url: str                                      # 职位详情页URL

    # 基础信息
    job_title: str                                    # 职位名称
    company_name: str                                 # 公司名称

    # 薪资 (单位: K/月)
    salary_range: Optional[str] = None                # 原始薪资描述 "15k-25k"
    salary_min: Optional[float] = None                # 最低月薪(K)
    salary_max: Optional[float] = None                # 最高月薪(K)
    salary_type: Optional[str] = None                 # 薪资类型: 月薪/年薪/面议

    # 工作地点
    city: Optional[str] = None                        # 城市
    district: Optional[str] = None                    # 区域
    address: Optional[str] = None                     # 详细地址

    # 要求
    experience_required: Optional[str] = None         # 经验要求
    education_required: Optional[str] = None          # 学历要求
    skills_required: list[str] = field(default_factory=list)  # 技能要求

    # 详情
    job_description: Optional[str] = None             # 职位描述
    job_requirements: Optional[str] = None            # 任职要求
    benefits: list[str] = field(default_factory=list) # 福利待遇
    tags: list[str] = field(default_factory=list)     # 职位标签

    # 公司信息
    company_size: Optional[str] = None                # 公司规模
    company_industry: Optional[str] = None            # 公司行业
    company_type: Optional[str] = None                # 公司类型(外企/民营/国企)
    company_logo: Optional[str] = None                # Logo URL

    # HR信息
    hr_name: Optional[str] = None                     # HR姓名
    hr_position: Optional[str] = None                 # HR职位

    # 时效
    publish_time: Optional[str] = None                # 发布时间
    scrape_time: str = field(default_factory=lambda: datetime.now().isoformat())

    # 投递状态
    applied: bool = False                             # 是否已投递
    apply_status: Optional[str] = None                # applied/skipped/failed/login_required/dry_run
    greeting_sent: Optional[str] = None               # 发送的打招呼语
    apply_skip_reason: Optional[str] = None           # 跳过/失败原因（如 LLM 规则匹配）

    def __post_init__(self):
        _STR_FIELDS = (
            "salary_range", "salary_type", "city", "district", "address",
            "experience_required", "education_required", "job_description",
            "job_requirements", "company_size", "company_industry",
            "company_type", "company_logo", "hr_name", "hr_position",
            "publish_time", "apply_status", "greeting_sent", "apply_skip_reason",
        )
        for fname in _STR_FIELDS:
            val = getattr(self, fname, None)
            if val is not None and isinstance(val, str) and not val.strip():
                object.__setattr__(self, fname, None)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def dedup_key(self) -> str:
        """生成去重键：公司名+职位名+归一化城市（不含薪资，因各平台格式不同）"""
        from core.city import normalize_city
        return f"{self.company_name}|{self.job_title}|{normalize_city(self.city or '')}"

    @property
    def avg_salary(self) -> Optional[float]:
        """计算平均薪资"""
        if self.salary_min and self.salary_max:
            return (self.salary_min + self.salary_max) / 2
        if self.salary_min:
            return self.salary_min
        if self.salary_max:
            return self.salary_max
        return None


@dataclass
class ScrapeResult:
    """单个平台的抓取结果"""
    platform: str
    platform_name: str
    status: ScrapeStatus
    total_count: int = 0
    jobs: list[JobInfo] = field(default_factory=list)
    error_message: Optional[str] = None
    scrape_time: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "platform_name": self.platform_name,
            "status": self.status.value,
            "total_count": self.total_count,
            "jobs": [j.to_dict() for j in self.jobs],
            "error_message": self.error_message,
            "scrape_time": self.scrape_time,
        }
