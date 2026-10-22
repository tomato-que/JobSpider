"""Playwright 引擎兼容层。

默认使用 rebrowser-playwright（补掉 CDP `Runtime.enable` 的自动化泄露），
导入失败或启动失败时回退到官方 playwright。两种包的 async_api 完全同构，
业务代码只从这里拿 `async_playwright`。

可用环境变量：
  JOBSPIDER_ENGINE=rebrowser|playwright   （默认 rebrowser）
"""
from __future__ import annotations

import os

_REQUESTED = os.getenv("JOBSPIDER_ENGINE", "rebrowser").strip().lower()
_ACTIVE = "playwright"

async_playwright = None  # type: ignore[assignment]

if _REQUESTED in ("", "rebrowser", "rebrowser-playwright"):
    try:
        from rebrowser_playwright.async_api import (  # type: ignore
            async_playwright as _async_playwright,
        )
        async_playwright = _async_playwright
        _ACTIVE = "rebrowser"
    except Exception as e:  # pragma: no cover - depends on local install
        print(
            f"  [pw_compat] rebrowser 不可用({type(e).__name__}: {e})，"
            f"回退 playwright",
            flush=True,
        )

if async_playwright is None:
    from playwright.async_api import (  # type: ignore
        async_playwright as _async_playwright,
    )
    async_playwright = _async_playwright
    _ACTIVE = "playwright"


def engine_name() -> str:
    """当前实际使用的引擎名（用于日志/诊断）。"""
    return _ACTIVE


def stock_async_playwright():
    """返回官方 playwright 的 async_playwright，供启动失败时回退。"""
    from playwright.async_api import async_playwright as _ap  # type: ignore

    return _ap
