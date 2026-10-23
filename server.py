"""招聘信息抓取系统 - Web API 服务器"""
import asyncio
import json
import os
import re
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (FastAPI, WebSocket, WebSocketDisconnect, Query,
                     HTTPException, Request, UploadFile, File)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from models.job import SearchParams
from agents.master import MasterAgent
from core.output import OUTPUT_DIR
from core import db
from core.public_jobs_service import (
    PublicJobsEnricher,
    PublicJobsScorer,
    BULLETIN_SRC_RE,
)

# 模块级单例：进程级缓存随实例存活
_enricher = PublicJobsEnricher()
_scorer = PublicJobsScorer()

# /api/config POST 鉴权 token（未设置时自动生成本机临时 token）
JOBSPIDER_TOKEN = os.getenv("JOBSPIDER_TOKEN", "")
_TOKEN_FROM_ENV = bool(JOBSPIDER_TOKEN)
if not JOBSPIDER_TOKEN:
    import secrets
    JOBSPIDER_TOKEN = secrets.token_hex(16)
    print(f"  [WARN] JOBSPIDER_TOKEN 未设置 — 已自动生成临时 token: {JOBSPIDER_TOKEN[:8]}…",
          flush=True)
    print(f"  [WARN] 如需持久化，请在 .env 中添加 JOBSPIDER_TOKEN={JOBSPIDER_TOKEN}",
          flush=True)


def _validate_task_id(task_id: str) -> None:
    """task_id 必须是 uuid4 前 8 位（小写十六进制）。防止路径穿越 / 注入。"""
    if not re.fullmatch(r"[0-9a-f]{8}", task_id or ""):
        raise HTTPException(status_code=400, detail="bad task_id")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # startup
    await db.init_db()
    await db.purge_old_tasks(days=7)
    await _mark_interrupted_tasks()
    _purge_old_output_files(days=7)
    try:
        yield
    finally:
        # shutdown
        _cancel_idle_browser_close()
        try:
            from core.browser_engine import BrowserEngine
            if BrowserEngine._instance:
                await BrowserEngine._instance.close()
        except Exception:
            pass
        await db.close_db()
        try:
            from sources._http import close_client
            await close_client()
        except Exception:
            pass
        # 释放共享浏览器（如果被 MOHRSS 等源拉起过）
        try:
            from sources._browser import close_browser
            await close_browser()
        except Exception:
            pass


async def _mark_interrupted_tasks():
    """服务启动时将 running/pending 状态的僵尸任务标记为 failed"""
    def _do():
        from threading import Lock
        c = db._conn()
        with db._write_lock:
            n = c.execute(
                "UPDATE tasks SET status='failed', error='服务重启，任务中断' "
                "WHERE status IN ('running','pending')"
            ).rowcount
            c.commit()
            return n
    try:
        n = await asyncio.to_thread(_do)
        if n:
            print(f"  [DB] 标记 {n} 个中断任务为 failed", flush=True)
    except Exception as e:
        print(f"  [DB] _mark_interrupted_tasks 失败: {e}", flush=True)


def _purge_old_output_files(days: int = 7):
    """清理 output/ 目录下超过 days 天的 JSON/CSV 文件"""
    import time
    cutoff = time.time() - days * 86400
    count = 0
    for pattern in ("jobs_*.json", "jobs_*.csv", "export_*.json", "export_*.csv"):
        for f in OUTPUT_DIR.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    count += 1
            except Exception:
                pass
    if count:
        print(f"  [Cleanup] 清理 {count} 个超过 {days} 天的输出文件", flush=True)


app = FastAPI(title="Job Scraper", version="2.3.0", lifespan=_lifespan)

# M10: CORS — 默认只允许本机来源
from fastapi.middleware.cors import CORSMiddleware
_CORS_ORIGINS = [
    "http://127.0.0.1:8765",
    "http://localhost:8765",
]
_extra_origins = os.getenv("JOBSPIDER_CORS_ORIGINS", "")
if _extra_origins:
    _CORS_ORIGINS.extend(o.strip() for o in _extra_origins.split(",") if o.strip())
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["X-Auth-Token", "Content-Type"],
)

# M09: 简易 IP 级速率限制 — 防止接口被滥用
import time as _time
_rate_limit_store: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("JOBSPIDER_RATE_LIMIT", "120"))

@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    now = _time.time()
    hits = _rate_limit_store.get(client_ip, [])
    hits = [t for t in hits if now - t < _RATE_LIMIT_WINDOW]
    if len(hits) >= _RATE_LIMIT_MAX_REQUESTS:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    hits.append(now)
    _rate_limit_store[client_ip] = hits
    if len(_rate_limit_store) > 1000:
        oldest_ip = min(_rate_limit_store, key=lambda k: _rate_limit_store[k][-1])
        del _rate_limit_store[oldest_ip]
    return await call_next(request)

# 任务状态存储（内存中保留最近 N 个，更早的从 DB 取）
tasks: "OrderedDict[str, dict]" = OrderedDict()# WebSocket 连接
active_ws: dict[str, list[WebSocket]] = {}
# 任务结果缓存（同样 LRU 限制）
task_results: "OrderedDict[str, dict]" = OrderedDict()

_MAX_INMEM_TASKS = 32  # 超过这个数就丢最旧的；DB 仍保留完整记录

# 浏览器空闲保留：任务结束后不立刻关 Chrome，避免下次任务重新启动窗口、
# 也避免部分平台的登录 cookie 在关闭时丢失。0 = 立即关闭。
_BROWSER_IDLE_MIN = float(os.getenv("JOBSPIDER_BROWSER_KEEP_MIN", "15"))
_browser_close_task: Optional[asyncio.Task] = None


def _cancel_idle_browser_close():
    global _browser_close_task
    if _browser_close_task is not None and not _browser_close_task.done():
        _browser_close_task.cancel()
    _browser_close_task = None


def _schedule_idle_browser_close():
    """任务结束后延迟关闭浏览器；期间新任务/登录窗口会取消这个定时器。"""
    global _browser_close_task
    _cancel_idle_browser_close()
    if _BROWSER_IDLE_MIN <= 0:
        from core.browser_engine import BrowserEngine
        if BrowserEngine._instance:
            asyncio.create_task(BrowserEngine._instance.close())
        return
    delay = _BROWSER_IDLE_MIN * 60

    async def _close_later():
        try:
            await asyncio.sleep(delay)
            from core.browser_engine import BrowserEngine
            if BrowserEngine._instance:
                print(f"  [BrowserEngine] 空闲 {_BROWSER_IDLE_MIN:g} 分钟，关闭浏览器",
                      flush=True)
                await BrowserEngine._instance.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"  [BrowserEngine] 空闲关闭失败: {e}", flush=True)

    _browser_close_task = asyncio.create_task(_close_later())


