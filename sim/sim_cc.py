"""Offline simulation for Cash & Carry over synthetic DLR futures curve."""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.cc_config import CCConfig
from strategies.cash_carry import CashCarryStrategy
from strategies.implied_rate import ImpliedRateCalculator
from core.utils import setup_logger

logger = setup_logger("sim_cc")

_MONTHS = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]


@dataclass(slots=True)
class SimTrade:
    """Represents one simulated cash-carry trade lifecycle."""

    id: int
    day_open: int
    ticker: str
    signal: str
    size: int
    entry_spot: float
    entry_future: float
    days_open_target: int
    realized_pnl: float = 0.0
    closed: bool = False
    day_close: int | None = None


def _month_add(d: date, months: int) -> tuple[int, int]:
    """Adds months to date and returns (year, month)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return y, m


def _make_ticker(today: date, months_ahead: int) -> str:
    """Builds DLR ticker for a month ahead offset."""
    y, m = _month_add(today, months_ahead)
    return f"DLR/{_MONTHS[m - 1]}{str(y)[-2:]}"


def _parse_args() -> argparse.Namespace:
    """Parses CLI args."""
    parser = argparse.ArgumentParser(description="Offline Cash & Carry simulation")
    parser.add_argument("--days", type=int, default=30, help="Number of simulated days")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    return parser.parse_args()


def _trade_mark_to_market(trade: SimTrade, current_spot: float, current_future: float, ref_rate: float, day_count: int) -> float:
    """Computes MTM pnl for one open trade in ARS."""
    hold_days = max(1, day_count - trade.day_open + 1)
    notional = trade.size * 1000.0
    financing = trade.entry_spot * notional * (ref_rate / 100.0) * (hold_days / 365.0)

    if trade.signal == "CASH_CARRY":
        gross = (trade.entry_future - current_future) * notional
    else:
        gross = (current_future - trade.entry_future) * notional
    return gross - financing


def run_simulation(days: int, seed: int) -> None:
    """Runs a day-by-day synthetic simulation for CC strategy."""
    random.seed(seed)

    cfg = CCConfig(ENABLE_TRADING=False)
    calc = ImpliedRateCalculator()
    strat = CashCarryStrategy(cfg)

    today = date.today()
    maturities = [1, 2, 3, 4, 6, 9, 12]
    tickers = [_make_ticker(today, m) for m in maturities]

    spot = 1400.0
    open_trades: list[SimTrade] = []
    closed_trades: list[SimTrade] = []
    next_trade_id = 1
    equity_curve: list[float] = []

    logger.info("SIM CC start | days=%d | seed=%d", days, seed)

    for day in range(1, days + 1):
        spot += random.gauss(0.7, 2.5)
        spot = max(800.0, spot)

        futures_data: dict[str, dict[str, Any]] = {}
        for idx, ticker in enumerate(tickers):
            rem_days = max(1, 30 * maturities[idx] - day)
            term_rate = cfg.REFERENCE_RATE_TNA + 0.5 * maturities[idx] + random.gauss(0.0, 1.2)

            if random.random() < 0.15:
                term_rate += random.choice([-4.0, 4.0, 6.0, -6.0])

            theo = spot * (1.0 + (term_rate / 100.0) * (rem_days / 365.0))
            spread = random.uniform(0.5, 3.0)
            bid = round((theo - spread / 2.0) * 2.0) / 2.0
            ask = round((theo + spread / 2.0) * 2.0) / 2.0
            last = round((bid + ask) / 2.0 * 2.0) / 2.0

            futures_data[ticker] = {
                "bid": bid,
                "ask": ask,
                "last": last,
                "days": rem_days,
            }

        rates = calc.scan_all_rates(spot=spot, futures_data=futures_data)
        opps = strat.scan_opportunities(rates)

        if opps:
            best = sorted(opps, key=lambda x: abs(float(x["spread"])), reverse=True)[0]
            ticker = str(best["ticker"])
            fut_price = float(best.get("future_price") or futures_data[ticker]["last"])
            size = min(cfg.MAX_CONTRACTS_PER_TRADE, max(1, cfg.MAX_TOTAL_POSITION // 4))

            res = strat.execute_trade(
                ticker=ticker,
                signal=str(best["signal"]),
                price=fut_price,
                size=size,
                implied_rate=float(best["implied_rate"]),
                spread=float(best["spread"]),
                days=int(best.get("days") or 1),
                spot=spot,
            )
            if res.get("status") == "OK":
                tr = SimTrade(
                    id=next_trade_id,
                    day_open=day,
                    ticker=ticker,
                    signal=str(best["signal"]),
                    size=size,
                    entry_spot=spot,
                    entry_future=fut_price,
                    days_open_target=int(best.get("days") or 1),
                )
                next_trade_id += 1
                open_trades.append(tr)

        day_realized = 0.0
        day_unrealized = 0.0
        still_open: list[SimTrade] = []
        for tr in open_trades:
            rem = max(0, tr.days_open_target - (day - tr.day_open))
            current_future = float(futures_data.get(tr.ticker, {}).get("last", spot))
            mtm = _trade_mark_to_market(tr, spot, current_future, cfg.REFERENCE_RATE_TNA, day)
            day_unrealized += mtm

            if rem <= 1:
                tr.realized_pnl = mtm
                tr.closed = True
                tr.day_close = day
                day_realized += mtm
                closed_trades.append(tr)
            else:
                still_open.append(tr)
        open_trades = still_open

        equity = sum(t.realized_pnl for t in closed_trades) + day_unrealized
        equity_curve.append(equity)
        logger.info(
            "DAY %02d | spot=%.2f | opps=%d | open=%d | closed=%d | equity=%.2f",
            day,
            spot,
            len(opps),
            len(open_trades),
            len(closed_trades),
            equity,
        )

    # Force close remaining trades at final spot.
    for tr in open_trades:
        mtm = _trade_mark_to_market(tr, spot, spot, cfg.REFERENCE_RATE_TNA, days)
        tr.realized_pnl = mtm
        tr.closed = True
        tr.day_close = days
        closed_trades.append(tr)

    total_pnl = sum(t.realized_pnl for t in closed_trades)
    gross_profit = sum(max(0.0, t.realized_pnl) for t in closed_trades)
    gross_loss = sum(abs(min(0.0, t.realized_pnl)) for t in closed_trades)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    peak = float("-inf")
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    logger.info("SIM CC summary start")
    logger.info("Trades executed: %d", len(closed_trades))
    for t in closed_trades:
        logger.info(
            "Trade %03d | %s | %s | size=%d | open_day=%d | close_day=%d | pnl=%.2f",
            t.id,
            t.ticker,
            t.signal,
            t.size,
            t.day_open,
            t.day_close or days,
            t.realized_pnl,
        )
    logger.info("PnL total: %.2f", total_pnl)
    logger.info("Gross profit: %.2f", gross_profit)
    logger.info("Gross loss: %.2f", gross_loss)
    logger.info("Profit factor: %.4f", profit_factor)
    logger.info("Max drawdown: %.2f", max_dd)
    logger.info("SIM CC summary end")


if __name__ == "__main__":
    args = _parse_args()
    run_simulation(days=args.days, seed=args.seed)
