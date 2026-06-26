"""Bot heartbeat writer — writes a small JSON file per bot each cycle.

Designed to be called from within the MM loop. All failures are swallowed
so a disk error never kills the trading loop.

Schema (data/heartbeats/{bot_id}.json):
{
    "bot_id":          "mm_dlr",
    "ticker":          "DLR/JUN26",
    "last_cycle_ts":   "2026-06-24T11:42:33.123456-03:00",
    "last_fill_ts":    null,           # ISO string or null
    "current_position": 0,
    "current_pnl":     0.0,
    "status":          "running",      # running | warming_up | stopped
    "fill_buy_qty":    0,
    "fill_sell_qty":   0,
    "cycles":          42
}
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_ART = timezone(timedelta(hours=-3))

# Resolved once at import; safe even if CWD changes inside the process.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HEARTBEAT_DIR = _PROJECT_ROOT / "data" / "heartbeats"

# Write heartbeat at most once every THROTTLE_SECONDS even if called more often.
_THROTTLE_SECONDS: float = 10.0

# Module-level cache: bot_id -> last written monotonic timestamp
_last_write: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(tz=_ART).isoformat(timespec="milliseconds")


def write(
    bot_id: str,
    ticker: str,
    position: int,
    pnl: float,
    fill_buy_qty: int,
    fill_sell_qty: int,
    cycles: int,
    status: str = "running",
    last_fill_ts: str | None = None,
) -> None:
    """Write heartbeat JSON atomically. Swallows all exceptions."""
    import time as _time

    mono_now = _time.monotonic()
    if mono_now - _last_write.get(bot_id, 0.0) < _THROTTLE_SECONDS:
        return

    try:
        _HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "bot_id": bot_id,
            "ticker": ticker,
            "last_cycle_ts": _now_iso(),
            "last_fill_ts": last_fill_ts,
            "current_position": int(position),
            "current_pnl": round(float(pnl), 4),
            "status": str(status),
            "fill_buy_qty": int(fill_buy_qty),
            "fill_sell_qty": int(fill_sell_qty),
            "cycles": int(cycles),
        }
        target = _HEARTBEAT_DIR / f"{bot_id}.json"
        # Atomic write: write to temp then rename to avoid partial reads.
        fd, tmp_path = tempfile.mkstemp(dir=_HEARTBEAT_DIR, prefix=f".{bot_id}_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        _last_write[bot_id] = mono_now
    except Exception:
        # Never crash the caller (trading loop).
        pass


def read(bot_id: str) -> dict[str, Any] | None:
    """Read the latest heartbeat for a bot. Returns None if missing or corrupt."""
    target = _HEARTBEAT_DIR / f"{bot_id}.json"
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def read_all() -> dict[str, dict[str, Any]]:
    """Read all available heartbeat files. Skips missing or corrupt ones."""
    result: dict[str, dict[str, Any]] = {}
    if not _HEARTBEAT_DIR.exists():
        return result
    for path in _HEARTBEAT_DIR.glob("*.json"):
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "bot_id" in data:
                result[data["bot_id"]] = data
        except Exception:
            continue
    return result


def staleness_seconds(heartbeat: dict[str, Any]) -> float | None:
    """Returns seconds since last_cycle_ts, or None if unparseable."""
    raw_ts = heartbeat.get("last_cycle_ts")
    if not raw_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(raw_ts))
        now = datetime.now(tz=_ART)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ART)
        return max(0.0, (now - ts).total_seconds())
    except Exception:
        return None