def _remember_task(task_id: str, entry: dict):
    """写入 tasks 字典并触发 LRU 淘汰"""
    if task_id in tasks:
        tasks.move_to_end(task_id)
    tasks[task_id] = entry
    while len(tasks) > _MAX_INMEM_TASKS:
        oldest, _ = tasks.popitem(last=False)
        task_results.pop(oldest, None)


def _remember_results(task_id: str, entry: dict):
    if task_id in task_results:
        task_results.move_to_end(task_id)
    task_results[task_id] = entry
    while len(task_results) > _MAX_INMEM_TASKS:
        task_results.popitem(last=False)


class ScrapeRequest(BaseModel):
    keyword: Optional[str] = None              # 单关键词（向后兼容）
    keywords: Optional[list[str]] = None       # 多关键词（行业多选模式）
    city: Optional[str] = None                 # 单城市（向后兼容）
    cities: Optional[list[str]] = None         # 多城市
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    experience: Optional[str] = None
    education: Optional[str] = None
    platforms: list[str] = ["boss", "zhilian", "job51", "liepin"]
    auto_apply: bool = True
    greeting_template: Optional[str] = None
    exclude_rules: Optional[str] = None        # LLM 解析的排除规则
    platform_limits: Optional[dict] = None     # {"boss": 10, "zhilian": 5, ...}
    apply_rate: Optional[str] = "normal"       # slow / normal / fast / turbo
    # 仅作前端预检提示/日志用；真正生效的 LLM 配置在 .env（由 /api/config 写入）
    llm_config: Optional[dict] = None
    global_limit: Optional[int] = None
    daily_limit_per_platform: Optional[dict] = None
    active_hours: Optional[list] = None         # [9, 22] — pydantic-friendly
    burst_pause_after: Optional[int] = None
    burst_pause_minutes: Optional[list] = None  # [30, 90]
    max_per_company_per_day: Optional[int] = None
    dry_run: bool = False
    apply_mode: str = "auto"                    # auto=正常发送；draft=只填不发
    experiences: Optional[list] = None          # 经验多选 ["1-3年","5-10年"]，与单值 experience 共存
    educations: Optional[list] = None           # 学历多选 ["本科","硕士"]
    skip_applied: bool = True                    # 跨任务去重：跳过 7 天内已真实投递的同一岗位


class EnrichRequest(BaseModel):
    items: list[dict] = []


class ScoreRequest(BaseModel):
    items: list[dict] = []
    prefs: str = ""


class LLMConfigRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None


class ResumeProfileRequest(BaseModel):
    profile_text: str = ""


@app.get("/")
async def index():
    """返回前端页面"""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Static files not found</h1>")


@app.post("/api/preflight")
async def preflight(req: ScrapeRequest):
    """投递前预览：返回平台估算、剩余配额、风险提示。

    不启动任务、不写库 — 只读 DB 历史。
    """
    try:
        from config import PLATFORM_DAILY_HARD_CAP, is_within_active_hours
    except ImportError:
        PLATFORM_DAILY_HARD_CAP = {"boss":150,"zhilian":80,"job51":120,"liepin":80}
        def is_within_active_hours(_): return True

    # 关键词/城市组合数
    kw = [k.strip() for k in (req.keywords or []) if k and k.strip()]
    if req.keyword and req.keyword.strip() and req.keyword.strip() not in kw:
        kw.append(req.keyword.strip())
    if not kw:
        raise HTTPException(status_code=422, detail="至少指定一个关键词")
    cities = [c.strip() for c in (req.cities or []) if c and c.strip()]
    if req.city and req.city.strip() and req.city.strip() not in cities:
        cities.append(req.city.strip())
    combos = max(1, len(kw)) * max(1, len(cities) or 1)

    pl = req.platform_limits or {}
    daily_cap = req.daily_limit_per_platform or {}
    estimates = []
    total = 0
    warnings = []

    try:
        active_risks = await db.list_platform_risks()
    except Exception:
        active_risks = []
    risk_map = {r.get("platform"): r for r in active_risks}
    for plat in (req.platforms or []):
        r = risk_map.get(plat)
        if r:
            warnings.append(
                f"{plat} 处于风控冷却中（至 {r.get('blocked_until')}："
                f"{r.get('reason') or r.get('state') or ''}）"
            )

    for plat in (req.platforms or []):
        per_platform_cap = pl.get(plat) if isinstance(pl, dict) else None
        per_daily_cap   = daily_cap.get(plat) if isinstance(daily_cap, dict) else None
        already = 0
        try:
            from core import db as _db
            already = await _db.count_applied_in_window(plat, 24)
        except Exception:
            already = 0
        hard = PLATFORM_DAILY_HARD_CAP.get(plat, 9999)
        cap_eff = per_platform_cap if per_platform_cap is not None else hard
        if per_daily_cap is not None:
            cap_eff = min(cap_eff, max(0, per_daily_cap - already))
        else:
            cap_eff = min(cap_eff, max(0, hard - already))
        estimates.append({
            "platform": plat,
            "platform_limit": per_platform_cap,
            "daily_limit": per_daily_cap,
            "hard_cap": hard,
            "applied_today": already,
            "estimated_remaining": cap_eff,
        })
        total += cap_eff
        if per_platform_cap is not None and per_platform_cap > hard:
            warnings.append(f"{plat} platform_limit={per_platform_cap} 超过硬上限 {hard}")
        if already >= hard:
            warnings.append(f"{plat} 今日已投 {already} 条，已达硬上限 {hard}")

    if req.global_limit is not None:
        total = min(total, max(0, int(req.global_limit)))

    # 时间窗
    in_window = True
    try:
        ah = req.active_hours
        if ah and len(ah) == 2:
            in_window = is_within_active_hours((int(ah[0]), int(ah[1])))
    except Exception:
        pass
    if not in_window:
        warnings.append("当前时间不在 active_hours 投递窗口内 — 任务启动后会立即暂停")

    # 速率估算（normal 平均 6s/条）
    rate = (req.apply_rate or "normal").lower()
    avg_sec = {"slow": 12.0, "normal": 6.0, "fast": 4.0, "turbo": 3.5}.get(rate, 6.0)
    eta_min = round(total * avg_sec / 60, 1)

    if rate == "turbo" and req.auto_apply and "boss" in (req.platforms or []):
        warnings.append("Boss + 极速 + 自动投递是高风险组合，建议 normal/slow")
    if req.greeting_template and not req.llm_config:
        warnings.append("固定招呼语模板会向多个 HR 发送相同文本，易被判为模板")

    return {
        "ok": True,
        "combos": combos,
        "platforms": estimates,
        "total_estimate": total,
        "eta_minutes": eta_min,
        "active_hours_now": in_window,
        "dry_run": bool(req.dry_run),
        "risk": active_risks,
        "warnings": warnings,
    }


