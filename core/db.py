"""持久化层 — SQLite (Python 标准库，零依赖、可移植)

为什么用 SQLite：
- Python 内置 sqlite3 模块，pip install 不需要任何额外包
- 单文件 data/jobs.db，clone 项目后开箱即用
- WAL 模式下读写并发足够本地工具使用

设计：
- 两张表 tasks + jobs，(task_id, platform, job_id) 复合主键
- 所有接口都是 async（用 asyncio.to_thread 包装同步调用），避免阻塞 FastAPI 事件循环
- DB 异常被吞，不影响主流程（持久化是辅助功能）
"""
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional

from config import ROOT_DIR

DB_DIR = ROOT_DIR / "data"
DB_PATH = DB_DIR / "jobs.db"

# 串行化写：sqlite3 在多线程并发写时容易 "database is locked"
_write_lock = Lock()

# 单一长连接 + 锁保护读写。避免每次查询都重新 connect + PRAGMA。
# check_same_thread=False 配合 _write_lock 串行化即可安全跨线程使用。
_conn_singleton: Optional[sqlite3.Connection] = None


def _conn() -> sqlite3.Connection:
    global _conn_singleton
    if _conn_singleton is not None:
        return _conn_singleton
    DB_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10.0,
                        check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    _conn_singleton = c
    return c


def _init_sync():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            params_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            total INTEGER DEFAULT 0,
            applied INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS jobs (
            task_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            job_id TEXT NOT NULL,
            job_title TEXT,
            company_name TEXT,
            salary_range TEXT,
            salary_min REAL,
            salary_max REAL,
            city TEXT,
            experience_required TEXT,
            education_required TEXT,
            job_url TEXT,
            apply_status TEXT,
            apply_skip_reason TEXT,
            greeting_sent TEXT,
            data_json TEXT,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (task_id, platform, job_id),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_task ON jobs(task_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(apply_status);
        CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tasks_started ON tasks(started_at DESC);
        """)


async def init_db():
    """应用启动时调用一次"""
    await asyncio.to_thread(_init_sync)
    print(f"  [DB] SQLite 就绪: {DB_PATH}", flush=True)


async def close_db():
    global _conn_singleton
    if _conn_singleton:
        _conn_singleton.close()
        _conn_singleton = None


# ───────── 任务 ─────────

def _save_task(task_id: str, params: dict, status: str):
    now = datetime.now().isoformat()
    with _write_lock, _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO tasks
                (task_id, status, params_json, started_at, finished_at, error,
                 total, applied)
            VALUES (?, ?, ?, ?,
                    (SELECT finished_at FROM tasks WHERE task_id=?),
                    (SELECT error FROM tasks WHERE task_id=?),
                    COALESCE((SELECT total FROM tasks WHERE task_id=?), 0),
                    COALESCE((SELECT applied FROM tasks WHERE task_id=?), 0))
        """, (task_id, status,
              json.dumps(params, ensure_ascii=False, default=str), now,
              task_id, task_id, task_id, task_id))


async def save_task(task_id: str, params: dict, status: str = "pending"):
    try:
        await asyncio.to_thread(_save_task, task_id, params, status)
    except Exception as e:
        print(f"  [DB] save_task 失败: {e}", flush=True)


def _update_task(task_id: str, status, error, finished):
    fields, vals = [], []
    if status is not None:
        fields.append("status = ?"); vals.append(status)
    if error is not None:
        fields.append("error = ?"); vals.append(error)
    if finished:
        fields.append("finished_at = ?"); vals.append(datetime.now().isoformat())
    if not fields:
        return
    vals.append(task_id)
    with _write_lock, _conn() as c:
        c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?", vals)


async def update_task(task_id: str, *, status: Optional[str] = None,
                      error: Optional[str] = None, finished: bool = False):
    try:
        await asyncio.to_thread(_update_task, task_id, status, error, finished)
    except Exception as e:
        print(f"  [DB] update_task 失败: {e}", flush=True)


def _refresh_counters(task_id: str):
    with _write_lock, _conn() as c:
        row = c.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN apply_status='applied' THEN 1 ELSE 0 END) AS applied
            FROM jobs WHERE task_id = ?
        """, (task_id,)).fetchone()
        c.execute("UPDATE tasks SET total=?, applied=? WHERE task_id=?",
                  (row["total"] or 0, row["applied"] or 0, task_id))


async def refresh_task_counters(task_id: str):
    try:
        await asyncio.to_thread(_refresh_counters, task_id)
    except Exception as e:
        print(f"  [DB] refresh_task_counters 失败: {e}", flush=True)


def _list_tasks(limit: int):
    with _conn() as c:
        rows = c.execute("""
            SELECT task_id, status, params_json, started_at, finished_at,
                   error, total, applied
            FROM tasks
            ORDER BY started_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d.pop("params_json") or "{}")
        except Exception:
            d["params"] = {}
        out.append(d)
    return out


async def list_tasks(limit: int = 50) -> list[dict]:
    try:
        return await asyncio.to_thread(_list_tasks, limit)
    except Exception as e:
        print(f"  [DB] list_tasks 失败: {e}", flush=True)
        return []


def _get_task(task_id: str):
    with _conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["params"] = json.loads(d.pop("params_json") or "{}")
    except Exception:
        d["params"] = {}
    return d


