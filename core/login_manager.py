"""平台登录窗口与登录态检测。

JobSpider 固定使用项目内的 chrome_profile 作为持久化配置目录；
用户在同一个 Chrome 窗口里登录后，后续任务会直接复用这份 cookie。

登录态判断只依据 cookie 是否存在，不读取、不上报任何 cookie 值：
  logged_in  命中已知的登录 cookie
  unknown    域名下有 cookie，但没有命中已知的登录 cookie
  logged_out 该域名下没有 cookie
"""
from __future__ import annotations

import asyncio
from typing import Optional

from core.log import get_logger

log = get_logger("login_manager")


PLATFORM_LOGIN = {
    "boss": {
        "name": "Boss直聘",
        "login_url": "https://www.zhipin.com/web/user/?ka=header-login",
        "home_url": "https://www.zhipin.com/web/geek/job",
        "domain": "zhipin.com",
        "auth_cookies": ("__zp_stoken__", "wt2", "bst", "geek_zp_token"),
        "logged_in_js": (
            "!!document.querySelector('.user-nav, .nav-figure, [ka=\"header-logout\"],"
            " .figure, .user-info')"
        ),
        "logged_out_js": (
            "!!document.querySelector('.login-btn, [ka=\"header-login\"],"
            " .nav-login, .header-login')"
        ),
    },
    "zhilian": {
        "name": "智联招聘",
        "login_url": "https://passport.zhaopin.com/login",
        "home_url": "https://www.zhaopin.com/sou",
        "domain": "zhaopin.com",
        "auth_cookies": ("at", "rt", "zhaopin_session", "__c_uid"),
        "logged_in_js": (
            "!!document.querySelector('.zp-user, .user-name, [class*=\"user-info\"],"
            " [class*=\"avatar\"]')"
        ),
        "logged_out_js": (
            "!!document.querySelector('.login-btn, [class*=\"login-btn\"],"
            " [class*=\"login-register\"]')"
        ),
    },
    "job51": {
        "name": "前程无忧",
        "login_url": "https://login.51job.com/login.php",
        "home_url": "https://we.51job.com/pc/search",
        "domain": "51job.com",
        "auth_cookies": ("guid", "job_login_token", "51job_session"),
        "logged_in_js": (
            "!!document.querySelector('.user-name, .header-user, [class*=\"user-info\"],"
            " [class*=\"avatar\"]')"
        ),
        "logged_out_js": (
            "!!document.querySelector('.login-btn, [class*=\"login\"],"
            " .to-login')"
        ),
    },
    "liepin": {
        "name": "猎聘",
        "login_url": "https://www.liepin.com/user/login/",
        "home_url": "https://www.liepin.com/zhaopin/",
        "domain": "liepin.com",
        "auth_cookies": ("lt_auth", "access_token", "liepin_session"),
        "logged_in_js": (
            "!!document.querySelector('.user-info, .user-name, [class*=\"user-info\"],"
            " [class*=\"avatar\"], .header-user')"
        ),
        "logged_out_js": (
            "!!document.querySelector('.login-btn, [class*=\"login-btn\"],"
            " .header-login')"
        ),
    },
}

# 登录后留在这个窗口里，用户可自行切换其它平台
_LOGIN_PAGES: dict[str, object] = {}


async def _engine():
    from core.browser_engine import BrowserEngine
    return await BrowserEngine.get_instance()