@app.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    """启动一次抓取任务"""
    # S07: 防止并发任务冲突——检查是否有 running 任务
    for tid, t in tasks.items():
        if t.get("status") in ("pending", "running"):
            raise HTTPException(
                status_code=409,
                detail=f"已有任务 {tid} 正在运行，请等待完成或先停止"
            )

    task_id = str(uuid.uuid4())[:8]

    # Anti-ban hard cap validation
    try:
        from config import PLATFORM_DAILY_HARD_CAP
    except ImportError:
        PLATFORM_DAILY_HARD_CAP = {"boss":150,"zhilian":80,"job51":120,"liepin":80}

    pl = req.platform_limits or {}
    if isinstance(pl, dict):
        for k, v in pl.items():
            if v is None:
                continue
            try:
                vi = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422,
                                    detail=f"platform_limits[{k}] 必须是整数")
            cap = PLATFORM_DAILY_HARD_CAP.get(k)
            if cap is not None and vi > cap:
                raise HTTPException(
                    status_code=422,
                    detail=f"platform_limits[{k}]={vi} 超过该平台日硬上限 {cap}，"
                           f"建议 ≤ {cap} 以降低封号风险"
                )
    dl = req.daily_limit_per_platform or {}
    if isinstance(dl, dict):
        for k, v in dl.items():
            if v is None:
                continue
            try:
                vi = int(v)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422,
                                    detail=f"daily_limit_per_platform[{k}] 必须是整数")
            cap = PLATFORM_DAILY_HARD_CAP.get(k)
            if cap is not None and vi > cap:
                raise HTTPException(
                    status_code=422,
                    detail=f"daily_limit_per_platform[{k}]={vi} 超过硬上限 {cap}"
                )

    tasks[task_id] = {
        "status": "pending",
        "progress": {},
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "error": None,
        "params": req.model_dump(),
    }
    # 触发 LRU 淘汰
    _remember_task(task_id, tasks[task_id])

    # 任务持久化
    await db.save_task(task_id, req.model_dump(), status="pending")

    # 异步执行抓取 — 添加 done_callback 防止异常被吞
    task = asyncio.create_task(_run_scrape(task_id, req))
    tasks[task_id]["_task"] = task

    def _on_task_done(t):
        entry = tasks.get(task_id)
        if not entry:
            return
        if t.cancelled():
            print(f"  [Task {task_id}] was cancelled", flush=True)
            entry["status"] = "stopped"
            entry["finished_at"] = datetime.now().isoformat()
        elif t.exception():
            print(f"  [Task {task_id}] unhandled exception: {t.exception()}",
                  flush=True)

    task.add_done_callback(_on_task_done)

    return {"task_id": task_id, "status": "started"}


@app.post("/api/stop/{task_id}")
async def stop_task(task_id: str):
    """停止正在运行的投递任务（标记 stop_requested 并取消 asyncio 任务）"""
    _validate_task_id(task_id)
    if task_id not in tasks:
        return {"ok": False, "error": "Task not found"}
    t = tasks[task_id]
    t["stop_requested"] = True
    t["status"] = "stopping"
    runtime_task = t.get("_task")
    if runtime_task and not runtime_task.done():
        runtime_task.cancel()
        print(f"  [Task {task_id}] 已收到停止请求", flush=True)
        return {"ok": True, "task_id": task_id, "status": "stopping"}
    return {"ok": True, "task_id": task_id, "status": "already_done"}


@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    """获取任务状态"""
    _validate_task_id(task_id)
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    t = tasks[task_id]
    return {
        "task_id": task_id,
        "status": t["status"],
        "progress": t["progress"],
        "started_at": t["started_at"],
        "finished_at": t["finished_at"],
        "error": t["error"],
    }


@app.get("/api/results/{task_id}")
async def get_results(
    task_id: str,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(200, ge=1, le=500),
):
    """获取抓取结果（优先内存缓存，缺失时从 DB 加载历史）"""
    _validate_task_id(task_id)
    if task_id in task_results:
        data = task_results[task_id]
        all_jobs = data.get("jobs", [])
        total = len(all_jobs)
        page = all_jobs[offset:offset + limit]
        return {
            "task_id": task_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "jobs": page,
        }

    # DB 回退
    jobs, total = await db.get_task_jobs(task_id, offset=offset, limit=limit)
    if total == 0 and not jobs:
        raise HTTPException(status_code=404, detail="Results not found")
    return {
        "task_id": task_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "jobs": jobs,
    }


@app.get("/api/tasks")
async def list_tasks(limit: int = Query(50, ge=1, le=500)):
    """历史任务列表（从 DB 读，按 started_at 倒序）"""
    rows = await db.list_tasks(limit=limit)
    return {"tasks": rows}


@app.get("/api/recent-jobs")
async def recent_jobs(
    limit: int = Query(500, ge=1, le=5000),
    only_applied: bool = False,
):
    """跨任务聚合最近职位 — 空状态展示全部历史投递"""
    jobs = await db.get_recent_jobs(limit=limit, only_applied=only_applied)
    return {"jobs": jobs, "total": len(jobs)}


@app.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(task_id: str):
    """删除一条历史任务及其所有职位"""
    _validate_task_id(task_id)
    await db.delete_task(task_id)
    task_results.pop(task_id, None)
    tasks.pop(task_id, None)
    return {"ok": True}


@app.delete("/api/tasks")
async def delete_all_tasks_endpoint():
    """清空全部历史任务及其职位、输出文件"""
    n = await db.delete_all_tasks()
    task_results.clear()
    tasks.clear()
    # 清理 output/ 下所有结果文件
    count = 0
    for pattern in ("jobs_*.json", "jobs_*.csv", "export_*.json", "export_*.csv"):
        for f in OUTPUT_DIR.glob(pattern):
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
    if count:
        print(f"  [Cleanup] 清理 {count} 个输出文件", flush=True)
    return {"ok": True, "deleted_tasks": n, "deleted_files": count}


