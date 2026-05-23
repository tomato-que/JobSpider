"""抓取引擎 — 基于 Playwright 持久化浏览器

核心理念：像真人一样操作浏览器
- click / fill / type_text → 模拟真实用户交互
- 使用 Chrome（headless=False）→ 浏览器窗口可见，便于调试
- 页面生命周期：fetch_page 返回后页面保持存活，直到显式关闭
"""
import asyncio
import random
from typing import Optional

from core.browser_engine import BrowserEngine
from core.log import get_logger
from config import (
    PAGE_LOAD_TIMEOUT, MAX_RETRIES, RETRY_DELAY, gaussian_delay,
)

log = get_logger("scrapling")


class ScraplingEngine:
    """浏览器引擎（Playwright 持久化版本）

    页面在 fetch_page 后保持存活，支持后续交互操作。
    """
    _instance: Optional["ScraplingEngine"] = None

    @classmethod
    def get_instance(cls) -> "ScraplingEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ───────── 页面获取 ─────────

    async def fetch_page(
        self, url: str, network_idle: bool = True,
        wait_selector: Optional[str] = None, **_kwargs,
    ):
        """打开新标签页并导航到 URL，返回 Playwright Page（保持存活）

        重试时关闭上一轮失败的 page，防止 Chrome 标签页泄漏。
        成功返回的 page 由调用方持有/关闭。
        """
        last_err = None
        page = None
        for attempt in range(MAX_RETRIES):
            # 上一轮失败遗留的 page 先关掉，避免标签页堆积
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
                page = None
            try:
                engine = await BrowserEngine.get_instance()
                page = await engine.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state(
                        "networkidle" if network_idle else "load",
                        timeout=PAGE_LOAD_TIMEOUT,
                    )
                except Exception:
                    pass
                if wait_selector:
                    try:
                        await page.wait_for_selector(
                            wait_selector, timeout=15000, state="attached"
                        )
                    except Exception as e:
                        log.error("wait_selector miss",
                                  url=url, selector=wait_selector,
                                  error=type(e).__name__, detail=str(e))
                await asyncio.sleep(gaussian_delay(0.5, 1.5))
                return page  # 成功 — 调用方持有

            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES - 1:
                    log.error("fetch retry",
                              url=url, attempt=attempt + 1,
                              error=type(e).__name__, detail=str(e))
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                else:
                    # 最后一次失败 — 关掉 page 后再抛
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = None
                    log.error("fetch failed (final)",
                              url=url, attempts=MAX_RETRIES,
                              error=type(e).__name__, detail=str(e))

        raise RuntimeError(
            f"Engine fetch failed after {MAX_RETRIES} retries: {last_err}"
        )

    # ───────── 浏览器交互 ─────────

    async def click(self, page, selector: str, timeout: int = 10000):
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        await asyncio.sleep(gaussian_delay(0.15, 0.4))
        await page.click(selector)

    async def fill(self, page, selector: str, text: str, timeout: int = 10000):
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        await page.click(selector)
        await asyncio.sleep(gaussian_delay(0.1, 0.2))
        await page.fill(selector, text)

    async def type_text(self, page, selector: str, text: str, timeout: int = 10000):
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        await page.click(selector)
        await asyncio.sleep(gaussian_delay(0.1, 0.3))
        await page.press(selector, "Control+a")
        await asyncio.sleep(0.05)
        await page.press(selector, "Backspace")
        await asyncio.sleep(gaussian_delay(0.1, 0.2))
        for ch in text:
            await page.type(selector, ch, delay=random.randint(60, 180))
        await asyncio.sleep(gaussian_delay(0.2, 0.5))

    async def press_key(self, page, key: str, selector: str = None):
        await page.press(selector or "body", key)

    async def wait_for(self, page, selector: str,
                       timeout: int = 15000, state: str = "visible"):
        return await page.wait_for_selector(
            selector, timeout=timeout, state=state
        )

    async def wait_load(self, page, timeout: int = 30000):
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)

    async def try_click(self, page, selectors: list, timeout: int = 4000) -> bool:
        for sel in selectors:
            try:
                el = await page.wait_for_selector(
                    sel, timeout=timeout, state="visible"
                )
                if el:
                    await asyncio.sleep(gaussian_delay(0.15, 0.4))
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    async def try_fill(self, page, selectors: list, text: str,
                       timeout: int = 4000) -> bool:
        for sel in selectors:
            try:
                el = await page.wait_for_selector(
                    sel, timeout=timeout, state="visible"
                )
                if el:
                    await page.click(sel)
                    await asyncio.sleep(0.1)
                    await page.press(sel, "Control+a")
                    await asyncio.sleep(0.05)
                    await page.press(sel, "Backspace")
                    await asyncio.sleep(0.1)
                    for ch in text:
                        await page.type(sel, ch, delay=random.randint(60, 150))
                    return True
            except Exception:
                continue
        return False

    # ───────── JavaScript 执行 ─────────

    async def eval_js(self, page, script: str):
        try:
            return await page.evaluate(script)
        except Exception as e:
            # JS 执行异常通常意味着上下文（页面）失效或选择器问题；
            # 出现频率高，所以默认静默，但保留一行结构化日志便于排查。
            log.error("eval_js failed",
                      error=type(e).__name__, detail=str(e))
            return None

    # ───────── 滚动 ─────────

    async def scroll_page(self, page, times: int = 3):
        for _ in range(times):
            await self.eval_js(page, "window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(gaussian_delay(0.3, 0.8))
        await self.eval_js(page, "window.scrollTo(0, 0)")

    # ───────── 调试工具 ─────────

    async def screenshot(self, page, path: str = "debug.png"):
        try:
            await page.screenshot(path=path, full_page=False)
            print(f"  [DEBUG] 截图: {path}")
        except Exception as e:
            print(f"  [DEBUG] 截图失败: {e}")

    async def current_url(self, page) -> str:
        try:
            return page.url
        except Exception:
            return ""
