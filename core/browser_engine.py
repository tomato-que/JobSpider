"""Playwright 持久化浏览器引擎 — 直接控制真实 Chrome

使用 launch_persistent_context 保持登录状态和 cookie 持久化。
所有页面共享同一个浏览器上下文（同一份 cookie/localStorage）。
"""
import asyncio
import os
from typing import Optional

from config import HEADLESS, PAGE_LOAD_TIMEOUT, ROOT_DIR, gaussian_delay
from core.log import get_logger
from core.pw_compat import (
    async_playwright, engine_name, stock_async_playwright,
)

log = get_logger("browser_engine")


# 只保留最小、且不会与真实 Chrome 指纹冲突的修补：
# - navigator.webdriver（AutomationControlled 之外的双保险）
# - notifications 权限查询（老牌检测项）
# plugins / languages / UA 不再伪造：真实 Chrome 的值本身就是一致的，
# 伪造成不完整列表反而会与 mimeTypes、Accept-Language 对不上。
STEALTH_JS = """
(() => {
    try {
        if (navigator.webdriver) {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        }
    } catch (e) {}
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
    } catch (e) {}
    try {
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : origQuery(params);
    } catch (e) {}
})();
"""

# S16: 常见第三方跟踪域名 — 在页面级拦截，防止跨平台指纹关联
_TRACKER_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "hotjar.com", "clarity.ms", "cnzz.com", "umeng.com",
    "baidu.com/hm.js", "51.la", "growingio.com", "sensors.data",
    "tongji.baidu.com", "hm.baidu.com",
}


