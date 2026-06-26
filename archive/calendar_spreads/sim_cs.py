"""Offline simulation for the DLR Calendar Spread strategy.

Generates a 120-day synthetic DLR futures term structure with mean-reverting
forward rates. Runs the CalendarSpreadEngine on that synthetic data and
produces a full performance summary.

Usage:
    python sim_cs.py [--days 120] [--seed 42]
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import numpy as np

from cs_config import CalendarSpreadConfig
from strategies import CalendarSpreadEngine
from global_risk import GlobalRiskManager
from utils import setup_logger

logger = setup_logger("sim_cs")


# ------------------------------------------------------------------ #
#  Synthetic curve generation                                          #
# ------------------------------------------------------------------ #

MESES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]

# Synthetic contracts: 6-month rolling front. Expiry offsets in calendar days.
CONTRATO_OFFSETS = [30, 60, 90, 120, 150, 180]  # days from "today"


def _gen_spot_path(days: int, rng: np.random.Generator, s0: float = 1200.0) -> np.ndarray:
    """Geometric random walk for DLR spot with mild upward drift."""
    drift = 0.0012       # ~0.12%/day ≈ 55% annual
    vol = 0.008          # daily vol
    log_ret = rng.normal(drift, vol, days)
    prices = s0 * np.exp(np.cumsum(log_ret))
    return prices


def _gen_rate_path(
    days: int,
    rng: np.random.Generator,
    rate0: float = 30.0,
    mean_rate: float = 30.0,
    kappa: float = 0.08,
    rate_vol: float = 3.0,
    shock_every: int = 30,
    shock_size: float = 5.0,
) -> np.ndarray:
    """Mean-reverting Ornstein–Uhlenbeck process for TNA forward rate (%)."""
    rates = np.empty(days)
    r = rate0
    for t in range(days):
        dr = kappa * (mean_rate - r) + rate_vol * rng.normal()
        r += dr
        # Periodic shocks
        if (t + 1) % shock_every == 0:
            r += rng.choice([-shock_size, shock_size])
        r = max(5.0, r)  # floor at 5%
        rates[t] = r
    return rates


def _future_price(spot: float, rate_tna: float, days_to_expiry: int) -> float:
    """Synthetic DLR future price: spot * (1 + rate * days/365)."""
    return spot * (1.0 + rate_tna / 100.0 * days_to_expiry / 365.0)


def _build_daily_futures(
    spot: float, rate_tna: float, offsets: list[int], day: int
) -> list[dict[str, Any]]:
    """Builds the synthetic futures strip for one simulation day."""
    contracts = []
    for i, offset in enumerate(offsets):
        expiry_days = max(offset - day, 1)  # shrinks as time passes
        ticker = f"DLR/{MESES[i % 12]}26"
        price = _future_price(spot, rate_tna, expiry_days)
        contracts.append({
            "ticker": ticker,
            "price": price,
            "expiry_days": expiry_days,
        })
    # Sort by expiry_days ascending
    contracts.sort(key=lambda x: x["expiry_days"])
    return contracts


# ------------------------------------------------------------------ #
#  Simulation loop                                                     #
# ------------------------------------------------------------------ #

def run_simulation(days: int = 120, seed: int = 42) -> None:
    cfg = CalendarSpreadConfig()
    engine = CalendarSpreadEngine(
        z_entry=cfg.Z_SCORE_ENTRY,
        z_exit=cfg.Z_SCORE_EXIT,
        lookback=cfg.LOOKBACK_WINDOW,
        max_contracts=cfg.MAX_CONTRACTS,
        max_open=cfg.MAX_OPEN_SPREADS,
        multiplier=cfg.CONTRACT_MULTIPLIER,
        max_loss_per_spread=cfg.MAX_LOSS_PER_SPREAD,
        max_strategy_mtm_loss=cfg.MAX_STRATEGY_MTM_LOSS,
        max_nocional_total=cfg.MAX_NOCIONAL_TOTAL,
        max_holding_days=cfg.MAX_HOLDING_DAYS,
    )
    grm = GlobalRiskManager()

    rng = np.random.default_rng(seed)
    spot_path = _gen_spot_path(days, rng)
    rate_path = _gen_rate_path(days, rng)

    # Metrics
    fills: list[dict] = []       # all trade events
    stop_reasons: dict[str, int] = {}  # count by reason
    daily_total_mtm: list[float] = []  # daily total MTM (realized + unrealized)
    cum_realized = 0.0
    first_trade_days: list[int] = []  # track days where first trades happen

    logger.info("=== SIM Calendar Spread | %d dias | seed=%d ===", days, seed)
    logger.info("%-6s | %-25s | %-8s | %-8s | %-10s | %-5s | %-12s | %-10s",
                "DIA", "Par", "Spread", "Z-score", "Senial", "POS", "PNL_dia", "PNL_acum")

    for day in range(days):
        spot = float(spot_path[day])
        rate = float(rate_path[day])
        futures = _build_daily_futures(spot, rate, CONTRATO_OFFSETS, day)

        # Remove contracts that have expired (expiry_days <= 0)
        futures = [f for f in futures if f["expiry_days"] > 0]
        if len(futures) < 2:
            continue

        price_map = {f["ticker"]: f["price"] for f in futures}

        pnl_today = 0.0

        # ---- STEP 1: Apply risk limits (stop-loss / time-stop / strategy stop) ----
        risk_closures = engine.check_risk_limits(price_map, day)
        for pair_id, reason in risk_closures:
            # Find current prices for this pair
            pos_match = next((p for p in engine.open_positions if p.pair_id == pair_id), None)
            if pos_match is None:
                continue
            near_p = price_map.get(pos_match.near_ticker, pos_match.entry_near_price)
            far_p = price_map.get(pos_match.far_ticker, pos_match.entry_far_price)
            pnl = engine.on_close_spread(pair_id, near_p, far_p, day)
            pnl_today += pnl
            cum_realized += pnl
            fills.append({"day": day, "pair_id": pair_id, "action": "CLOSE",
                           "pnl": pnl, "reason": reason})
            stop_reasons[reason] = stop_reasons.get(reason, 0) + 1

        # ---- STEP 2: Check global kill switch ----
        open_mtm = _compute_open_mtm(engine, futures)
        grm.update("calendar_spread", cum_realized, open_mtm,
                   engine.get_nocional_total())
        risk_result = grm.check_risk()
        if risk_result["status"] == "KILLED":
            logger.critical("DAY %03d | GLOBAL KILL SWITCH | Cerrando todo", day + 1)
            for pos in list(engine.open_positions):
                near_p = price_map.get(pos.near_ticker, pos.entry_near_price)
                far_p = price_map.get(pos.far_ticker, pos.entry_far_price)
                pnl = engine.on_close_spread(pos.pair_id, near_p, far_p, day)
                pnl_today += pnl
                cum_realized += pnl
                fills.append({"day": day, "pair_id": pos.pair_id, "action": "CLOSE",
                               "pnl": pnl, "reason": "kill_switch"})
            break

        # ---- STEP 3: Normal signal processing ----
        # Re-identify pairs each day
        pairs = engine.identify_spread_pairs(futures)
        signals = engine.generate_signals(pairs)

        for sig in signals:
            act = sig["action"]

            if act in ("SELL_SPREAD", "BUY_SPREAD"):
                # Apply global allocation multiplier
                alloc = grm.get_allocation_multiplier()
                if alloc <= 0:
                    continue
                size = max(1, int(cfg.MAX_CONTRACTS * alloc))
                engine.on_fill_spread(
                    pair_id=sig["pair_id"],
                    near_ticker=sig["near_ticker"],
                    far_ticker=sig["far_ticker"],
                    signal=act,
                    near_price=sig["near_price"],
                    far_price=sig["far_price"],
                    days_between=sig["days_between"],
                    size=size,
                    current_day=day,
                    z_score=sig["z_score"],
                )
                fills.append({"day": day, "pair_id": sig["pair_id"], "action": act,
                               "z_score": sig["z_score"]})

            elif act == "CLOSE":
                pnl = engine.on_close_spread(sig["pair_id"], sig["near_price"],
                                             sig["far_price"], day)
                pnl_today += pnl
                cum_realized += pnl
                fills.append({"day": day, "pair_id": sig["pair_id"], "action": "CLOSE",
                               "pnl": pnl, "reason": "signal"})

        # ---- STEP 4: Record daily MTM ----
        open_mtm = _compute_open_mtm(engine, futures)
        total_daily = cum_realized + open_mtm
        daily_total_mtm.append(total_daily)

        # Determine if this is a first-trade-day for logging
        has_trade = any(f["day"] == day and f["action"] in ("SELL_SPREAD", "BUY_SPREAD", "CLOSE")
                        for f in fills)
        if has_trade:
            first_trade_days.append(day)

        # Daily log — show first 2 signals for brevity
        for sig in signals[:2]:
            z_str = f"{sig['z_score']:+.2f}" if sig["n_history"] >= 2 else "  ---"
            logger.info(
                "DAY %03d | %-25s | %8.2f | %8s | %-10s | %3d | %+12.2f | %+10.2f",
                day + 1,
                sig["pair_id"],
                sig["spread"],
                z_str,
                sig["action"],
                len(engine.open_positions),
                pnl_today,
                engine.realized_pnl,
            )

    # Force-close any remaining positions on last day
    if engine.open_positions:
        futures_last = _build_daily_futures(
            float(spot_path[-1]), float(rate_path[-1]), CONTRATO_OFFSETS, days - 1
        )
        price_map_last = {f["ticker"]: f["price"] for f in futures_last}
        for pos in list(engine.open_positions):
            near_p = price_map_last.get(pos.near_ticker, pos.entry_near_price)
            far_p = price_map_last.get(pos.far_ticker, pos.entry_far_price)
            pnl = engine.on_close_spread(pos.pair_id, near_p, far_p, days)
            cum_realized += pnl
            daily_total_mtm.append(cum_realized)

    _print_summary(fills, daily_total_mtm, engine, days, cfg, stop_reasons)


def _compute_open_mtm(engine: CalendarSpreadEngine, futures: list[dict]) -> float:
    """Incremental mark-to-market PnL of open positions against current prices."""
    price_map = {f["ticker"]: f["price"] for f in futures}
    mtm = 0.0
    for pos in engine.open_positions:
        near_p = price_map.get(pos.near_ticker, pos.entry_near_price)
        far_p = price_map.get(pos.far_ticker, pos.entry_far_price)
        cur_spread = far_p - near_p
        if pos.signal == "SELL_SPREAD":
            mtm += (pos.entry_spread - cur_spread) * pos.contracts * pos.multiplier
        else:
            mtm += (cur_spread - pos.entry_spread) * pos.contracts * pos.multiplier
    return mtm


def _print_summary(
    fills: list[dict],
    daily_total_mtm: list[float],
    engine: CalendarSpreadEngine,
    days: int,
    cfg: CalendarSpreadConfig,
    stop_reasons: dict[str, int],
) -> None:
    """Prints a detailed performance summary."""
    arr = np.asarray(daily_total_mtm, dtype=float)

    trades = [f for f in fills if f["action"] in ("SELL_SPREAD", "BUY_SPREAD")]
    closes = [f for f in fills if f["action"] == "CLOSE"]

    wins = [c["pnl"] for c in closes if c.get("pnl", 0) > 0]
    losses = [c["pnl"] for c in closes if c.get("pnl", 0) <= 0]

    win_rate = len(wins) / len(closes) * 100 if closes else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 1e-9 else float("inf")

    # Average holding period
    holding_periods: list[int] = []
    open_days: dict[str, int] = {}
    for f in fills:
        if f["action"] in ("SELL_SPREAD", "BUY_SPREAD"):
            open_days[f["pair_id"]] = f["day"]
        elif f["action"] == "CLOSE" and f["pair_id"] in open_days:
            holding_periods.append(f["day"] - open_days.pop(f["pair_id"]))

    avg_holding = np.mean(holding_periods) if holding_periods else 0.0

    # Spreads captured (avg |z| at entry minus |z| at close proxy)
    entry_zs = [abs(f["z_score"]) for f in fills if "z_score" in f]
    avg_z_entry = np.mean(entry_zs) if entry_zs else 0.0

    # Drawdown (on cumulative P&L curve including open MTM)
    peak = np.maximum.accumulate(arr)
    drawdown = arr - peak
    max_dd = float(np.min(drawdown))
    total_pnl = engine.realized_pnl

    logger.info("")
    logger.info("=" * 62)
    logger.info("  RESUMEN SIMULACION - Calendar Spread DLR (%d dias)", days)
    logger.info("=" * 62)
    logger.info("  Pares analizados (unicos) : %d", len(engine.spread_histories))
    logger.info("  Spreads operados          : %d", len(trades))
    logger.info("  Spreads cerrados          : %d", len(closes))
    logger.info("  Tasa de acierto           : %.1f%%", win_rate)
    logger.info("  Profit factor             : %.2f", profit_factor)
    logger.info("  Periodo promedio (dias)   : %.1f", avg_holding)
    logger.info("  Z-score entrada promedio  : %.2f", avg_z_entry)
    logger.info("  PNL realizado ARS         : %+.2f", total_pnl)
    logger.info("  Drawdown maximo MTM ARS   : %.2f", max_dd)
    logger.info("  --- Cierres por motivo ---")
    for reason, count in sorted(stop_reasons.items()):
        logger.info("    %-25s: %d", reason, count)
    logger.info("  MAX_CONTRACTS=%-3d | MAX_OPEN=%d | STOP_SPREAD=%.0fK | STRAT_STOP=%.0fK | TIME=%dd",
                cfg.MAX_CONTRACTS, cfg.MAX_OPEN_SPREADS,
                cfg.MAX_LOSS_PER_SPREAD / 1000, cfg.MAX_STRATEGY_MTM_LOSS / 1000,
                cfg.MAX_HOLDING_DAYS)
    logger.info("=" * 62)


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simulacion Calendar Spread DLR")
    p.add_argument("--days", type=int, default=120, help="Dias de simulacion (default: 120)")
    p.add_argument("--seed", type=int, default=42, help="Semilla aleatoria (default: 42)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_simulation(days=args.days, seed=args.seed)