@app.get("/api/stats/{task_id}")
async def get_stats(task_id: str):
    """获取统计信息"""
    _validate_task_id(task_id)
    if task_id in task_results:
        data = task_results[task_id]
        jobs = data.get("jobs", [])
        results = data.get("results", [])
    else:
        # DB 回退：从持久化层重建 stats
        task_row = await db.get_task(task_id)
        db_jobs, _total = await db.get_task_jobs(task_id, offset=0, limit=10000)
        if not task_row and not db_jobs:
            raise HTTPException(status_code=404, detail="Stats not found")
        jobs = db_jobs
        results = []

    # 城市分布
    city_count: dict[str, int] = {}
    for j in jobs:
        c = j.get("city", "未知")
        city_count[c] = city_count.get(c, 0) + 1

    # 薪资分布
    salaries = [
        j for j in jobs
        if j.get("salary_min") or j.get("salary_max")
    ]
    salary_buckets = {}
    for j in salaries:
        avg = (j.get("salary_min", 0) or 0) + (j.get("salary_max", 0) or 0)
        if avg == 0:
            continue
        avg = avg / 2
        if avg < 5: bucket = "5K以下"
        elif avg < 10: bucket = "5-10K"
        elif avg < 15: bucket = "10-15K"
        elif avg < 20: bucket = "15-20K"
        elif avg < 30: bucket = "20-30K"
        elif avg < 50: bucket = "30-50K"
        else: bucket = "50K以上"
        salary_buckets[bucket] = salary_buckets.get(bucket, 0) + 1

    # 平台分布
    platform_count: dict[str, int] = {}
    for j in jobs:
        p = j.get("platform_name", "未知")
        platform_count[p] = platform_count.get(p, 0) + 1

    applied_count = sum(1 for j in jobs if j.get("apply_status") == "applied")

    return {
        "total": len(jobs),
        "applied": applied_count,
        "cities": dict(sorted(city_count.items(), key=lambda x: x[1], reverse=True)[:10]),
        "salary_distribution": salary_buckets,
        "platforms": platform_count,
        "platform_status": {r["platform_name"]: r["status"] for r in results},
    }


@app.get("/api/summary/{task_id}")
async def get_task_summary(task_id: str):
    """投递小结：成功/跳过/失败计数 + 跳过原因分桶，用于任务结束后的可读汇总。"""
    _validate_task_id(task_id)
    breakdown = await db.get_task_skip_breakdown(task_id)
    return {"ok": True, "task_id": task_id, **breakdown}


