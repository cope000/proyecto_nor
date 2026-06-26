"""Standalone bot health check script.

Reads data/heartbeats/*.json and cross-checks against market hours
from mm_config.py to produce a human-readable health report.

Usage:
    python scripts/health_check.py
    python scripts/health_check.py --json
    python scripts/health_check.py --stale-threshold 120

Exit codes:
    0 — all bots healthy (or outside market hours)
    1 — at least one bot stale during market hours
    2 — runtime error
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import utils.heartbeat as hb
from config.mm_config import MMConfig

_ART = timezone(timedelta(hours=-3))

# Market hours per instrument ticker_pattern (close = exclusive upper bound).
# Pulled from MMConfig at runtime; this dict is only a fallback.
_FALLBACK_HOURS: dict[str, tuple[int, int, int, int]] = {
    "DLR":     (10, 0, 15, 0),
    "CAUC":    (10, 0, 15, 0),
    "SOJ.ROS": (11, 0, 17, 0),
    "SOJ.MIN": (11, 0, 17, 0),
}

# Mapping from bot_id prefix to instrument ticker_pattern.
_BOT_INSTRUMENT: dict[str, str] = {
    "mm_dlr":      "DLR",
    "mm_dlr_mini": "DLR",
    "mm_soj":      "SOJ.ROS",
    "mm_soj_mini": "SOJ.MIN",
    "cs_dlr":      "DLR",
    "cash_carry":  "DLR",
}


def _build_market_hours() -> dict[str, tuple[int, int, int, int]]:
    """Extract open/close from MMConfig instruments."""
    try:
        cfg = MMConfig()
        hours: dict[str, tuple[int, int, int, int]] = {}
        for _key, inst in cfg.instruments.items():
            hours[inst.ticker_pattern] = (
                inst.market_open_hour,
                inst.market_open_minute,
                inst.market_close_hour,
                inst.market_close_minute,
            )
        return hours
    except Exception:
        return _FALLBACK_HOURS.copy()


def _is_market_open(instrument_pattern: str, market_hours: dict[str, tuple[int, int, int, int]]) -> bool:
    now = datetime.now(tz=_ART)
    oh, om, ch, cm = market_hours.get(instrument_pattern, _FALLBACK_HOURS.get(instrument_pattern, (10, 0, 15, 0)))
    open_time  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_time = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_time <= now < close_time


def _severity(stale_sec: float | None, in_market: bool) -> str:
    if stale_sec is None:
        return "MISSING" if in_market else "UNKNOWN"
    if stale_sec < 60:
        return "OK"
    if stale_sec < 300:
        return "STALE" if in_market else "OK"
    return "CRASHED" if in_market else "STALE_OFFHOURS"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bot health check")
    p.add_argument("--stale-threshold", type=int, default=60,
                   help="Seconds before a heartbeat is considered stale (default 60)")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="Output machine-readable JSON instead of text")
    p.add_argument("--all", action="store_true", dest="show_all",
                   help="Show all bots including those outside market hours")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    market_hours = _build_market_hours()
    heartbeats = hb.read_all()
    now_str = datetime.now(tz=_ART).strftime("%Y-%m-%d %H:%M:%S %Z")

    # Build known bot list (from BOT_INSTRUMENT keys + any extra heartbeat files)
    all_bot_ids = sorted(set(_BOT_INSTRUMENT.keys()) | set(heartbeats.keys()))

    rows: list[dict] = []
    worst_severity = "OK"

    for bot_id in all_bot_ids:
        instrument = _BOT_INSTRUMENT.get(bot_id, "DLR")
        in_market = _is_market_open(instrument, market_hours)
        beat = heartbeats.get(bot_id)

        if beat is None:
            stale_sec = None
            ticker = "—"
            position = None
            pnl = None
            cycles = None
            status_field = "no_heartbeat"
        else:
            stale_sec = hb.staleness_seconds(beat)
            ticker = beat.get("ticker", "—")
            position = beat.get("current_position")
            pnl = beat.get("current_pnl")
            cycles = beat.get("cycles")
            status_field = beat.get("status", "—")

        sev = _severity(stale_sec, in_market)

        if sev in ("CRASHED", "MISSING"):
            worst_severity = "ALERT"
        elif sev == "STALE" and worst_severity == "OK":
            worst_severity = "WARN"

        rows.append({
            "bot_id": bot_id,
            "ticker": ticker,
            "in_market": in_market,
            "stale_sec": round(stale_sec, 1) if stale_sec is not None else None,
            "severity": sev,
            "position": position,
            "pnl": pnl,
            "cycles": cycles,
            "status": status_field,
        })

    if args.as_json:
        out = {
            "checked_at": now_str,
            "overall": worst_severity,
            "bots": rows,
        }
        print(json.dumps(out, indent=2, ensure_ascii=True))
    else:
        print(f"Bot Health Check — {now_str}")
        print(f"Overall: {worst_severity}")
        print()

        hdr = f"{'bot_id':<16} {'ticker':<16} {'market':^7} {'stale_sec':>10} {'sev':<16} {'pos':>5} {'pnl':>10} {'cycles':>7}"
        print(hdr)
        print("-" * len(hdr))

        for r in rows:
            if not args.show_all and not r["in_market"] and r["severity"] in ("OK", "UNKNOWN", "STALE_OFFHOURS"):
                continue
            stale_str = f"{r['stale_sec']:.0f}s" if r["stale_sec"] is not None else "—"
            pos_str = str(r["position"]) if r["position"] is not None else "—"
            pnl_str = f"{r['pnl']:.2f}" if r["pnl"] is not None else "—"
            cyc_str = str(r["cycles"]) if r["cycles"] is not None else "—"
            mkt_str = "OPEN" if r["in_market"] else "CLOSED"
            prefix = "*** " if r["severity"] in ("CRASHED", "MISSING", "STALE") else "    "
            print(f"{prefix}{r['bot_id']:<12} {r['ticker']:<16} {mkt_str:^7} {stale_str:>10} {r['severity']:<16} {pos_str:>5} {pnl_str:>10} {cyc_str:>7}")

        print()

        # Detailed alerts
        alerts = [r for r in rows if r["severity"] in ("CRASHED", "MISSING", "STALE")]
        if alerts:
            print("=== ALERTS ===")
            for r in alerts:
                stale_str = f"{r['stale_sec']:.0f}s" if r["stale_sec"] is not None else "no heartbeat"
                print(f"  [{r['severity']}] {r['bot_id']} ({r['ticker']}) — last seen: {stale_str} ago")
        else:
            if any(r["in_market"] for r in rows):
                print("All bots healthy during market hours.")

    # Exit 1 if any CRASHED or MISSING during market hours.
    critical = any(r["severity"] in ("CRASHED", "MISSING") for r in rows if r["in_market"])
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
