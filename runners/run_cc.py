"""Run Cash & Carry scanner on reMarkets (read-only by default)."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.cc_config import CCConfig
from core.connect import connect
from core.instruments import get_futures_dollar
from core.market_data import get_snapshot
from strategies.cash_carry import CashCarryStrategy
from strategies.implied_rate import ImpliedRateCalculator
from core.utils import setup_logger

logger = setup_logger("run_cc")


def _format_px(v: float | None) -> str:
    """Formats optional price for table output."""
    if v is None:
        return "---"
    return f"{v:.2f}"


def _get_spot_price() -> tuple[str, float | None]:
    """Gets spot proxy price from DLR/SPOT snapshot, fallback None."""
    spot_ticker = "DLR/SPOT"
    snap = get_snapshot(spot_ticker)
    if not snap:
        return spot_ticker, None
    spot = snap.get("last") or snap.get("bid_price") or snap.get("ask_price")
    return spot_ticker, float(spot) if spot else None


def _print_curve(rates: list[dict[str, Any]], config: CCConfig) -> None:
    """Prints implied-rate curve table."""
    print("=" * 76)
    print(f"CURVA DE TASAS IMPLICITAS DLR - {date.today().isoformat()}")
    print("=" * 76)
    print("Ticker      | Dias | Bid     | Ask     | Last    | TNA Impl | Basis   | vs Ref")
    for r in rates:
        implied_last = r.get("implied_rate_last")
        if implied_last is None:
            tna_txt = "  ---  "
            vs_ref = "  ---  "
        else:
            tna_txt = f"{implied_last:6.2f}%"
            vs_ref = f"{(implied_last - config.REFERENCE_RATE_TNA):+6.2f}%"

        print(
            f"{r['ticker']:<11} | {int(r['days']):>4} | {_format_px(r.get('bid')):>7} | {_format_px(r.get('ask')):>7} | "
            f"{_format_px(r.get('last')):>7} | {tna_txt:>7} | {r.get('basis', 0.0):>7.2f} | {vs_ref:>7}"
        )
    print("=" * 76)
    print(f"Tasa referencia: {config.REFERENCE_RATE_TNA:.1f}% TNA | Umbral: {config.MIN_SPREAD_BPS}bps")


def _parse_args() -> argparse.Namespace:
    """Parses CLI args."""
    parser = argparse.ArgumentParser(description="Cash & Carry scanner")
    parser.add_argument("--cycles", type=int, default=1, help="Number of scan cycles (0 = infinite)")
    return parser.parse_args()


def run_cash_carry(config: CCConfig, cycles: int) -> None:
    """Runs scan loop for implied-rate opportunities."""
    if not connect():
        raise RuntimeError("No se pudo conectar a reMarkets")

    rate_calc = ImpliedRateCalculator()
    strat = CashCarryStrategy(config)

    keep_running = True

    def _stop_handler(_sig: int, _frame: Any) -> None:
        nonlocal keep_running
        keep_running = False
        logger.info("Stop signal received")

    signal.signal(signal.SIGINT, _stop_handler)

    cycle = 0
    while keep_running:
        cycle += 1

        spot_ticker, spot = _get_spot_price()
        if spot is None:
            logger.warning("No spot available from %s. Skipping cycle.", spot_ticker)
            if cycles > 0 and cycle >= cycles:
                break
            time.sleep(config.SCAN_INTERVAL_SECONDS)
            continue

        futures = get_futures_dollar()
        futures_data: dict[str, dict[str, Any]] = {}
        for inst in futures:
            ticker = inst.get("instrumentId", {}).get("symbol", "")
            if not ticker:
                continue

            days = rate_calc.calculate_days_to_expiry(ticker)
            if days <= 0:
                continue

            months_ahead = max(1, round(days / 30.0))
            if months_ahead < config.MONTHS_AHEAD_MIN or months_ahead > config.MONTHS_AHEAD_MAX:
                continue

            snap = get_snapshot(ticker)
            if not snap:
                continue

            futures_data[ticker] = {
                "bid": snap.get("bid_price"),
                "ask": snap.get("ask_price"),
                "last": snap.get("last"),
                "days": days,
            }

        rates = rate_calc.scan_all_rates(spot=spot, futures_data=futures_data)
        _print_curve(rates, config)

        opportunities = strat.scan_opportunities(rates)
        print(f"Oportunidades detectadas: {len(opportunities)}")
        print("=" * 76)

        if config.ENABLE_TRADING and opportunities:
            for opp in opportunities:
                price = float(opp.get("future_price") or 0.0)
                if price <= 0:
                    continue
                strat.execute_trade(
                    ticker=str(opp["ticker"]),
                    signal=str(opp["signal"]),
                    price=price,
                    size=config.MAX_CONTRACTS_PER_TRADE,
                    implied_rate=float(opp["implied_rate"]),
                    spread=float(opp["spread"]),
                    days=int(opp.get("days") or 0),
                    spot=float(opp.get("spot") or spot),
                )

        if cycles > 0 and cycle >= cycles:
            break

        time.sleep(config.SCAN_INTERVAL_SECONDS)

    summary = strat.get_portfolio_summary()
    logger.info("CC summary: %s", summary)


if __name__ == "__main__":
    args = _parse_args()
    cfg = CCConfig()
    run_cash_carry(cfg, cycles=args.cycles)
