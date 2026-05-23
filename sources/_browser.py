"""共享浏览器实例 — 给反爬强的源用 Playwright 兜底

仅在 _http.py 失败/检测到挑战页时调用，不参与正常 HTTP 抓取流程。
单例 + 懒启动：第一次用到才拉起 Chromium，之后复用，进程退出时关闭。

stealth 程度：基础 — 改 navigator.webdriver / chrome.runtime / 语言 / UA。
对付 EO_Bot / 瑞数等"基础 JS 挑战"够用；不适合应付 Boss 这种重武器。
"""
from __future__ import annotations

import asyncio
import atexit
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# 在 Windows + Python 3.9 上 asyncio 默认 ProactorEventLoop 跟 playwright 偶发冲突
# 这里不强制切，让 server.py 启动逻辑决定。

_pw = None
_browser: Optional[Browser] = None
_lock = asyncio.Lock()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# 注入到每个新 context 的脚本：抹平 navigator.webdriver 等
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', length: 1},
        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', length: 1},
        {name: 'Native Client', filename: 'internal-nacl-plugin', length: 1},
    ]
});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) =>
    p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : origQuery(p);
}
"""


async def _ensure_browser():
    global _pw, _browser
    if _browser and _browser.is_connected():
        return _browser
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        if _pw is None:
            _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        return _browser


async def get_page_html(
    url: str,
    *,
    wait_selector: Optional[str] = None,
    timeout: float = 20.0,
    extra_wait_ms: int = 0,
) -> Optional[str]:
    """打开 URL → 等渲染 → 返回 HTML。失败返回 None。

    - wait_selector：等到该选择器出现再读 HTML（多用于挑战页跳转后的真实容器）
    - extra_wait_ms：额外硬等（让 EO_Bot 跑完 cookie 注入和重定向）
    """
    try:
        browser = await _ensure_browser()
        ctx: BrowserContext = await browser.new_context(
            user_agent=_UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 800},
        )
        await ctx.add_init_script(_STEALTH_JS)
        page: Page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=int(timeout * 1000))
            if extra_wait_ms:
                await page.wait_for_timeout(extra_wait_ms)
            if wait_selector:
                try:
                    await page.wait_for_selector(
                        wait_selector, timeout=int(timeout * 1000),
                    )
                except Exception:
                    pass  # 选择器没出现也读一次内容，让上层自己判断
            html = await page.content()
            return html
        finally:
            await ctx.close()
    except Exception as e:
        print(f"  [browser] {url[:60]}… {type(e).__name__}: {e}", flush=True)
        return None


async def close_browser():
    """进程退出时调用。"""
    global _browser, _pw
    try:
        if _browser:
            await _browser.close()
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    finally:
        _browser = None
        _pw = None


def _sync_close():
    """atexit 兜底（理论上 server.py 的 shutdown 钩子先到）。"""
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_running():
            return
        loop.run_until_complete(close_browser())
    except Exception:
        pass


atexit.register(_sync_close)
