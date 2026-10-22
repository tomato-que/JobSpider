"""简历摘要的本地持久化。

只保存提取后的打码文本与 LLM 摘要，不保存原始文件。
路径：data/resume.json（data/ 已在 .gitignore 中）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import ROOT_DIR
from core.log import get_logger
from core.output import _atomic_write

log = get_logger("resume_store")

RESUME_PATH = ROOT_DIR / "data" / "resume.json"
MAX_PROFILE_TEXT = 4000
MAX_RAW_FALLBACK = 1500

_LOCK = threading.RLock()
_cache_mtime: Optional[float] = None
_cache_data: Optional[dict] = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_unlocked() -> Optional[dict]:
    global _cache_mtime, _cache_data
    if not RESUME_PATH.exists():
        _cache_mtime = None
        _cache_data = None
        return None
    try:
        mtime = RESUME_PATH.stat().st_mtime
    except OSError:
        return None
    if _cache_data is not None and _cache_mtime == mtime:
        return _cache_data
    try:
        with open(RESUME_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("resume.json 不是对象")
    except Exception as e:
        log.error("resume load failed",
                  error=type(e).__name__, detail=str(e))
        return None
    _cache_mtime = mtime
    _cache_data = data
    return data


def _write_unlocked(data: dict) -> None:
    global _cache_mtime, _cache_data
    RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(RESUME_PATH,
                  json.dumps(data, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    try:
        _cache_mtime = RESUME_PATH.stat().st_mtime
    except OSError:
        _cache_mtime = None
    _cache_data = data


def save_resume(filename: str, raw_text: str, profile: Optional[dict],
                profile_text: str, source: str) -> dict:
    """保存一次新上传（保留首次上传时间）。"""
    with _LOCK:
        existing = _load_unlocked() or {}
        now = _now()
        data = {
            "filename": Path(filename or "resume").name,
            "uploaded_at": existing.get("uploaded_at") or now,
            "updated_at": now,
            "chars": len(raw_text or ""),
            "raw_text": raw_text or "",
            "profile": profile or {},
            "profile_text": (profile_text or "")[:MAX_PROFILE_TEXT],
            "profile_source": source,
        }
        _write_unlocked(data)
        return dict(data)


def load_resume() -> Optional[dict]:
    with _LOCK:
        data = _load_unlocked()
        return dict(data) if data else None


def get_profile_text() -> str:
    """给招呼语 prompt 用的紧凑摘要；没有摘要时回退原文摘录。"""
    with _LOCK:
        data = _load_unlocked()
        if not data:
            return ""
        text = (data.get("profile_text") or "").strip()
        if text:
            return text[:MAX_PROFILE_TEXT]
        return (data.get("raw_text") or "")[:MAX_RAW_FALLBACK]


def get_resume_version() -> str:
    """摘要内容指纹，用于招呼语缓存 key。"""
    text = get_profile_text()
    if not text:
        return ""
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def update_profile_text(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("简历摘要不能为空")
    if len(text) > MAX_PROFILE_TEXT:
        raise ValueError(f"简历摘要不能超过 {MAX_PROFILE_TEXT} 字")
    with _LOCK:
        data = _load_unlocked()
        if not data:
            raise FileNotFoundError("还没有上传简历")
        data = dict(data)
        data["profile_text"] = text
        data["profile_source"] = "manual"
        data["updated_at"] = _now()
        _write_unlocked(data)
        return dict(data)


def delete_resume() -> bool:
    global _cache_mtime, _cache_data
    with _LOCK:
        existed = RESUME_PATH.exists()
        if existed:
            try:
                RESUME_PATH.unlink()
            except OSError as e:
                log.error("resume delete failed",
                          error=type(e).__name__, detail=str(e))
                raise
        _cache_mtime = None
        _cache_data = None
        return existed


def build_profile_text(profile: Optional[dict], raw_text: str) -> str:
    """把 LLM 结构化结果格式化成紧凑摘要；无结果时回退原文摘录。"""
    p = profile if isinstance(profile, dict) else {}
    lines: list[str] = []

    name = str(p.get("name") or "").strip()
    if name:
        lines.append(f"姓名：{name[:30]}")

    years = p.get("years")
    if isinstance(years, (int, float)) and years >= 0:
        lines.append(f"工作年限：{int(years)}年")
    elif isinstance(years, str) and years.strip():
        lines.append(f"工作年限：{years.strip()[:30]}")

    role = str(p.get("current_role") or "").strip()
    if role:
        lines.append(f"当前/最近职位：{role[:60]}")

    skills = p.get("skills")
    if isinstance(skills, list):
        cleaned = [str(s).strip() for s in skills if str(s).strip()]
        if cleaned:
            lines.append("核心技能：" + "、".join(cleaned[:15]))

    highlights = p.get("highlights")
    if isinstance(highlights, list):
        cleaned = [str(s).strip() for s in highlights if str(s).strip()]
        if cleaned:
            lines.append("代表亮点：")
            lines.extend(f"- {s[:80]}" for s in cleaned[:5])

    education = str(p.get("education") or "").strip()
    if education:
        lines.append(f"教育背景：{education[:80]}")

    preferred = p.get("preferred_roles")
    if isinstance(preferred, list):
        cleaned = [str(s).strip() for s in preferred if str(s).strip()]
        if cleaned:
            lines.append("求职方向：" + "、".join(cleaned[:8]))

    if lines:
        return "\n".join(lines)[:MAX_PROFILE_TEXT]

    fallback = (raw_text or "").strip()[:MAX_RAW_FALLBACK]
    if not fallback:
        return ""
    return ("简历原文摘录（已打码）：\n" + fallback)[:MAX_PROFILE_TEXT]
