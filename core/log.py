"""Lightweight structured logging — replace scattered print() calls.

Usage:
    from core.log import get_logger
    log = get_logger("scrapling")
    log.info("started", url=url, attempt=1)
    log.error("page.goto failed", url=url, error=type(e).__name__, detail=str(e))
"""
import json
import sys
import time
from typing import Any

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_MIN_LEVEL = _LEVELS["info"]


class StructLogger:
    def __init__(self, name: str):
        self.name = name

    def _emit(self, level: str, msg: str, **kwargs: Any) -> None:
        if _LEVELS[level] < _MIN_LEVEL:
            return
        rec = {"t": time.strftime("%H:%M:%S"), "lvl": level, "logger": self.name, "msg": msg}
        if kwargs:
            rec.update(kwargs)
        try:
            line = json.dumps(rec, ensure_ascii=False)
        except Exception:
            line = f"[{rec['t']}] {level} {self.name} {msg} {kwargs}"
        print(line, file=sys.stderr, flush=True)

    def debug(self, msg: str, **kw): self._emit("debug", msg, **kw)
    def info(self, msg: str, **kw): self._emit("info", msg, **kw)
    def warn(self, msg: str, **kw): self._emit("warn", msg, **kw)
    def error(self, msg: str, **kw): self._emit("error", msg, **kw)


def get_logger(name: str) -> StructLogger:
    return StructLogger(name)


def set_level(level: str) -> None:
    global _MIN_LEVEL
    _MIN_LEVEL = _LEVELS.get(level.lower(), _MIN_LEVEL)