async def get_task(task_id: str) -> Optional[dict]:
    try:
        return await asyncio.to_thread(_get_task, task_id)
    except Exception as e:
        print(f"  [DB] get_task 失败: {e}", flush=True)
        return None


def _delete_task(task_id: str):
    with _write_lock, _conn() as c:
        c.execute("DELETE FROM jobs WHERE task_id = ?", (task_id,))
        c.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))


async def delete_task(task_id: str):
    try:
        await asyncio.to_thread(_delete_task, task_id)
    except Exception as e:
        print(f"  [DB] delete_task 失败: {e}", flush=True)


def _purge_old_tasks(days: int = 7) -> int:
    """删除超过 days 天的任务及其职位（CASCADE）"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _write_lock, _conn() as c:
        rows = c.execute(
            "SELECT task_id FROM tasks WHERE started_at < ?", (cutoff,)
        ).fetchall()
        if not rows:
            return 0
        ids = [r["task_id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        c.execute(f"DELETE FROM jobs WHERE task_id IN ({placeholders})", ids)
        c.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", ids)
        return len(ids)


async def purge_old_tasks(days: int = 7) -> int:
    try:
        n = await asyncio.to_thread(_purge_old_tasks, days)
        if n > 0:
            print(f"  [DB] 清理 {n} 个超过 {days} 天的历史任务", flush=True)
        return n
    except Exception as e:
        print(f"  [DB] purge_old_tasks 失败: {e}", flush=True)
        return 0


def _delete_all_tasks() -> int:
    with _write_lock, _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        c.execute("DELETE FROM jobs")
        c.execute("DELETE FROM tasks")
    # VACUUM 回收磁盘空间（必须在事务外执行）
    with _write_lock:
        _conn().execute("VACUUM")
    return n


async def delete_all_tasks() -> int:
    try:
        n = await asyncio.to_thread(_delete_all_tasks)
        print(f"  [DB] 已清空全部历史: {n} 个任务", flush=True)
        return n
    except Exception as e:
        print(f"  [DB] delete_all_tasks 失败: {e}", flush=True)
        return 0


# ───────── 职位 ─────────

def _upsert_job(task_id: str, job: dict):
    platform = job.get("platform") or ""
    job_id = job.get("job_id") or ""
    if not platform or not job_id:
        return
    now = datetime.now().isoformat()
    with _write_lock, _conn() as c:
        c.execute("""
            INSERT INTO jobs (task_id, platform, job_id, job_title, company_name,
                              salary_range, salary_min, salary_max, city,
                              experience_required, education_required, job_url,
                              apply_status, apply_skip_reason, greeting_sent,
                              data_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id, platform, job_id) DO UPDATE SET
                job_title=excluded.job_title,
                company_name=excluded.company_name,
                salary_range=excluded.salary_range,
                salary_min=excluded.salary_min,
                salary_max=excluded.salary_max,
                city=excluded.city,
                experience_required=excluded.experience_required,
                education_required=excluded.education_required,
                job_url=excluded.job_url,
                apply_status=COALESCE(excluded.apply_status, jobs.apply_status),
                apply_skip_reason=COALESCE(excluded.apply_skip_reason, jobs.apply_skip_reason),
                greeting_sent=COALESCE(excluded.greeting_sent, jobs.greeting_sent),
                data_json=excluded.data_json,
                updated_at=excluded.updated_at
        """, (
            task_id, platform, job_id,
            job.get("job_title"), job.get("company_name"),
            job.get("salary_range"),
            _to_float(job.get("salary_min")), _to_float(job.get("salary_max")),
            job.get("city"),
            job.get("experience_required"), job.get("education_required"),
            job.get("job_url"),
            job.get("apply_status"), job.get("apply_skip_reason"),
            job.get("greeting_sent"),
            json.dumps(job, ensure_ascii=False, default=str),
            now, now,
        ))


async def upsert_job(task_id: str, job: dict):
    try:
        await asyncio.to_thread(_upsert_job, task_id, job)
    except Exception as e:
        print(f"  [DB] upsert_job 失败: {e}", flush=True)


def _get_task_jobs(task_id: str, offset: int, limit: int):
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE task_id = ?", (task_id,)
        ).fetchone()["n"]
        rows = c.execute("""
            SELECT data_json FROM jobs
            WHERE task_id = ?
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
        """, (task_id, limit, offset)).fetchall()
    jobs = []
    for r in rows:
        try:
            jobs.append(json.loads(r["data_json"]))
        except Exception:
            continue
    return jobs, total


async def get_task_jobs(task_id: str, offset: int = 0,
                         limit: int = 200) -> tuple:
    try:
        return await asyncio.to_thread(_get_task_jobs, task_id, offset, limit)
    except Exception as e:
        print(f"  [DB] get_task_jobs 失败: {e}", flush=True)
        return [], 0


def _get_recent_jobs(limit: int, only_applied: bool):
    where = "WHERE apply_status='applied'" if only_applied else ""
    with _conn() as c:
        rows = c.execute(f"""
            SELECT data_json FROM jobs {where}
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data_json"]))
        except Exception:
            continue
    return out


async def get_recent_jobs(limit: int = 500,
                          only_applied: bool = False) -> list[dict]:
    """跨任务聚合最近的职位 — 用于"全部历史投递"视图"""
    try:
        return await asyncio.to_thread(_get_recent_jobs, limit, only_applied)
    except Exception as e:
        print(f"  [DB] get_recent_jobs 失败: {e}", flush=True)
        return []


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