@app.get("/api/export")
async def export_results(task_id: str, format: str = "json"):
    """导出结果文件"""
    _validate_task_id(task_id)
    if task_id in task_results:
        data = task_results[task_id]
    else:
        # DB 回退：从持久化层拼出 data 字典
        task_row = await db.get_task(task_id)
        db_jobs, _total = await db.get_task_jobs(task_id, offset=0, limit=10000)
        if not task_row and not db_jobs:
            raise HTTPException(status_code=404, detail="Results not found")
        data = {
            "jobs": db_jobs,
            "results": [],
            "total": len(db_jobs),
            "json_path": "",
            "csv_path": "",
        }

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    if format == "csv":
        import csv
        import io
        from core.output import _atomic_write
        filepath = out_dir / f"export_{task_id}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        jobs = data.get("jobs", [])

        fieldnames = [
            "job_title", "company_name", "salary_range", "salary_min", "salary_max",
            "city", "experience_required", "education_required",
            "platform_name", "job_url", "publish_time",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for j in jobs:
            writer.writerow(j)
        _atomic_write(filepath, buf.getvalue(), encoding="utf-8-sig")
    else:
        from core.output import _atomic_write
        filepath = out_dir / f"export_{task_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
        _atomic_write(filepath,
                      json.dumps(data, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    return FileResponse(filepath, filename=filepath.name)


@app.get("/api/industries")
async def get_industries_endpoint():
    """返回所有行业及其对应的岗位列表"""
    from industries import INDUSTRY_JOBS
    return {"industries": INDUSTRY_JOBS}


@app.get("/api/cities")
async def get_cities_endpoint():
    """返回热门城市 + 按省份分组的完整城市列表"""
    from cities import HOT_CITIES, CITIES_BY_PROVINCE
    return {"hot": HOT_CITIES, "provinces": CITIES_BY_PROVINCE}


# ───────── 公开岗位平台聚合 ─────────

_public_jobs_lock = asyncio.Lock()
_public_jobs_busy = False
_public_jobs_last: dict = {"at": None, "per_source": {}, "errors": []}


@asynccontextmanager
async def _public_jobs_guard():
    """原子 busy 判断 + 加锁，避免两个请求同时通过 locked() 检查。"""
    global _public_jobs_busy
    if _public_jobs_busy:
        yield False
        return
    _public_jobs_busy = True
    try:
        async with _public_jobs_lock:
            yield True
    finally:
        _public_jobs_busy = False


@app.get("/api/public_jobs/sources")
async def list_public_job_sources():
    """返回前端要渲染的源开关 chip 列表（key/name/category/enabled）"""
    from sources import list_sources_meta
    return {"sources": list_sources_meta()}


@app.get("/api/public_jobs/status")
async def public_jobs_status():
    """最近一次刷新摘要（每个源拿到了多少条 / 哪些挂了），供前端展示健康度。"""
    return {
        "fetched_at": _public_jobs_last.get("at"),
        "per_source": _public_jobs_last.get("per_source") or {},
        "errors": _public_jobs_last.get("errors") or [],
        "industry": _public_jobs_last.get("industry") or "",
        "running": _public_jobs_busy,
    }


@app.post("/api/public_jobs/refresh")
async def refresh_public_jobs(
    limit_per_source: int = Query(120, ge=1, le=500),
    industry: Optional[str] = Query(None, description="行业大类名，传给 WebSearch 用"),
    keyword: Optional[str] = Query(None, description="岗位关键词，传给所有支持的源"),
    sources: Optional[str] = Query(None, description="逗号分隔的 source key，缺省=全部"),
    force: bool = Query(False, description="清空 HTTP 缓存后重抓（默认复用 TTL 缓存）"),
):
    """并发拉所有（或指定）公开岗位源，返回合并去重后的列表。

    设计：
      - 不接受 keyword/city 过滤参数 —— 前端用筛选器在客户端 filter。
        多数源天然没有 city 字段，强行后端过滤会把数据全砍光。
      - industry 仅传给 WebSearch 当关键词用，其它源忽略。
      - 不落 DB —— 公开列表是"实时视图"，每次点击重抓。
    """
    async with _public_jobs_guard() as ok:
        if not ok:
            return {"status": "busy", "message": "上一次抓取还在跑"}
        import time
        from sources import iter_sources
        from sources._http import clear_cache, drain_errors
        drain_errors()  # 清掉上一轮的 HTTP 错误
        if force:
            clear_cache()
        all_sources = iter_sources()
        if sources:
            wanted = {s.strip() for s in sources.split(",") if s.strip()}
            all_sources = [s for s in all_sources if s.key in wanted]

        ind = (industry or "").strip()
        kw = (keyword or "").strip()

        # 预先识别 websearch 无关键词的情况 — 直接跳过、记入 errors，
        # 不然 source 内部会因为空 query 报错或返回噪声
        skipped_keys: set[str] = set()
        early_errors: list[dict] = []
        for s in all_sources:
            if s.key == "websearch" and not (kw or ind):
                skipped_keys.add(s.key)
                early_errors.append({
                    "source": "websearch",
                    "skipped": True,
                    "reason": "需要关键词",
                })

        async def _run(s):
            # WebSearch 必须有关键词 → 优先用 keyword，回退到 industry；
            # 其它源也尽量把 keyword 传下去（支持的源会用，不支持的忽略）
            if s.key == "websearch":
                kw_for_source = kw or ind
            else:
                kw_for_source = kw
            t0 = time.perf_counter()
            try:
                items = await asyncio.wait_for(
                    s.fetch(limit=limit_per_source, keyword=kw_for_source, city=""),
                    timeout=60.0,
                )
                return s.key, items, None, time.perf_counter() - t0
            except asyncio.TimeoutError:
                return s.key, [], "TimeoutError: >60s", time.perf_counter() - t0
            except Exception as e:
                return s.key, [], f"{type(e).__name__}: {e}", time.perf_counter() - t0

        runnable_sources = [s for s in all_sources if s.key not in skipped_keys]
        results = await asyncio.gather(*[_run(s) for s in runnable_sources])

        # 服务端关键词「硬过滤」— 防止 iguopin/websearch 返回的「宽松匹配」漏出无关结果。
        # 公告类源（sasac/mohrss）跳过：标题通常是「XX单位2026招聘公告」格式，没有具体岗位词。
        bulletin_keys = {s.key for s in all_sources if getattr(s, "bulletin", False)}
        kw_terms = [t.strip().lower() for t in kw.split() if t.strip()] if kw else []

        def _kw_hit(it: dict) -> bool:
            if not kw_terms:
                return True
            blob = " ".join([
                it.get("title") or "",
                it.get("company") or "",
                it.get("summary") or "",
            ]).lower()
            # 任一关键词命中即保留（OR 语义，跟前端筛选一致）
            return any(t in blob for t in kw_terms)

        merged: dict[str, dict] = {}
        per_source: dict[str, int] = {}
        per_source_raw: dict[str, int] = {}
        timings: dict[str, float] = {}
        errors: list[dict] = list(early_errors)  # 预跳过的源先入列
        errors.extend(drain_errors())
        for key, items, err, elapsed in results:
            if err:
                errors.append({"source": key, "error": err})
            per_source_raw[key] = len(items)
            # bulletin 源不做关键词硬过滤；其它源必须命中关键词才保留
            if kw_terms and key not in bulletin_keys:
                items = [it for it in items if _kw_hit(it)]
            for it in items:
                _id = it.get("id")
                if _id and _id not in merged:
                    merged[_id] = it
            per_source[key] = len(items)
            timings[key] = round(elapsed, 2)
        # 标记被跳过的源 — 让前端 chip 不至于显示"未刷新"
        for k in skipped_keys:
            per_source.setdefault(k, 0)
            per_source_raw.setdefault(k, 0)

        # 模糊去重：跨源同 title+company 视为重复（id 级合并之后再跑一遍）
        # core/dedup.py 的 deduplicate_jobs 期望 JobInfo 对象 — 公开列表是 dict，
        # 这里走 10 行内联实现：(title.lower, company.lower) 作为聚合 key
        merged_list = list(merged.values())
        seen_keys: set[tuple[str, str, str]] = set()
        deduped: list[dict] = []
        for it in merged_list:
            # 公司名归一化：去掉部门后缀（括号内）、空白
            company = re.sub(r"[（(【\[].*?[）)】\]]", "",
                             (it.get("company") or "").lower())
            company = re.sub(r"\s+", "", company)
            k = (
                (it.get("title") or "").strip().lower(),
                company,
                (it.get("location") or "").strip().lower(),
            )
            # 全空键不参与去重（避免把多个无 title/company 的条目误合并）
            if k != ("", "", "") and k in seen_keys:
                continue
            if k != ("", "", ""):
                seen_keys.add(k)
            deduped.append(it)

        # 注：refresh 只做抓取/去重，不内联 LLM 增强；
        # 富化与打分由 /api/public_jobs/llm_enrich、/llm_score 单独触发。
        all_items = sorted(
            deduped,
            key=lambda x: x.get("publish_time") or x.get("fetched_at") or "",
            reverse=True,
        )
        _public_jobs_last.update({
            "at": datetime.now().isoformat(timespec="seconds"),
            "per_source": per_source,
            "errors": errors,
            "industry": ind,
        })
        return {
            "status": "ok",
            "items": all_items,
            "per_source": per_source,
            "per_source_raw": per_source_raw,  # 关键词过滤前每源数量 — 让前端能提示"30 条中 3 条匹配"
            "timings": timings,
            "errors": errors,
            "industry": ind,
            "keyword": kw,
            "fetched_at": _public_jobs_last["at"],
        }


# ───────── 公开岗位 · LLM 增强 ─────────
# 业务逻辑全部委托给 core.public_jobs_service 中的 _enricher / _scorer 单例，
# 缓存随实例存活；本文件只做请求体解析与响应壳。


@app.post("/api/public_jobs/llm_enrich")
async def llm_enrich_public_jobs(body: EnrichRequest):
    """对前端选定的若干公开岗位条目跑 LLM 智能字段抽取。

    body: {"items": [{id, title, company, summary, location, education,
                      experience, publish_time}, ...]}
    返回: {"results": {id: {deadline?, location?, education?, experience?, salary?}, ...}}
    """
    return await _enricher.enrich(body.items or [])


@app.post("/api/public_jobs/llm_score")
async def llm_score_public_jobs(body: ScoreRequest):
    """根据用户偏好对岗位打匹配分。

    body: {"items": [...], "prefs": {...用户求职偏好...}}
    返回: {"results": {id: {score, reason}, ...}}
    """
    prefs = (body.prefs or "").strip()
    if not prefs:
        return {"error": "需要先在「求职偏好」里写明你的期望"}

    return await _scorer.score(body.items or [], prefs)


@app.get("/api/public_jobs/diag")
async def diag_public_jobs():
    """快速逐源体检：每个源独立调一次小 limit，返回详细结果。

    用途：用户看到全 0 时一键自检，立即知道是网络问题、单源失败还是空数据。
    """
    import time
    from sources import iter_sources
    async with _public_jobs_guard() as ok:
        if not ok:
            return {"results": [], "error": "refresh running"}
        out = []
        for s in iter_sources():
            t0 = time.perf_counter()
            try:
                items = await asyncio.wait_for(
                    s.fetch(limit=3, keyword="", city=""), timeout=30.0)
                sample = items[0] if items else None
                out.append({
                    "key": s.key,
                    "name": s.display_name,
                    "category": s.category,
                    "count": len(items),
                    "elapsed": round(time.perf_counter() - t0, 2),
                    "ok": True,
                    "error": None,
                    "sample_title": (sample or {}).get("title", "")[:60] if sample else None,
                })
            except Exception as e:
                out.append({
                    "key": s.key,
                    "name": s.display_name,
                    "category": s.category,
                    "count": 0,
                    "elapsed": round(time.perf_counter() - t0, 2),
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "sample_title": None,
                })
        return {"results": out, "at": datetime.now().isoformat(timespec="seconds")}

@app.get("/api/risk")
async def risk_status():
    """当前仍生效的平台风控冷却。"""
    return {"risks": await db.list_platform_risks()}


@app.post("/api/risk/clear/{platform}")
async def risk_clear(platform: str):
    """人工完成验证后，手动解除某平台的风控冷却。"""
    if platform not in MasterAgent.AGENT_MAP:
        raise HTTPException(status_code=400, detail="unknown platform")
    await db.clear_platform_risk(platform)
    return {"ok": True, "platform": platform}


@app.get("/api/config")
async def get_config():
    """获取当前 LLM 配置（脱敏，从环境变量实时读取）"""
    provider = os.getenv("LLM_PROVIDER", "")
    model = os.getenv("LLM_MODEL", "")
    base_url = os.getenv("LLM_BASE_URL", "")
    enabled = os.getenv("LLM_ENABLED", "false").lower() == "true"

    # 检查是否有任何 API Key
    key_names = ["DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "OPENROUTER_API_KEY", "SILICONFLOW_API_KEY", "LLM_API_KEY"]
    has_key = any(os.getenv(k, "") for k in key_names)

    return {
        "config": {
            "provider": provider,
            "model": model,
            "api_key": "***" if has_key else "",
            "base_url": base_url,
            "enabled": enabled,
        } if provider else None,
    }


@app.post("/api/config")
async def save_config(req: LLMConfigRequest, request: Request):
    """保存 LLM 配置到 .env 文件"""
    # 鉴权：仅在用户显式在 .env 设置 JOBSPIDER_TOKEN 时要求 X-Auth-Token。
    # 自动生成的临时 token 前端并不知道，若也强制校验会导致「保存到 .env」永远 401。
    if _TOKEN_FROM_ENV and request.headers.get("X-Auth-Token") != JOBSPIDER_TOKEN:
        raise HTTPException(status_code=401, detail="auth required")
    try:
        from config import ROOT_DIR
        _ENV_FILE = ROOT_DIR / ".env"

        provider = req.provider or ""
        model = req.model or ""
        api_key = req.api_key or ""
        base_url = req.base_url or ""

        if not provider:
            return {"ok": False, "error": "请填写提供商标识"}

        # 构建要写入/更新的 key-value 对
        updates: dict[str, str] = {
            "LLM_PROVIDER": provider,
            "LLM_ENABLED": "true",
        }
        if model:
            updates["LLM_MODEL"] = model
        if base_url:
            updates["LLM_BASE_URL"] = base_url
        if api_key and api_key != "***":
            key_env_map = {
                "deepseek": "DEEPSEEK_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "siliconflow": "SILICONFLOW_API_KEY",
            }
            env_key = key_env_map.get(provider.lower(), "LLM_API_KEY")
            updates[env_key] = api_key
            updates["LLM_API_KEY"] = api_key

        # 读取-修改-写回：逐行保留原有注释和非 LLM 配置
        original_lines: list[str] = []
        if _ENV_FILE.exists():
            with open(_ENV_FILE, "r", encoding="utf-8") as f:
                original_lines = f.read().splitlines()

        updated_keys: set[str] = set()
        new_lines: list[str] = []
        for line in original_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in updates:
                    new_lines.append(f"{k}={updates[k]}")
                    updated_keys.add(k)
                    continue
            new_lines.append(line)

        # 追加 .env 中尚不存在的新 key
        remaining = {k: v for k, v in updates.items() if k not in updated_keys}
        if remaining:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            for k, v in remaining.items():
                new_lines.append(f"{k}={v}")

        with open(_ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")

        # 刷新当前进程的环境变量（从刚写好的 .env 重新加载，override=True）
        # 避免直接 os.environ[...]= 污染进程级 LLM_* 状态。
        try:
            from dotenv import load_dotenv as _reload_dotenv
            _reload_dotenv(_ENV_FILE, override=True)
        except ImportError:
            pass

        return {"ok": True, "env_file": str(_ENV_FILE)}
    except Exception as e:
        # 不要把异常详情泄露给客户端（可能含文件路径、堆栈等）
        print(f"[/api/config] save failed: {type(e).__name__}: {e}", flush=True)
        return {"ok": False, "error": "config save failed"}


# ───────── 简历（用于生成个性化招呼语） ─────────

def _resume_response(data: dict) -> dict:
    return {
        "exists": True,
        "filename": data.get("filename") or "",
        "uploaded_at": data.get("uploaded_at"),
        "updated_at": data.get("updated_at"),
        "chars": int(data.get("chars") or 0),
        "profile_text": data.get("profile_text") or "",
        "profile_source": data.get("profile_source") or "",
        "profile": data.get("profile") or {},
    }


@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...)):
    """上传简历 → 提取文本 → 打码 → LLM 解析摘要 → 本地保存。"""
    from core.resume_parser import (MAX_UPLOAD_BYTES, ResumeParseError,
                                    extract_text, mask_contacts,
                                    normalize_text)
    from core.resume_store import build_profile_text, save_resume
    from core.llm_extractor import LLMExtractor

    filename = file.filename or "resume"
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 限制")
    try:
        text = await asyncio.to_thread(extract_text, filename, data)
    except ResumeParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"简历解析失败：{type(e).__name__}")

    text = normalize_text(mask_contacts(text))
    profile: dict = {}
    try:
        profile = await asyncio.wait_for(
            LLMExtractor().parse_resume(text), timeout=30.0)
    except Exception:
        profile = {}
    if not isinstance(profile, dict):
        profile = {}

    profile_text = build_profile_text(profile, text)
    source = "llm" if profile else "fallback"
    saved = save_resume(filename, text, profile, profile_text, source)
    resp = _resume_response(saved)
    resp["ok"] = True
    if not profile:
        resp["warning"] = ("LLM 未配置或解析失败，当前使用简历原文摘录；"
                           "配置 LLM 后可点「重新解析」")
    return resp


@app.get("/api/resume")
async def get_resume():
    from core.resume_store import load_resume
    data = load_resume()
    if not data:
        return JSONResponse({"exists": False},
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(_resume_response(data),
                        headers={"Cache-Control": "no-store"})


@app.post("/api/resume/profile")
async def update_resume_profile(body: ResumeProfileRequest):
    """保存人工编辑后的简历摘要。"""
    from core.resume_store import update_profile_text
    try:
        saved = update_profile_text(body.profile_text)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="还没有上传简历")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    resp = _resume_response(saved)
    resp["ok"] = True
    return resp


@app.post("/api/resume/reparse")
async def reparse_resume():
    """用已保存的打码原文重新跑 LLM 解析。"""
    from core.resume_store import build_profile_text, load_resume, save_resume
    from core.llm_extractor import LLMExtractor

    data = load_resume()
    if not data:
        raise HTTPException(status_code=404, detail="还没有上传简历")
    raw = data.get("raw_text") or ""
    if not raw:
        raise HTTPException(status_code=400, detail="简历原文为空，请重新上传")

    try:
        profile = await asyncio.wait_for(
            LLMExtractor().parse_resume(raw), timeout=30.0)
    except Exception:
        profile = {}
    if not isinstance(profile, dict) or not profile:
        return {"ok": False,
                "warning": "LLM 未配置或解析失败，请先保存 LLM 配置后重试"}

    profile_text = build_profile_text(profile, raw)
    saved = save_resume(data.get("filename") or "resume", raw, profile,
                        profile_text, "llm")
    resp = _resume_response(saved)
    resp["ok"] = True
    return resp


@app.delete("/api/resume")
async def remove_resume():
    from core.resume_store import delete_resume
    return {"ok": True, "deleted": delete_resume()}


# ───────── 登录窗口 / 登录态 ─────────

class LoginRequest(BaseModel):
    platforms: list[str] = ["boss", "zhilian", "job51", "liepin"]


@app.post("/api/login/open")
async def open_login_window(body: LoginRequest):
    """在项目 chrome_profile 里打开各平台登录页，登录后 cookie 会被后续任务复用。"""
    from core.login_manager import open_login
    if any(t.get("status") in ("running", "pending", "stopping")
           for t in tasks.values()):
        raise HTTPException(status_code=409,
                            detail="任务运行中，请先停止任务再打开登录窗口")
    _cancel_idle_browser_close()
    return await open_login(body.platforms or [])


@app.get("/api/login/status")
async def login_status(probe: bool = Query(False, description="对 cookie 未知的平台做页面探测")):
    from core.login_manager import check_status
    return await check_status(probe_dom=probe)


@app.post("/api/login/close")
async def close_login_window():
    from core.login_manager import close_login_pages
    await close_login_pages()
    _schedule_idle_browser_close()
    return {"ok": True}


_DEFAULT_WS_ORIGINS = {"http://127.0.0.1:8765", "http://localhost:8765"}


def _allowed_ws_origins() -> set[str]:
    extra = os.getenv("JOBSPIDER_WS_ORIGINS", "")
    out = set(_DEFAULT_WS_ORIGINS)
    if extra:
        for o in extra.split(","):
            o = o.strip()
            if o:
                out.add(o)
    return out


_MAX_WS_PER_TASK = 10
_MAX_WS_TOTAL = 50


@app.websocket("/ws/{task_id}")
async def websocket_endpoint(ws: WebSocket, task_id: str):
    """WebSocket 实时进度推送"""
    if not re.fullmatch(r"[0-9a-f]{8}", task_id or ""):
        await ws.close(code=1008)
        return
    origin = ws.headers.get("origin")
    if origin and origin not in _allowed_ws_origins():
        await ws.close(code=1008)
        return

    # 连接数限制
    total_ws = sum(len(v) for v in active_ws.values())
    task_ws = len(active_ws.get(task_id, []))
    if total_ws >= _MAX_WS_TOTAL or task_ws >= _MAX_WS_PER_TASK:
        await ws.close(code=1013)
        return

    await ws.accept()
    if task_id not in active_ws:
        active_ws[task_id] = []
    active_ws[task_id].append(ws)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        try:
            active_ws[task_id].remove(ws)
        except ValueError:
            pass
        if not active_ws.get(task_id):
            active_ws.pop(task_id, None)


async def _broadcast_progress(task_id: str, platform: str, count: int, status: str):
    """广播进度到 WebSocket 客户端"""
    await _broadcast(task_id, {
        "type": "progress",
        "platform": platform,
        "platform_key": platform,
        "count": count,
        "status": status,
    })


async def _broadcast_job(task_id: str, job: dict, event: str = "update"):
    """广播单条职位（新增 / 投递状态变更）到 WebSocket 客户端

    event: "new" = 新抓到 | "update" = 投递状态变更
    """
    await _broadcast(task_id, {
        "type": "job",
        "event": event,
        "job": job,
    })


async def _broadcast_log(task_id: str, message: str):
    """广播日志消息到 WebSocket 客户端（前端实时日志面板用）"""
    await _broadcast(task_id, {
        "type": "log",
        "message": message,
    })


async def _broadcast(task_id: str, payload: dict):
    """底层广播：清理失效连接"""
    if task_id not in active_ws:
        return
    msg = json.dumps(payload, ensure_ascii=False, default=str)
    dead = []
    for ws in active_ws[task_id]:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in active_ws[task_id]:
            active_ws[task_id].remove(ws)


async def _run_scrape(task_id: str, req: ScrapeRequest):
    """后台执行抓取任务，支持多关键词 × 多城市，可中途停止"""
    # 新任务复用现有浏览器窗口（登录态也还在），取消空闲关闭定时器
    _cancel_idle_browser_close()
    # S17: 持有本地引用，防止 LRU 淘汰后 KeyError
    entry = tasks[task_id]

    # 整理关键词列表
    kw_list: list[str] = []
    if req.keywords:
        kw_list = [k.strip() for k in req.keywords if k and k.strip()]
    if req.keyword and req.keyword.strip() and req.keyword.strip() not in kw_list:
        kw_list.append(req.keyword.strip())
    if not kw_list:
        entry["status"] = "failed"
        entry["error"] = "至少指定一个关键词或岗位"
        entry["finished_at"] = datetime.now().isoformat()
        return

    # 整理城市列表（多选）
    city_list: list[str] = []
    if req.cities:
        city_list = [c.strip() for c in req.cities if c and c.strip()]
    if req.city and req.city.strip() and req.city.strip() not in city_list:
        city_list.append(req.city.strip())
    if not city_list:
        city_list = [None]  # 全国

    print(f"\n  [Task {task_id}] _run_scrape: keywords={kw_list}, "
          f"cities={city_list}, platforms={req.platforms}, "
          f"limits={req.platform_limits}, exclude={bool(req.exclude_rules)}",
          flush=True)
    entry["status"] = "running"

    # 初始化进度状态
    for platform_key in req.platforms:
        entry["progress"][platform_key] = {
            "status": "waiting",
            "count": 0,
            "name": MasterAgent.PLATFORM_NAMES.get(platform_key, platform_key),
        }

    # M04: 持有回调中 create_task 的引用，防止被 GC 回收
    _bg_tasks: set[asyncio.Task] = set()

    def _track(t: asyncio.Task):
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)

    # 进度回调 — 推送到 WebSocket
    def on_progress(platform: str, count: int, status: str):
        if platform in entry["progress"]:
            entry["progress"][platform] = {
                "status": status,
                "count": count,
                "name": MasterAgent.PLATFORM_NAMES.get(platform, platform),
            }
        _track(asyncio.create_task(_broadcast_progress(task_id, platform, count, status)))

    # 单条 job 回调 — 实时推送投递状态变更 + 持久化
    def on_job(job_dict: dict, event: str):
        _track(asyncio.create_task(_broadcast_job(task_id, job_dict, event)))
        _track(asyncio.create_task(db.upsert_job(task_id, job_dict)))

    # 日志回调 — 实时推送到前端日志面板
    def on_log(message: str):
        _track(asyncio.create_task(_broadcast_log(task_id, message)))

    merged_jobs: list[dict] = []
    merged_results: list[dict] = []

    try:
        # 仅打印本次请求携带的配置，便于排查；真正生效的是 /api/config 写入 .env 的配置
        if req.llm_config:
            llm_cfg = req.llm_config
            print(f"  [LLM 配置] provider={llm_cfg.get('provider', 'deepseek')} "
                  f"model={llm_cfg.get('model', 'deepseek-chat')} "
                  f"base_url={llm_cfg.get('base_url', '')} "
                  f"has_key={bool(llm_cfg.get('api_key'))}",
                  flush=True)

        params_list: list[SearchParams] = []
        active_hours = (tuple(req.active_hours)
                        if req.active_hours and len(req.active_hours) == 2
                        else None)
        burst_minutes = (tuple(req.burst_pause_minutes)
                         if req.burst_pause_minutes
                         and len(req.burst_pause_minutes) == 2
                         else (30, 90))
        for city in city_list:
            for kw in kw_list:
                params_list.append(SearchParams(
                    keyword=kw,
                    city=city,
                    salary_min=req.salary_min,
                    salary_max=req.salary_max,
                    experience=req.experience,
                    education=req.education,
                    experiences=req.experiences or [],
                    educations=req.educations or [],
                    skip_applied=req.skip_applied,
                    platforms=req.platforms,
                    auto_apply=req.auto_apply,
                    greeting_template=req.greeting_template,
                    exclude_rules=req.exclude_rules,
                    # 任务级上限：每个组合传入同一个完整上限，由 lane 内累计计数控制。
                    platform_limits=dict(req.platform_limits or {}),
                    apply_rate=req.apply_rate or "normal",
                    global_limit=req.global_limit,
                    daily_limit_per_platform=dict(
                        req.daily_limit_per_platform or {}),
                    active_hours=active_hours,
                    burst_pause_after=req.burst_pause_after,
                    burst_pause_minutes=burst_minutes,
                    max_per_company_per_day=req.max_per_company_per_day,
                    dry_run=req.dry_run,
                    apply_mode=getattr(req, "apply_mode", "auto") or "auto",
                ))
        print(f"\n  [Task {task_id}] {len(params_list)} 个 kw×city 组合，"
              f"平台={req.platforms}", flush=True)

        agent = MasterAgent(params_list, progress_callback=on_progress,
                            job_callback=on_job, log_callback=on_log)
        agent.set_stop_check(lambda: bool(entry.get("stop_requested")))
        agent.set_global_limit(req.global_limit)
        result = await agent.run()
        merged_jobs.extend(result["jobs"])
        merged_results.extend(result["results"])
        _remember_results(task_id, {
            "jobs": merged_jobs,
            "results": merged_results,
            "total": len(merged_jobs),
            "json_path": result.get("json_path", ""),
            "csv_path": result.get("csv_path", ""),
        })

        if entry.get("stop_requested"):
            entry["status"] = "stopped"
            await db.update_task(task_id, status="stopped")
        else:
            entry["status"] = "done"
            await db.update_task(task_id, status="done")

        applied_count = sum(1 for j in merged_jobs if j.get("apply_status") == "applied")
        uncertain_count = sum(1 for j in merged_jobs if j.get("apply_status") == "uncertain")
        skipped_count = sum(1 for j in merged_jobs if j.get("apply_status") == "skipped")
        failed_count = sum(1 for j in merged_jobs if j.get("apply_status") == "failed")
        print(f"\n  [Task {task_id}] 最终汇总: 总计 {len(merged_jobs)} 条, "
              f"已投递 {applied_count}, 待确认 {uncertain_count}, "
              f"跳过 {skipped_count}, 失败 {failed_count}",
              flush=True)

    except asyncio.CancelledError:
        print(f"\n  [Task {task_id}] 已停止（用户主动中止）", flush=True)
        entry["status"] = "stopped"
        entry["error"] = "用户停止"
        await db.update_task(task_id, status="stopped", error="用户停止")
        if task_id in task_results:
            task_results[task_id]["jobs"] = merged_jobs
            task_results[task_id]["results"] = merged_results
    except Exception as e:
        import traceback
        entry["status"] = "failed"
        entry["error"] = str(e)
        print(f"\n  [ERROR] Task {task_id} failed: {e}", flush=True)
        traceback.print_exc()
        await db.update_task(task_id, status="failed", error=str(e))

    # 等所有 job 的 DB 写入/WS 广播任务落完，再刷新统计
    while _bg_tasks:
        await asyncio.gather(*list(_bg_tasks), return_exceptions=True)

    entry["finished_at"] = datetime.now().isoformat()
    await db.update_task(task_id, finished=True)
    await db.refresh_task_counters(task_id)

    # 任务（含所有 kw×city 组合）跑完后，不再立即关浏览器：
    # 保留窗口可以复用登录态（部分平台会清 session cookie），也让下一次
    # 任务不必重新冷启动 Chrome。超过 JOBSPIDER_BROWSER_KEEP_MIN 分钟无任务再关。
    _schedule_idle_browser_close()

    print(f"  [Task {task_id}] finished: {entry['status']}", flush=True)


# 启动时创建必要目录
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    import uvicorn
    # 默认仅本地访问；如需 LAN/外网，显式设 HOST=0.0.0.0
    host = os.getenv("JOBSPIDER_HOST", "127.0.0.1")
    port = int(os.getenv("JOBSPIDER_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")