class BrowserEngine:
    """持久化浏览器引擎 — 单例模式，一个 Chrome 实例服务所有平台

    使用 launch_persistent_context：
    - 所有标签页共享 cookie/登录状态
    - chrome_profile 目录持久化，下次启动自动恢复登录
    """

    _instance: Optional["BrowserEngine"] = None
    _lock: Optional[asyncio.Lock] = None

    def __init__(self):
        self._pw = None
        self._context = None
        self._alive = False
        self._owned_pages = set()  # 主操作页集合，不在此集合的新弹出页会自动关闭
        self._popup_tasks: set[asyncio.Task] = set()  # 跟踪 _maybe_close 任务，防止 fire-and-forget 警告
        # page -> [(status, url, resource_type)]，风险守卫读取
        self._http_signals: dict = {}
        self._engine = engine_name()

    @classmethod
    async def get_instance(cls) -> "BrowserEngine":
        if cls._lock is None:
            cls._lock = asyncio.Lock()  # safe: first call always on single event loop
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            if not cls._instance._alive:
                await cls._instance._launch()
        return cls._instance

    async def _launch(self):
        self._pw = await async_playwright().start()
        profile_dir = str(ROOT_DIR / "chrome_profile")
        os.makedirs(profile_dir, exist_ok=True)
        launch_kwargs = dict(
            user_data_dir=profile_dir,
            channel="chrome",
            headless=HEADLESS,
            args=[
                "--window-size=1440,900",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
            # 用真实窗口尺寸，不做 viewport 模拟（innerWidth 与 outerWidth 一致）
            no_viewport=True,
            locale="zh-CN",
        )
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                **launch_kwargs)
        except Exception as e:
            if self._engine != "rebrowser":
                raise
            log.warn("rebrowser launch failed, falling back to playwright",
                     error=type(e).__name__, detail=str(e))
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._engine = "playwright"
            self._pw = await stock_async_playwright()().start()
            self._context = await self._pw.chromium.launch_persistent_context(
                **launch_kwargs)
        await self._context.add_init_script(STEALTH_JS)

        # 监听新弹出页：用 opener() 区分主动创建 vs 弹出
        # - context.new_page() 创建的页面 opener=None → 保留
        # - target=_blank / window.open 触发的弹出页有 opener → 关闭
        # 注意：不能用 "是否在 _owned_pages 中" 判断，因为 page 事件触发时
        # _owned_pages.add() 还没执行（race condition），会误关自己创建的页
        def _on_page(p):
            async def _maybe_close():
                try:
                    try:
                        opener = await p.opener()
                    except Exception:
                        opener = None
                    # 主动创建的页（含初始 about:blank）不关
                    if opener is None:
                        return
                    # 已显式标记为主操作页的也不关
                    if p in self._owned_pages:
                        return
                    try:
                        await p.wait_for_load_state("domcontentloaded", timeout=3000)
                    except Exception:
                        pass
                    try:
                        url = p.url
                        print(f"  [BrowserEngine] 自动关闭弹出页: {url[:80]}",
                              flush=True)
                        await p.close()
                    except Exception as e:
                        # 已关闭页面再 close 会抛 — 静默吞掉，但留一行结构化日志便于排查
                        log.error("popup close failed",
                                  error=type(e).__name__, detail=str(e))
                except Exception as e:
                    log.error("popup handler crashed",
                              error=type(e).__name__, detail=str(e))

            try:
                loop = asyncio.get_running_loop()
                t = loop.create_task(_maybe_close())
                self._popup_tasks.add(t)
                t.add_done_callback(lambda fut: self._popup_tasks.discard(fut))
            except Exception as e:
                log.error("popup task schedule failed",
                          error=type(e).__name__, detail=str(e))

        self._context.on("page", _on_page)
        self._alive = True
        print(f"  [BrowserEngine] Chrome launched (persistent profile, "
              f"engine={self._engine})", flush=True)

    async def _close_resources(self):
        """关闭 context 与 driver，供重启/退出复用。"""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._http_signals.clear()

    async def _ensure_alive(self):
        if not self._alive or not self._context:
            await self._close_resources()
            self._alive = False
            await self._launch()

    async def new_page(self):
        if self.__class__._lock is None:
            self.__class__._lock = asyncio.Lock()
        async with self.__class__._lock:
            await self._ensure_alive()
            if len(self._owned_pages) >= 20:
                raise RuntimeError(f"已打开 {len(self._owned_pages)} 个页面，超出上限")
            try:
                page = await self._context.new_page()
            except Exception as e:
                if "closed" in str(e).lower():
                    log.error("context dead, will relaunch on next call",
                              error=type(e).__name__, detail=str(e))
                    self._alive = False
                    self._context = None
                raise
            self._owned_pages.add(page)

        page.set_default_timeout(PAGE_LOAD_TIMEOUT)
        page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)

        def _on_close():
            self._owned_pages.discard(page)
            self._http_signals.pop(page, None)
        page.once("close", _on_close)

        # 记录 4xx/5xx 网络信号，供风险守卫判定风控/登录失效
        self._http_signals[page] = []
        page.on("response", lambda resp: self._note_response(page, resp))

        # S16: 页面级拦截第三方跟踪脚本，降低跨平台指纹关联风险
        async def _block_trackers(route):
            await route.abort()
        try:
            for domain in _TRACKER_DOMAINS:
                await page.route(f"**/{domain}/**", _block_trackers)
        except Exception:
            pass

        return page

    async def goto_current(self, page, url: str, timeout: int = 40000):
        """在已存在的页面里导航（登录窗口复用同一标签，避免多开）。"""
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        try:
            await page.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass
        return page

    def _note_response(self, page, resp):
        try:
            status = int(resp.status)
            url = resp.url or ""
            req = resp.request
            rtype = req.resource_type if req else ""
        except Exception:
            return
        if status < 400 or rtype not in ("document", "xhr", "fetch"):
            return
        bucket = self._http_signals.get(page)
        if bucket is not None and len(bucket) < 20:
            bucket.append((status, url, rtype))

    def pop_http_signals(self, page):
        """取出并清空该页累计的 HTTP 异常信号。"""
        bucket = self._http_signals.get(page) or []
        self._http_signals[page] = []
        return list(bucket)


    async def close_page_clean(self, page):
        """关闭页面并清理 service worker / cache（S16 平台隔离增强）"""
        try:
            await page.evaluate("""async () => {
                try {
                    const regs = await navigator.serviceWorker?.getRegistrations?.() || [];
                    for (const r of regs) r.unregister();
                } catch {}
                try {
                    const keys = await caches?.keys?.() || [];
                    for (const k of keys) caches.delete(k);
                } catch {}
            }""")
        except Exception:
            pass
        try:
            await page.close()
        except Exception:
            pass

    async def close(self):
        await self._close_resources()
        self._alive = False
        BrowserEngine._instance = None
        print("  [BrowserEngine] Chrome closed")


# ==================== 页面交互辅助函数 ====================


async def safe_click(page, selectors: str, timeout: int = 8000) -> bool:
    """尝试点击，selectors 用逗号分隔多个备选选择器"""
    for sel in selectors.split(","):
        sel = sel.strip()
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


async def safe_fill(page, selectors: str, text: str, timeout: int = 8000) -> bool:
    """尝试填充输入框"""
    for sel in selectors.split(","):
        sel = sel.strip()
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            await loc.fill("")
            await page.keyboard.type(text, delay=50 + int(gaussian_delay(20, 80)))
            return True
        except Exception:
            continue
    return False


async def safe_type(page, text: str, delay: int = 80):
    """直接键盘输入（不定位元素）"""
    await page.keyboard.type(text, delay=delay)


async def wait_any(page, selectors: str, timeout: int = 15000) -> bool:
    """等待任意一个选择器出现"""
    for sel in selectors.split(","):
        sel = sel.strip()
        try:
            await page.wait_for_selector(sel, timeout=timeout, state="attached")
            return True
        except Exception:
            continue
    return False


async def extract_js(page, script: str, default=None):
    """安全执行 JS 并返回结果"""
    try:
        return await page.evaluate(script)
    except Exception:
        return default


async def human_delay(min_s: float = 0.5, max_s: float = 1.5):
    """模拟人类操作间隔"""
    await asyncio.sleep(gaussian_delay(min_s, max_s))
