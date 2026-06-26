"""Analyze simulation outputs (sim_run logs, fills CSVs, reconciliation log)."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LOG_FILE_RE = re.compile(r"^sim_run_(?P<bot_safe>.+)_(?P<ts>\d{8}_\d{6})\.log$")

# Supports two observed timestamp formats:
# 1) [2026-04-27 09:43:55.728] ...
# 2) 2026-05-05 00:01:02 | INFO | ...
_LINE_TS_RE = re.compile(
    r"^\[?(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[\.,]\d+)?\]?"
)

_TOTAL_FILLS_RE = re.compile(r"Total fills:\s*(-?\d+)", re.IGNORECASE)
_REALIZED_PNL_RE = re.compile(r"Realized PnL:\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
_MAX_DRAWDOWN_RE = re.compile(r"Max drawdown:\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
_FINAL_POSITION_RE = re.compile(r"Final position:\s*(-?\d+)", re.IGNORECASE)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze sim_run logs + fills CSVs + reconciliation output."
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default="",
        help="Timestamp group in YYYYMMDD_HHMMSS (default: auto-detect latest with sim_run files)",
    )
    parser.add_argument(
        "--session-date",
        type=str,
        default="",
        help="Session date YYYYMMDD for fills CSVs (default: inferred from timestamp)",
    )
    parser.add_argument(
        "--logs-dir",
        type=str,
        default="logs",
        help="Directory containing sim_run and reconciliation logs (default: logs)",
    )
    parser.add_argument(
        "--fills-dir",
        type=str,
        default="logs/fills_sim",
        help="Directory containing sim fills CSV files (default: logs/fills_sim)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON output to logs/sim_analysis_<timestamp>.json",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default="",
        help="Optional explicit JSON output path",
    )
    return parser.parse_args()


def _to_abs(path_value: str) -> Path:
    p = Path(path_value)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _safe_to_bot_name(bot_safe: str) -> str:
    mapping = {
        "DLR-MAY26": "DLR/MAY26",
        "DLR-MAY26M": "DLR/MAY26M",
        "SOJMIN-JUL26": "SOJ.MIN/JUL26",
        "SOJROS-JUL26": "SOJ.ROS/JUL26",
    }
    if bot_safe in mapping:
        return mapping[bot_safe]

    # Generic fallback: first '-' becomes '/', and SOJMIN/SOJROS recover dotted names.
    candidate = bot_safe
    if candidate.startswith("SOJMIN-"):
        candidate = candidate.replace("SOJMIN-", "SOJ.MIN-", 1)
    if candidate.startswith("SOJROS-"):
        candidate = candidate.replace("SOJROS-", "SOJ.ROS-", 1)
    if "-" in candidate:
        left, right = candidate.split("-", 1)
        return f"{left}/{right}"
    return candidate


def _bot_to_csv_stem(bot_name: str) -> str:
    return bot_name.replace("/", "-")


def _pick_timestamp(logs_dir: Path, explicit_ts: str) -> tuple[str, list[Path]]:
    if not logs_dir.exists():
        raise FileNotFoundError(f"logs directory not found: {logs_dir}")

    groups: dict[str, list[Path]] = {}
    for p in logs_dir.glob("sim_run_*_*.log"):
        m = _LOG_FILE_RE.match(p.name)
        if not m:
            continue
        ts = m.group("ts")
        groups.setdefault(ts, []).append(p)

    if not groups:
        raise FileNotFoundError(f"No sim_run_*_*.log files found in {logs_dir}")

    if explicit_ts:
        selected = explicit_ts
        files = sorted(groups.get(selected, []), key=lambda x: x.name)
        if not files:
            raise FileNotFoundError(
                f"No sim_run logs found for timestamp {selected} in {logs_dir}"
            )
        return selected, files

    selected = sorted(groups.keys())[-1]
    files = sorted(groups[selected], key=lambda x: x.name)
    return selected, files


def _parse_line_ts(line: str) -> datetime | None:
    m = _LINE_TS_RE.match(line.strip())
    if not m:
        return None
    raw = m.group("ts")
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


@dataclass
class BotRow:
    bot: str
    fills_csv: int | None
    fills_summary: int | None
    match_csv_summary: str
    realized_pnl: float | None
    max_drawdown_intraday: float | None
    final_position: int | None
    cb_events_count: int
    fill_validation_skipped: str
    eod_triggered: str
    session_duration_sec: int | None


@dataclass
class EventRow:
    bot: str
    timestamp: str
    detail: str


def _parse_sim_log(log_path: Path, bot_name: str) -> tuple[dict[str, Any], list[EventRow], list[EventRow]]:
    fills_summary: int | None = None
    realized_pnl: float | None = None
    max_drawdown: float | None = None
    final_position: int | None = None
    cb_events: list[EventRow] = []
    unexpected_lines: list[EventRow] = []

    fill_validation_state = "missing"
    eod_triggered = "no"

    first_ts: datetime | None = None
    last_ts: datetime | None = None

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            maybe_ts = _parse_line_ts(line)
            ts_str = maybe_ts.strftime("%Y-%m-%d %H:%M:%S") if maybe_ts else ""
            if maybe_ts is not None:
                if first_ts is None:
                    first_ts = maybe_ts
                last_ts = maybe_ts

            m_total = _TOTAL_FILLS_RE.search(line)
            if m_total:
                fills_summary = int(m_total.group(1))

            m_pnl = _REALIZED_PNL_RE.search(line)
            if m_pnl:
                realized_pnl = float(m_pnl.group(1))

            m_dd = _MAX_DRAWDOWN_RE.search(line)
            if m_dd:
                max_drawdown = float(m_dd.group(1))

            m_pos = _FINAL_POSITION_RE.search(line)
            if m_pos:
                final_position = int(m_pos.group(1))

            upper_line = line.upper()

            if "[FILL_VALIDATION]" in upper_line:
                if "SKIPPED" in upper_line and "SIM MODE" in upper_line:
                    fill_validation_state = "yes"
                else:
                    fill_validation_state = "no"

            if "[EOD_TWAP]" in upper_line or ("EOD" in upper_line and "TWAP" in upper_line):
                eod_triggered = "yes"

            if "[CIRCUIT_BREAKER]" in upper_line:
                cb_events.append(EventRow(bot=bot_name, timestamp=ts_str, detail=line))

            if "WARNING" in upper_line or "ERROR" in upper_line:
                # Keep as "unexpected" candidates for manual review.
                unexpected_lines.append(EventRow(bot=bot_name, timestamp=ts_str, detail=line))

    duration_sec: int | None = None
    if first_ts is not None and last_ts is not None:
        delta = int((last_ts - first_ts).total_seconds())
        duration_sec = max(0, delta)

    parsed = {
        "fills_summary": fills_summary,
        "realized_pnl": realized_pnl,
        "max_drawdown_intraday": max_drawdown,
        "final_position": final_position,
        "cb_events_count": len(cb_events),
        "fill_validation_skipped": fill_validation_state,
        "eod_triggered": eod_triggered,
        "session_duration_sec": duration_sec,
    }
    return parsed, cb_events, unexpected_lines


def _count_csv_fills(csv_path: Path) -> int | None:
    if not csv_path.exists():
        return None

    count = 0
    with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            count += 1
    return count


def _build_reconciliation_summary(recon_path: Path | None) -> dict[str, Any]:
    if recon_path is None:
        return {
            "path": "",
            "result": "MISSING",
            "key_lines": ["No reconciliation log found."],
        }

    key_lines: list[str] = []
    result = "UNKNOWN"

    with recon_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            upper = line.upper()
            if line.startswith("session_date="):
                key_lines.append(line)
            elif line.startswith("CSV files matched"):
                key_lines.append(line)
            elif line.startswith("total_symbols="):
                key_lines.append(line)
            elif line.startswith("drift_symbols="):
                key_lines.append(line)
            elif line.startswith("RESULT:"):
                key_lines.append(line)
                result = line.replace("RESULT:", "", 1).strip() or "UNKNOWN"
            elif line.startswith("[SIM MODE]"):
                key_lines.append(line)

    if not key_lines:
        key_lines.append("Reconciliation log read but no known summary lines found.")

    return {
        "path": str(recon_path),
        "result": result,
        "key_lines": key_lines,
    }


def _find_reconciliation_log(logs_dir: Path, timestamp: str) -> Path | None:
    preferred = logs_dir / f"sim_reconciliation_{timestamp}.log"
    if preferred.exists():
        return preferred

    candidates = sorted(logs_dir.glob("sim_reconciliation_*.log"), key=lambda p: p.name)
    if not candidates:
        return None
    return candidates[-1]


def _fmt_num(value: Any) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _print_table(rows: list[BotRow]) -> None:
    headers = [
        "bot",
        "fills_csv",
        "fills_summary",
        "match_csv_summary",
        "realized_pnl",
        "max_drawdown_intraday",
        "final_position",
        "cb_events_count",
        "fill_validation_skipped",
        "eod_triggered",
        "session_duration_sec",
    ]

    matrix: list[list[str]] = []
    for r in rows:
        matrix.append(
            [
                r.bot,
                _fmt_num(r.fills_csv),
                _fmt_num(r.fills_summary),
                r.match_csv_summary,
                _fmt_num(r.realized_pnl),
                _fmt_num(r.max_drawdown_intraday),
                _fmt_num(r.final_position),
                _fmt_num(r.cb_events_count),
                r.fill_validation_skipped,
                r.eod_triggered,
                _fmt_num(r.session_duration_sec),
            ]
        )

    widths = [len(h) for h in headers]
    for row in matrix:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def render_line(values: list[str]) -> str:
        return " | ".join(v.ljust(widths[i]) for i, v in enumerate(values))

    sep = "-+-".join("-" * w for w in widths)
    print(render_line(headers))
    print(sep)
    for row in matrix:
        print(render_line(row))


def main() -> int:
    args = _parse_args()

    logs_dir = _to_abs(args.logs_dir)
    fills_dir = _to_abs(args.fills_dir)

    try:
        timestamp, sim_logs = _pick_timestamp(logs_dir, args.timestamp.strip())
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 2

    session_date = args.session_date.strip() or timestamp.split("_", 1)[0]

    rows: list[BotRow] = []
    cb_event_lines: list[EventRow] = []
    unexpected_lines: list[EventRow] = []

    for log_path in sim_logs:
        m = _LOG_FILE_RE.match(log_path.name)
        if not m:
            continue

        bot_safe = m.group("bot_safe")
        bot_name = _safe_to_bot_name(bot_safe)

        parsed, cb_events, warns_errs = _parse_sim_log(log_path, bot_name)
        cb_event_lines.extend(cb_events)
        unexpected_lines.extend(warns_errs)

        csv_stem = _bot_to_csv_stem(bot_name)
        csv_path = fills_dir / f"{csv_stem}_{session_date}.csv"
        fills_csv = _count_csv_fills(csv_path)

        fills_summary = parsed["fills_summary"]
        if fills_csv is None or fills_summary is None:
            match = "missing"
        else:
            match = "yes" if fills_csv == fills_summary else "no"

        rows.append(
            BotRow(
                bot=bot_name,
                fills_csv=fills_csv,
                fills_summary=fills_summary,
                match_csv_summary=match,
                realized_pnl=parsed["realized_pnl"],
                max_drawdown_intraday=parsed["max_drawdown_intraday"],
                final_position=parsed["final_position"],
                cb_events_count=parsed["cb_events_count"],
                fill_validation_skipped=parsed["fill_validation_skipped"],
                eod_triggered=parsed["eod_triggered"],
                session_duration_sec=parsed["session_duration_sec"],
            )
        )

    rows.sort(key=lambda r: r.bot)

    print(f"analysis_timestamp={timestamp} session_date={session_date}")
    print(f"sim_logs_found={len(sim_logs)} fills_dir={fills_dir}")
    print()
    _print_table(rows)

    print("\n[CIRCUIT_BREAKER] details:")
    if not cb_event_lines:
        print("- none")
    else:
        for ev in cb_event_lines:
            ts = ev.timestamp or "NA"
            print(f"- bot={ev.bot} ts={ts} reason={ev.detail}")

    recon_path = _find_reconciliation_log(logs_dir, timestamp)
    recon_summary = _build_reconciliation_summary(recon_path)

    print("\nReconciliation summary:")
    print(f"- path={recon_summary['path'] or 'MISSING'}")
    print(f"- result={recon_summary['result']}")
    for line in recon_summary["key_lines"]:
        print(f"- {line}")

    print("\nUnexpected WARNING/ERROR lines:")
    if not unexpected_lines:
        print("- none")
    else:
        for ev in unexpected_lines:
            ts = ev.timestamp or "NA"
            print(f"- bot={ev.bot} ts={ts} line={ev.detail}")

    if args.json or args.json_out.strip():
        if args.json_out.strip():
            json_path = _to_abs(args.json_out.strip())
        else:
            json_path = logs_dir / f"sim_analysis_{timestamp}.json"

        payload = {
            "analysis_timestamp": timestamp,
            "session_date": session_date,
            "sim_logs": [str(p) for p in sim_logs],
            "rows": [asdict(r) for r in rows],
            "circuit_breaker_events": [asdict(x) for x in cb_event_lines],
            "reconciliation": recon_summary,
            "unexpected_warning_error_lines": [asdict(x) for x in unexpected_lines],
        }

        json_path.parent.mkdir(parents=True, exist_ok=True)
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=True)
        print(f"\nJSON written: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
