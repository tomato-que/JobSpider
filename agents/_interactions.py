"""浏览器交互混入 — 鼠标轨迹、城市选择、关键词输入、搜索提交、翻页

从 base.py 拆出。保持方法签名、签名顺序、函数体与原实现一致。
"""
import asyncio
import json

from config import gaussian_delay


class BrowserInteractionsMixin:
    """通用浏览器交互方法集合 (供 BaseAgent 继承)。"""

    # ──────── 反检测：真实鼠标点击 ────────

    async def human_click(self, page, find_js: str,
                          before_click_pause: tuple = (0.2, 0.6)) -> dict:
        """以"真人鼠标轨迹"的方式点击元素，规避 JS .click() 检测

        find_js: 一段 JS 代码，返回 {found, x, y, w, h, text, ...} 或 {found:false, reason}
                 该 JS 应负责：找到目标元素、scrollIntoView、移除 target=_blank、
                 返回元素的视口坐标
        返回: JS 的返回值 + clicked: bool
        """
        import random as _random
        info = await self.eval_js(page, find_js)
        if not info or not info.get("found"):
            return {"clicked": False, **(info or {})}

        x = info.get("x", 0)
        y = info.get("y", 0)
        w = info.get("w", 0)
        h = info.get("h", 0)
        if w <= 0 or h <= 0:
            return {"clicked": False, "reason": "zero_size", **info}

        # 点击点：在元素中心附近加少量抖动
        cx = x + w / 2 + _random.uniform(-w * 0.15, w * 0.15)
        cy = y + h / 2 + _random.uniform(-h * 0.15, h * 0.15)

        # 起点：从屏幕某个随机位置开始（避免每次都从 0,0）
        try:
            sx = _random.uniform(100, 800)
            sy = _random.uniform(100, 600)
            # 沿贝塞尔轨迹移动（用直线段近似），steps 3-6
            steps = _random.randint(3, 6)
            for i in range(1, steps + 1):
                t = i / steps
                # 二次贝塞尔：起点 sx,sy → 控制点（带随机偏移）→ 终点 cx,cy
                bx = (1 - t) ** 2 * sx + 2 * (1 - t) * t * (
                    (sx + cx) / 2 + _random.uniform(-50, 50)
                ) + t ** 2 * cx
                by = (1 - t) ** 2 * sy + 2 * (1 - t) * t * (
                    (sy + cy) / 2 + _random.uniform(-30, 30)
                ) + t ** 2 * cy
                await page.mouse.move(bx, by, steps=_random.randint(2, 5))
                await asyncio.sleep(_random.uniform(0.02, 0.08))
        except Exception:
            pass

        # 点击前微停顿（人会犹豫一下）
        await asyncio.sleep(gaussian_delay(*before_click_pause))

        try:
            await page.mouse.click(cx, cy, delay=_random.randint(50, 150))
            return {"clicked": True, **info}
        except Exception as e:
            return {"clicked": False, "reason": str(e), **info}

    # ──────── 通用交互：城市选择 ────────

    async def select_city_by_text(self, page, city: str,
                                  trigger_selectors: list,
                                  container_selector: str = None) -> bool:
        """通用城市选择：点击触发器 → 在面板中找到城市文本 → 点击"""
        name = self.platform_cfg["name"]

        # 1. 点击打开城市面板
        opened = await self.engine.try_click(page, trigger_selectors)
        if not opened:
            opened = await self.eval_js(page, """() => {
                const els = document.querySelectorAll(
                    '[class*="city"], [class*="City"], [class*="location"]'
                );
                for (const el of els) {
                    if (el.offsetParent !== null
                        && (el.tagName === 'A' || el.tagName === 'SPAN'
                            || el.tagName === 'DIV' || el.tagName === 'BUTTON')
                        && el.offsetHeight < 80) {
                        el.click(); return true;
                    }
                }
                return false;
            }""")
        if not opened:
            print(f"  [{name}] 未找到城市选择器，使用默认城市")
            return False

        await asyncio.sleep(gaussian_delay(0.5, 1.0))

        # 2. 在面板中找到目标城市并点击
        safe_city = json.dumps(city)[1:-1]  # strip outer quotes
        container_js = (
            f"document.querySelector('{container_selector}')"
            if container_selector else "document"
        )
        selected = await self.eval_js(page, f"""() => {{
            const container = {container_js} || document;
            const candidates = container.querySelectorAll('a, li, span, div, label, dd');
            // 优先精确匹配叶子节点
            for (const el of candidates) {{
                if (el.childElementCount === 0
                    && el.textContent?.trim() === '{safe_city}'
                    && el.offsetParent !== null) {{
                    el.click(); return true;
                }}
            }}
            // 再匹配含城市名的短元素
            for (const el of candidates) {{
                const t = el.textContent?.trim();
                if (t && (t === '{safe_city}' || (t.startsWith('{safe_city}') && t.length < {len(city) + 8}))
                    && el.offsetParent !== null
                    && el.offsetHeight < 60 && el.offsetWidth > 10) {{
                    el.click(); return true;
                }}
            }}
            return false;
        }}""")

        if selected:
            print(f"  [{name}] 城市: {city}")
            await asyncio.sleep(gaussian_delay(0.8, 1.5))
            return True
        else:
            print(f"  [{name}] 未找到城市 '{city}'，使用默认城市")
            return False

    # ──────── 通用交互：关键词输入 ────────

    async def input_keyword(self, page, keyword: str,
                            input_selectors: list) -> bool:
        """在搜索框中输入关键词（Playwright 原生键盘事件）"""
        filled = await self.engine.try_fill(page, input_selectors, keyword)
        if filled:
            return True

        # JS fallback: nativeInputValueSetter 兼容 React/Vue
        safe_kw = keyword.replace("\\", "\\\\").replace("'", "\\'")
        filled = await self.eval_js(page, f"""() => {{
            const inputs = document.querySelectorAll(
                'input[type="text"], input[type="search"], input:not([type])'
            );
            for (const el of inputs) {{
                const ph = (el.placeholder || '').toLowerCase();
                const nm = (el.name || '').toLowerCase();
                if ((ph.includes('搜索') || ph.includes('职位') || ph.includes('关键')
                     || ph.includes('search') || ph.includes('job')
                     || nm === 'query' || nm === 'keyword' || nm === 'kw' || nm === 'key')
                    && el.offsetParent !== null) {{
                    el.focus();
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, '{safe_kw}');
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
            }}
            return false;
        }}""")
        return bool(filled)

    # ──────── 通用交互：搜索提交 ────────

    async def click_search_button(self, page, btn_selectors: list) -> bool:
        """点击搜索按钮，失败则按 Enter"""
        clicked = await self.engine.try_click(page, btn_selectors)
        if clicked:
            return True

        clicked = await self.eval_js(page, """() => {
            const btns = document.querySelectorAll('button, a, div, span, i');
            for (const btn of btns) {
                const text = btn.textContent?.trim();
                const cls = (btn.className || '').toLowerCase();
                if (btn.offsetParent !== null && btn.offsetHeight > 0 && (
                    text === '搜索' || text === '查询' || text === 'Search'
                    || cls.includes('search-btn') || cls.includes('btn-search')
                    || cls.includes('search-icon') || cls.includes('icon-search')
                )) {
                    btn.click(); return true;
                }
            }
            return false;
        }""")
        if clicked:
            return True

        try:
            await self.engine.press_key(page, "Enter")
            return True
        except Exception:
            return False

    # ──────── 通用交互：翻页 ────────

    async def click_next_page(self, page) -> bool:
        """点击下一页按钮"""
        result = await self.eval_js(page, """() => {
            // 策略1: 常见下一页按钮 class
            const nexts = document.querySelectorAll(
                '.next:not(.disabled), .page-next:not(.disabled), '
                + '[class*="next"]:not(.disabled), .btn-next:not(.disabled), '
                + '.ant-pagination-next:not(.ant-pagination-disabled)'
            );
            for (const btn of nexts) {
                if (btn.offsetParent !== null
                    && !btn.classList.contains('disabled')
                    && !btn.getAttribute('aria-disabled')) {
                    btn.click(); return true;
                }
            }
            // 策略2: 文本匹配
            const links = document.querySelectorAll('a, button, li, span');
            for (const el of links) {
                const t = el.textContent?.trim();
                if ((t === '下一页' || t === '>' || t === '›' || t === '»')
                    && el.offsetParent !== null
                    && !el.classList.contains('disabled')) {
                    el.click(); return true;
                }
            }
            return false;
        }""")
        return bool(result)

    # ──────── 诊断：扫描卡片按钮 ────────

    async def diagnose_buttons(self, page, card_selector: str) -> None:
        """诊断：扫描每张卡片上有哪些按钮，便于排查"""
        info = await self.eval_js(page, f"""() => {{
            const cards = document.querySelectorAll('{card_selector}');
            if (!cards.length) return {{ cards: 0, samples: [] }};
            const samples = [];
            // 只取前 2 张卡片做诊断
            for (let i = 0; i < Math.min(2, cards.length); i++) {{
                const card = cards[i];
                const btns = Array.from(card.querySelectorAll('a, button, span, div'))
                    .filter(el => el.offsetParent !== null
                        && el.offsetHeight > 0 && el.offsetHeight < 80
                        && (el.textContent || '').trim().length > 0
                        && (el.textContent || '').trim().length < 20)
                    .map(el => ({{
                        tag: el.tagName,
                        text: (el.textContent || '').trim().slice(0, 20),
                        cls: (el.className || '').toString().slice(0, 80),
                    }}))
                    .slice(0, 12);
                samples.push({{ idx: i, btns: btns }});
            }}
            return {{ cards: cards.length, samples: samples }};
        }}""")
        name = self.platform_cfg["name"]
        if not info:
            print(f"  [{name}] 诊断: eval_js 失败", flush=True)
            return
        print(f"  [{name}] 诊断: 找到 {info.get('cards', 0)} 张卡片", flush=True)
        for s in info.get("samples", []):
            print(f"  [{name}] 卡片#{s['idx']} 的可点击元素:", flush=True)
            for b in s.get("btns", []):
                print(f"    - <{b['tag']}> '{b['text']}' class='{b['cls']}'",
                      flush=True)
