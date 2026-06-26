"""Read-only reconciliation of broker positions vs positions reconstructed from fills CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path for absolute imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import pyRofex

from core import credentials as config
from core.connect import connect


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare broker positions vs positions reconstructed from logs/fills CSV files."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now().strftime("%Y%m%d"),
        help="Session date in YYYYMMDD (default: today)",
    )
    parser.add_argument(
        "--fills-dir",
        type=str,
        default="logs/fills",
        help="Directory containing fill CSV files (default: logs/fills)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra diagnostics",
    )
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Sim mode: skip broker API, read from logs/fills_sim/ instead of logs/fills/",
    )
    return parser.parse_args()


def _normalize_symbol(raw: str) -> str:
    return str(raw or "").strip().upper()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_broker_positions(resp: dict[str, Any], verbose: bool = False) -> dict[str, int]:
    positions_raw = resp.get("positions")
    if not isinstance(positions_raw, list):
        raise ValueError("Broker response missing 'positions' list")

    out: dict[str, int] = {}
    for item in positions_raw:
        if not isinstance(item, dict):
            continue

        instrument = item.get("instrumentId")
        symbol = ""
        if isinstance(instrument, dict):
            symbol = str(instrument.get("symbol") or instrument.get("ticker") or "")
        if not symbol:
            symbol = str(item.get("symbol") or item.get("ticker") or "")

        symbol = _normalize_symbol(symbol)
        if not symbol:
            continue

        pos = _safe_int(
            item.get("position", item.get("netPosition", item.get("net", item.get("qty", 0))))
        )
        out[symbol] = out.get(symbol, 0) + pos

    if verbose:
        print(f"Broker positions parsed: {len(out)} symbols")
    return out


def _csv_positions_from_fills(fills_dir: Path, session_date: str, verbose: bool = False) -> dict[str, int]:
    out: dict[str, int] = {}
    pattern = f"*_{session_date}.csv"

    files = sorted(fills_dir.glob(pattern))
    if verbose:
        print(f"CSV files matched in {fills_dir}: {len(files)}")

    for csv_path in files:
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=",")
                for row in reader:
                    symbol = _normalize_symbol(row.get("ticker", ""))
                    if not symbol:
                        continue
                    side = str(row.get("side", "")).strip().upper()
                    qty = _safe_int(row.get("qty", 0))
                    if qty <= 0:
                        continue
                    if side == "BUY":
                        out[symbol] = out.get(symbol, 0) + qty
                    elif side == "SELL":
                        out[symbol] = out.get(symbol, 0) - qty
        except Exception as exc:
            if verbose:
                print(f"Warning: could not read {csv_path}: {exc}")

    return out


def _print_comparison_table(broker: dict[str, int], csv_pos: dict[str, int]) -> int:
    symbols = sorted(set(broker.keys()) | set(csv_pos.keys()))
    if not symbols:
        print("No symbols found in broker response or CSV fills for requested date.")
        return 0

    print("symbol                    broker_pos   csv_pos   drift")
    print("-------------------------------------------------------")

    drift_count = 0
    for sym in symbols:
        b = broker.get(sym, 0)
        c = csv_pos.get(sym, 0)
        d = b - c
        if d != 0:
            drift_count += 1
        print(f"{sym:<24} {b:>10} {c:>9} {d:>7}")

    print("-------------------------------------------------------")
    print(f"drift_symbols={drift_count} total_symbols={len(symbols)}")
    return drift_count


def main() -> int:
    args = _parse_args()

    fills_dir = Path(args.fills_dir)
    # In sim mode, default fills dir switches to logs/fills_sim/ unless overridden explicitly
    if args.sim and args.fills_dir == "logs/fills":
        fills_dir = Path("logs/fills_sim")
    if not fills_dir.is_absolute():
        fills_dir = _PROJECT_ROOT / fills_dir

    if args.sim:
        print("[SIM MODE] Skipping broker connection — CSV-only reconciliation")
        csv_positions = _csv_positions_from_fills(fills_dir, args.date, verbose=args.verbose)
        symbols = sorted(csv_positions.keys())
        if not symbols:
            print(f"No fills found in {fills_dir} for date {args.date}")
            return 0
        print(f"session_date={args.date}  fills_dir={fills_dir}")
        print("symbol                    csv_pos")
        print("---------------------------------")
        for sym in symbols:
            print(f"{sym:<24} {csv_positions[sym]:>9}")
        print("---------------------------------")
        print(f"total_symbols={len(symbols)}")
        print("RESULT: SIM_OK (no broker comparison)")
        return 0

    if not connect():
        print("ERROR: Could not initialize broker connection")
        return 2

    try:
        resp = pyRofex.get_account_position(account=config.ACCOUNT)
    except Exception as exc:
        print(f"ERROR: Broker API call failed: {exc}")
        return 2

    status_upper = str((resp or {}).get("status", "")).upper()
    if status_upper and status_upper != "OK":
        print(f"ERROR: Broker API status not OK: {status_upper}")
        if args.verbose:
            print(resp)
        return 2

    try:
        broker_positions = _extract_broker_positions(resp or {}, verbose=args.verbose)
    except Exception as exc:
        print(f"ERROR: Invalid broker response format: {exc}")
        if args.verbose:
            print(resp)
        return 2

    csv_positions = _csv_positions_from_fills(fills_dir, args.date, verbose=args.verbose)

    print(f"session_date={args.date} account={config.ACCOUNT}")
    drift_count = _print_comparison_table(broker_positions, csv_positions)

    if drift_count > 0:
        print("RESULT: DRIFT_FOUND")
        return 1

    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
