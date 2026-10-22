"""全局配置文件 - 基于真实平台逆向工程经验"""
import os
import random
import sys
from pathlib import Path

# 强制 stdout/stderr 使用 UTF-8（errors=replace）— 否则在 Windows GBK 控制台
# 或 stdout 被重定向（如 uvicorn 后台运行）时，日志里的 emoji（✓✗◐⏸🚨 等）
# 会触发 UnicodeEncodeError 直接中断投递任务。config 被几乎所有模块最早导入，
# 在此 reconfigure 可覆盖全进程的打印路径。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 加载 .env 文件
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE, override=False)
    except ImportError:
        # Fallback parser (existing hand-rolled logic)
        with open(_ENV_FILE, "r", encoding="utf-8-sig") as f:
            for _line in f:
                _line = _line.strip()
                if _line.startswith("export "):
                    _line = _line[7:]
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _val = _line.split("=", 1)
                    _key = _key.strip()
                    _val = _val.strip().strip('"').strip("'")
                    if _key not in os.environ:
                        os.environ[_key] = _val

# 项目根目录
ROOT_DIR = Path(__file__).parent
OUTPUT_DIR = ROOT_DIR / "output"

# ===== LLM 配置 =====
# 使用函数读取，确保 /api/config 修改后立即生效（不再冻结为模块级常量）
def get_llm_provider() -> str:
    return os.getenv("LLM_PROVIDER", "")

def get_llm_model() -> str:
    return os.getenv("LLM_MODEL", "")

def get_llm_api_key() -> str:
    return os.getenv("LLM_API_KEY", "")

def get_llm_base_url() -> str:
    return os.getenv("LLM_BASE_URL", "")

def get_llm_enabled() -> bool:
    return os.getenv("LLM_ENABLED", "false").lower() == "true"

# 浏览器配置
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
BROWSER_TIMEOUT = 30000
PAGE_LOAD_TIMEOUT = 60000

# 抓取配置
MAX_PAGES_PER_PLATFORM = 10
REQUEST_DELAY_MIN = 2.0
REQUEST_DELAY_MAX = 5.0
MAX_RETRIES = 3
RETRY_DELAY = 3.0

# 高斯抖动延迟 (参考boss-cli的反检测策略) — 投递场景使用，适当放慢
GAUSSIAN_JITTER_MEAN = 3.5
GAUSSIAN_JITTER_STD = 1.2

# 并发配置
MAX_CONCURRENT_PLATFORMS = 4

# 数据配置 — 仅 DEDUP_THRESHOLD 被实际引用（core/dedup.py）
DEDUP_THRESHOLD = 0.85

# 平台配置 (基于真实逆向经验)
PLATFORM_CONFIG = {
    "boss": {
        "name": "Boss直聘",
        "base_url": "https://www.zhipin.com",
        "search_url": "https://www.zhipin.com/web/geek/job",
        # boss-cli 发现了完整的API层，搜索API端点为 /wapi/zpgeek/search/joblist.json
        "api_search_url": "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
        "detail_url": "https://www.zhipin.com/job_detail/{job_id}.html",
        "jobs_per_page": 30,
        "need_login": True,
        "anti_crawl": "strong",
        "cdp_required": True,
        # boss-cli 关键发现: __zp_stoken__ 是核心认证cookie，过期需重新登录
        "auth_cookie": "__zp_stoken__",
        # 反检测: 高斯抖动延迟 + 禁止并行请求
        "rate_limit_cooldown": [10, 20, 40, 60],  # 逐步递增冷却时间
    },
    "zhilian": {
        "name": "智联招聘",
        "base_url": "https://www.zhaopin.com",
        "search_url": "https://www.zhaopin.com/sou",
        # 参考项目发现可用旧域名 sou.zhaopin.com/jobs/searchresult.ashx
        "legacy_search_url": "https://sou.zhaopin.com/jobs/searchresult.ashx",
        "detail_url": "https://www.zhaopin.com/job/{job_id}.htm",
        "jobs_per_page": 60,
        "need_login": False,
        "anti_crawl": "medium",
        "cdp_required": False,
    },
    "job51": {
        "name": "前程无忧",
        "base_url": "https://www.51job.com",
        "search_url": "https://we.51job.com/pc/search",
        "detail_url": "https://jobs.51job.com/all/{job_id}.html",
        "jobs_per_page": 50,
        "need_login": False,
        "anti_crawl": "strong",
        "cdp_required": True,
    },
    "liepin": {
        "name": "猎聘",
        "base_url": "https://www.liepin.com",
        "search_url": "https://www.liepin.com/zhaopin/",
        "detail_url": "https://www.liepin.com/job/{job_id}.shtml",
        "jobs_per_page": 40,
        "need_login": True,
        "anti_crawl": "medium",
        "cdp_required": True,
    },
}

# (城市编码映射已移除 — 新版使用浏览器交互选择城市，不再需要硬编码)


def gaussian_delay(min_delay: float = 1.0, max_delay: float = 5.0) -> float:
    """高斯抖动延迟 — 按传入区间推导分布，保证窄区间也有抖动。

    旧实现固定从 N(3.5, 1.2) 抽样再 clamp，导致所有小于 ~2.3s 的区间
    （0.2–0.6、0.3–0.6、0.5–1.0）都恒等于上界，等于没有抖动。
    """
    lo, hi = float(min_delay), float(max_delay)
    if hi <= lo:
        return lo
    mean = (lo + hi) / 2.0
    std = max((hi - lo) / 4.0, 0.05)
    return max(lo, min(hi, random.gauss(mean, std)))


# 平台每 24h 滚动窗口的"硬上限"（参考各平台公开/经验数据）
# server.py 在 /api/scrape 接收 platform_limits 时会把每平台值 clamp 到此上限，避免用户误填导致封号
PLATFORM_DAILY_HARD_CAP = {
    "boss":    150,   # Boss 直聘官方每日招呼 150 条
    "zhilian":  80,   # 智联约 80-150 (取下限保守)
    "job51":   120,   # 51job 约 100-200 (取中位)
    "liepin":   80,   # 猎聘约 80
}

# 工作时间窗：默认推荐值 — 9:00-22:00 投递更像真人，半夜投递是强 bot 信号
DEFAULT_ACTIVE_HOURS = (9, 22)

# Burst 默认参数
DEFAULT_BURST_PAUSE_AFTER = 25       # 每投 25 条进入长休
DEFAULT_BURST_PAUSE_MINUTES = (30, 90)


def is_within_active_hours(active_hours) -> bool:
    """判断当前小时是否在投递窗口内。
    active_hours 为 None 或假值 → 全天投递（True）
    active_hours = (lo, hi) 闭区间，跨午夜也支持（hi < lo 表示 22-6 这种）
    """
    if not active_hours:
        return True
    try:
        lo, hi = int(active_hours[0]), int(active_hours[1])
    except Exception:
        return True
    from datetime import datetime
    h = datetime.now().hour
    if lo <= hi:
        return lo <= h <= hi
    # 跨午夜
    return h >= lo or h <= hi


def gaussian_minutes(lo_min: float, hi_min: float) -> float:
    """供 burst pause 等场景使用 — 高斯抽样的分钟数，clamp 到 [lo,hi]。"""
    import random
    mean = (lo_min + hi_min) / 2.0
    std = (hi_min - lo_min) / 4.0
    val = random.gauss(mean, std)
    return max(lo_min, min(hi_min, val))
