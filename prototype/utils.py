"""通用小工具（时间 / ID / 数值转换 / 热点键）。"""
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))

def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"

def _hotkey(title) -> str:
    """热点去重键：忽略空白与大小写，同一议题只留一条。"""
    return re.sub(r"\s+", "", str(title or "")).lower()

def _step_view(row):
    """车间工位记录 → 前端视图（detail JSON 解包）。"""
    step = dict(row)
    try:
        step["detail"] = json.loads(step.get("detail") or "null")
    except ValueError:
        step["detail"] = None
    return step

def _iso_shift(value, seconds):
    """ISO 时间字符串平移 n 秒（工位时间窗放宽用）；非法输入原样返回。"""
    try:
        return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return value

def _to_int(value, default):
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default

def _num(v):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            continue
    return 0