async def open_login(platforms: list[str], timeout_s: float = 40.0) -> dict:
    """在持久化 Chrome 里打开各平台登录页，保持窗口不关闭。

    返回 {ok, opened: [...], skipped: [...], error?}
    """
    wanted = [p for p in (platforms or []) if p in PLATFORM_LOGIN]
    if not wanted:
        return {"ok": False, "error": "没有可登录的平台", "opened": []}

    try:
        engine = await _engine()
    except Exception as e:
        return {"ok": False, "error": _launch_error_text(e), "opened": []}

    opened: list[str] = []
    for key in wanted:
        cfg = PLATFORM_LOGIN[key]
        page = _LOGIN_PAGES.get(key)
        try:
            if page is None or page.is_closed():
                page = await engine.new_page()
                _LOGIN_PAGES[key] = page
            await engine.goto_current(page, cfg["login_url"],
                                      timeout=timeout_s * 1000)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            opened.append(key)
        except Exception as e:
            log.error("open login failed", platform=key,
                      error=type(e).__name__, detail=str(e))
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
                _LOGIN_PAGES.pop(key, None)

    if not opened:
        return {"ok": False, "error": "登录页打开失败，请查看实时日志", "opened": []}
    return {"ok": True, "opened": opened}


async def check_status(probe_dom: bool = False) -> dict:
    """返回各平台登录态。

    默认只依据 cookie 判断，不打开任何页面。
    probe_dom=True 时会在已登录的浏览器里依次打开平台首页做 DOM 探测
    （较慢，仅用于 cookie 判断为 unknown 时手动确认）。
    """
    from core.browser_engine import BrowserEngine

    engine = BrowserEngine._instance
    out = {k: {"name": v["name"], "state": "unknown", "detail": ""}
           for k, v in PLATFORM_LOGIN.items()}
    if engine is None or not engine._alive or engine._context is None:
        for v in out.values():
            v["detail"] = "浏览器未启动，无法检测"
        return {"running": False, "platforms": out}

    try:
        cookies = await engine._context.cookies()
    except Exception as e:
        for v in out.values():
            v["detail"] = f"读取失败: {type(e).__name__}"
        return {"running": False, "platforms": out}

    unknown: list[str] = []
    for key, cfg in PLATFORM_LOGIN.items():
        domain = cfg["domain"]
        owned = [c for c in cookies
                 if domain in (c.get("domain") or "")]
        names = {c.get("name") for c in owned}
        if any(n in names for n in cfg["auth_cookies"]):
            state, detail = "logged_in", f"{len(owned)} 个 cookie"
        elif owned:
            state, detail = "unknown", f"{len(owned)} 个 cookie"
            unknown.append(key)
        else:
            state, detail = "logged_out", "无 cookie"
        out[key] = {"name": cfg["name"], "state": state, "detail": detail}

    if probe_dom and unknown:
        for key in unknown:
            cfg = PLATFORM_LOGIN[key]
            state = await _probe_login_page(engine, key, cfg)
            if state:
                out[key]["state"] = state
                out[key]["detail"] = "页面探测"

    return {"running": True, "platforms": out}


async def _probe_login_page(engine, key: str, cfg: dict) -> str:
    """打开平台首页，用 DOM 判断是否已登录；失败返回空串。"""
    page = None
    try:
        page = await engine.new_page()
        await engine.goto_current(page, cfg["home_url"], timeout=15000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        logged_in = await page.evaluate(f"() => {cfg['logged_in_js']}")
        logged_out = await page.evaluate(f"() => {cfg['logged_out_js']}")
        if logged_in and not logged_out:
            return "logged_in"
        if logged_out and not logged_in:
            return "logged_out"
        if logged_in and logged_out:
            return "logged_in"
        return ""
    except Exception as e:
        log.info("login probe failed", platform=key,
                 error=type(e).__name__)
        return ""
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def close_login_pages() -> None:
    """关闭由登录窗口创建的页面（任务结束/服务关闭时调用）。"""
    for key, page in list(_LOGIN_PAGES.items()):
        _LOGIN_PAGES.pop(key, None)
        try:
            if page is not None and not page.is_closed():
                await page.close()
        except Exception:
            pass


def _launch_error_text(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    if ("processsingleton" in low or "profile" in low
            or "user data directory is already in use" in low
            or "in use" in low):
        return (
            "chrome_profile 正被另一个 Chrome 占用。请关闭用该项目配置目录"
            "打开的 Chrome 窗口后重试（注意不要与日常 Chrome 共用同一目录）。"
        )
    return f"浏览器启动失败: {type(e).__name__}: {text[:200]}"
