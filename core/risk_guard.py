"""平台风控信号识别与熔断。

信号来源：
  1. 主文档 URL（safe/captcha/verify/geetest/about:blank）
  2. 页面 DOM（验证码/登录弹窗 + 短正文风险词）
  3. 网络响应（401/403/429/503；Boss code 31/37 等由响应层判定）
  4. Boss 认证 cookie 缺失（仅作辅助，不单独熔断）

冷却写入 DB（platform_risk），跨任务生效；同一平台熔断不影响其他平台。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from core import db
from core.log import get_logger

log = get_logger("risk_guard")


PLATFORM_HOSTS = {
    "boss": ("zhipin.com",),
    "zhilian": ("zhaopin.com",),
    "job51": ("51job.com",),
    "liepin": ("liepin.com",),
}


class RiskSignal(Enum):
    OK = "ok"
    LOGIN_REQUIRED = "login_required"
    VERIFY = "verify"
    RATE_LIMITED = "rate_limited"
    ACCOUNT_RESTRICTED = "account_restricted"


PLATFORM_PATTERNS = {
    "boss": {
        "url_keys": ("/web/user/safe", "safe/", "captcha", "verify", "geetest"),
        "verify_selectors": [
            ".geetest_panel", ".geetest_holder", ".geetest_box",
            ".verify-wrap", ".verify-box", ".captcha-box",
            ".dialog-login.show", ".login-required-modal",
        ],
        "login_selectors": [
            ".login-tips", ".login-register", ".dialog-login",
            ".sign-form-container",
        ],
        "auth_cookie": "__zp_stoken__",
    },
    "zhilian": {
        "url_keys": ("captcha", "verify", "secure"),
        "verify_selectors": [
            ".captcha-mask", ".verify-mask", ".geetest_holder",
            ".geetest_panel", ".nc_iconfont", ".nc-container", ".captcha-box",
        ],
        "login_selectors": [
            ".login-box", ".zppp-modal__login", ".sign-dialog",
            ".zppp-modal-body", '[class*="login-dialog"]',
        ],
        "auth_cookie": None,
    },
    "job51": {
        "url_keys": ("captcha", "verify", "wapsec"),
        "verify_selectors": [
            ".swiper-wrapper.captcha", ".verify-slider", ".yidun_modal",
            ".yidun-popup", ".verify-wrap", ".captcha-box",
        ],
        "login_selectors": [
            ".login-dialog", ".login-box", ".qs-dialog-login",
            '[class*="login-modal"]', '[class*="login-popup"]',
        ],
        "auth_cookie": None,
    },
    "liepin": {
        "url_keys": ("captcha", "verify", "safety", "safe."),
        "verify_selectors": [
            ".geetest_panel", ".geetest_holder", ".geetest_box",
            ".verify-modal", ".captcha-popup", ".captcha-box",
        ],
        "login_selectors": [
            ".login-modal", ".login-pop", ".com-login",
        ],
        "auth_cookie": None,
    },
}

VERIFY_WORDS = (
    "验证码", "安全验证", "请完成验证", "滑动验证", "人机验证",
    "系统检测到", "完成验证", "拖动滑块", "为了验证您的安全性",
    "请勾选", "验证完成后", "自动跳转回", "edgeone",
    "security verification", "security check", "just a moment",
    "checking your browser", "verify you are human",
)
LOGIN_WORDS = (
    "登录失效", "请重新登录", "登录已过期", "未登录", "登录后查看",
    "登录查看完整内容",
)
RATE_WORDS = (
    "操作频繁", "访问频繁", "请求过于频繁", "访问太频繁", "稍后再试",
)
ACCOUNT_WORDS = (
    "账号异常", "账号存在异常", "账号被限制", "账号受限", "封禁",
)


DOM_PROBE_JS = """
(payload) => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return el.offsetParent !== null && r.width > 30 && r.height > 30;
    };
    for (const s of payload.verifySelectors) {
        const el = document.querySelector(s);
        if (visible(el)) return {kind: 'verify', evidence: s};
    }
    for (const s of payload.loginSelectors) {
        const el = document.querySelector(s);
        if (visible(el)) return {kind: 'login', evidence: s};
    }
    const text = (document.body ? document.body.innerText : '') || '';
    const lower = text.toLowerCase();
    const short = text.length < 900;
    const hasVerifyEl = payload.verifySelectors.some(s => !!document.querySelector(s));
    const hasLoginEl = payload.loginSelectors.some(s => !!document.querySelector(s));
    for (const w of payload.accountWords) {
        if (lower.includes(w.toLowerCase())) return {kind: 'account', evidence: w};
    }
    for (const w of payload.rateWords) {
        if (lower.includes(w.toLowerCase())) return {kind: 'rate', evidence: w};
    }
    for (const w of payload.loginWords) {
        if (lower.includes(w.toLowerCase()) && (short || hasLoginEl)) {
            return {kind: 'login', evidence: w};
        }
    }
    for (const w of payload.verifyWords) {
        if (lower.includes(w.toLowerCase()) && (short || hasVerifyEl)) {
            return {kind: 'verify', evidence: w};
        }
    }
    return {kind: '', evidence: ''};
}
"""


class RiskGuard:
    _instance: Optional["RiskGuard"] = None

    @classmethod
    def get_instance(cls) -> "RiskGuard":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # 平台连续投递失败计数（任务级，reset_task_state 清零）
        self._apply_failures: dict[str, int] = {}

    def reset_task_state(self):
        self._apply_failures.clear()

    # ───────── 冷却查询/写入 ─────────

    async def is_blocked(self, platform: str) -> Optional[dict]:
        """返回未到期的冷却记录；无冷却返回 None。"""
        try:
            return await db.get_platform_risk(platform)
        except Exception as e:
            log.error("risk lookup failed", platform=platform,
                      error=type(e).__name__, detail=str(e))
            return None

    async def trip(self, platform: str, signal: RiskSignal,
                   reason: str) -> Optional[str]:
        """记录一次风控并返回 blocked_until（ISO 字符串）。"""
        if signal == RiskSignal.OK:
            return None
        minutes = {
            RiskSignal.VERIFY: random.randint(60, 120),
            RiskSignal.LOGIN_REQUIRED: random.randint(30, 60),
            RiskSignal.RATE_LIMITED: random.randint(15, 60),
            RiskSignal.ACCOUNT_RESTRICTED: 360,
        }.get(signal, 30)
        until = (datetime.now() + timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        await db.set_platform_risk(platform, signal.value, until,
                                   (reason or "")[:200])
        log.warn("platform risk tripped", platform=platform,
                 signal=signal.value, minutes=minutes, reason=(reason or "")[:120])
        return until

    # ───────── 投递失败计数 ─────────

    def note_apply_failure(self, platform: str) -> int:
        n = self._apply_failures.get(platform, 0) + 1
        self._apply_failures[platform] = n
        return n

    def note_apply_success(self, platform: str):
        self._apply_failures[platform] = 0

    # ───────── 页面检查 ─────────

    async def inspect(self, page, platform: str):
        """返回 (RiskSignal, evidence)。"""
        cfg = PLATFORM_PATTERNS.get(platform, {})

        # 1) URL
        try:
            url = page.url or ""
        except Exception:
            url = ""
        low = url.lower()
        if not low or low.startswith("about:"):
            return RiskSignal.LOGIN_REQUIRED, "about:blank"
        for key in cfg.get("url_keys", ()):
            if key in low:
                if "login" in low:
                    return RiskSignal.LOGIN_REQUIRED, url[:120]
                return RiskSignal.VERIFY, url[:120]

        # 1b) 标题（EdgeOne / Cloudflare 等挑战页常直接写 Security Verification）
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""
        tl = title.lower()
        for kw in ("security verification", "security check", "just a moment",
                   "checking your browser", "verify you are human",
                   "安全验证", "人机验证"):
            if kw in tl:
                return RiskSignal.VERIFY, f"title:{title[:60]}"

        # 2) 网络响应
        try:
            from core.browser_engine import BrowserEngine
            be = BrowserEngine._instance
            signals = be.pop_http_signals(page) if be else []
        except Exception:
            signals = []
        for status, u, _rtype in signals:
            host = (urlparse(u).hostname or "").lower()
            hosts = PLATFORM_HOSTS.get(platform, ())
            if hosts and not any(host.endswith(h) for h in hosts):
                continue  # 忽略第三方 CDN/广告的 4xx
            if status == 401:
                return RiskSignal.LOGIN_REQUIRED, f"HTTP 401 {u[:100]}"
            if status in (403, 429, 503):
                return RiskSignal.RATE_LIMITED, f"HTTP {status} {u[:100]}"
            # 5xx 视为瞬态，交给重试，不熔断

        # 3) DOM
        try:
            hit = await page.evaluate(DOM_PROBE_JS, {
                "verifySelectors": cfg.get("verify_selectors", []),
                "loginSelectors": cfg.get("login_selectors", []),
                "verifyWords": list(VERIFY_WORDS),
                "loginWords": list(LOGIN_WORDS),
                "rateWords": list(RATE_WORDS),
                "accountWords": list(ACCOUNT_WORDS),
            })
        except Exception:
            hit = None
        if isinstance(hit, dict):
            kind = hit.get("kind") or ""
            evidence = hit.get("evidence") or ""
            if kind == "verify":
                return RiskSignal.VERIFY, evidence
            if kind == "login":
                return RiskSignal.LOGIN_REQUIRED, evidence
            if kind == "rate":
                return RiskSignal.RATE_LIMITED, evidence
            if kind == "account":
                return RiskSignal.ACCOUNT_RESTRICTED, evidence

        # 4) 认证 cookie（辅助信号：只在页面已经像登录墙时不单独触发）
        cookie_name = cfg.get("auth_cookie")
        if cookie_name:
            try:
                cookies = await page.context.cookies()
                if not any(c.get("name") == cookie_name for c in cookies):
                    # 不直接熔断；交给 DOM/响应层判断，避免 cookie 改名误锁
                    log.info("auth cookie missing", platform=platform,
                             cookie=cookie_name)
            except Exception:
                pass

        return RiskSignal.OK, ""
