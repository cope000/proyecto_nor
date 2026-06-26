"""Offline simulation for equity futures vs spot arbitrage."""

from __future__ import annotations

import argparse
import math

import numpy as np

from eq_config import EquityArbConfig
from global_risk import GlobalRiskManager
from strategies.equity_arb import EquityArbEngine
from utils import setup_logger

logger = setup_logger("sim_eq_arb")


def run_simulation(days: int = 60, seed: int = 42) -> None:
    cfg = EquityArbConfig()
    engine = EquityArbEngine(
        reference_rate_tna=cfg.REFERENCE_RATE_TNA,
        min_spread_bps=cfg.MIN_SPREAD_BPS,
        contract_multiplier=cfg.CONTRACT_MULTIPLIER,
    )
    grm = GlobalRiskManager()

    rng = np.random.default_rng(seed)

    specs = {
        "GGAL": {"s0": 5000.0, "drift": 0.0015, "vol": 0.025},
        "PAMP": {"s0": 3800.0, "drift": 0.0010, "vol": 0.020},
        "YPFD": {"s0": 45000.0, "drift": 0.0008, "vol": 0.018},
    }

    spots = {k: v["s0"] for k, v in specs.items()}
    rate_state = {k: 30.0 for k in specs}

    trades = 0
    wins = 0
    pnl_path = [0.0]

    logger.info("=== SIM Equity Arb | %d dias | seed=%d ===", days, seed)
    logger.info("DIA     | TICKER | SPOT     | FUT_30   | TNA     | SIGNAL             | PNL_D      | PNL_T")

    for day in range(1, days + 1):
        spot_map: dict[str, float] = {}
        futs_by_sym: dict[str, list[dict]] = {}

        for sym, sp in specs.items():
            ret = rng.normal(sp["drift"], sp["vol"])
            spots[sym] *= math.exp(ret)
            spot = spots[sym]
            spot_map[sym] = spot

            # Mean-reverting implied rate with periodic shocks.
            r = rate_state[sym]
            r += 0.20 * (30.0 - r) + rng.normal(0.0, 1.5)
            if day % 15 == 0:
                r += rng.choice([-6.0, 6.0])
            r = float(np.clip(r, 10.0, 55.0))
            rate_state[sym] = r

            f30 = spot * (1.0 + r / 100.0 * 30.0 / 365.0)
            f60 = spot * (1.0 + (r + rng.normal(0.0, 1.0)) / 100.0 * 60.0 / 365.0)

            futs_by_sym[sym] = [
                {"ticker": f"{sym}/ABR26", "price": f30, "days": 30},
                {"ticker": f"{sym}/MAY26", "price": f60, "days": 60},
            ]

        scan = engine.scan_equity_futures(cfg.INSTRUMENTS, futs_by_sym, spot_map)
        signals = engine.generate_signals(scan)

        # Respect global position cap and per-ticker notional cap.
        open_contracts = sum(p.contracts for p in engine.positions)
        for sig in signals:
            if open_contracts >= cfg.MAX_TOTAL_POSITION:
                break
            ticker_open = sum(p.contracts for p in engine.positions if p.ticker == sig["ticker"])
            if ticker_open >= cfg.MAX_CONTRACTS:
                continue

            max_by_nocional = int(cfg.MAX_NOCIONAL_PER_TICKER // (sig["spot"] * cfg.CONTRACT_MULTIPLIER))
            allowed = min(cfg.MAX_CONTRACTS - ticker_open, cfg.MAX_TOTAL_POSITION - open_contracts, max_by_nocional)
            if allowed <= 0:
                continue

            engine.execute_arb(sig, size=allowed, day=day)
            trades += 1
            open_contracts += allowed

        future_price_map = {
            f["ticker"]: f["price"]
            for arr in futs_by_sym.values()
            for f in arr
        }
        pnl_day = engine.mark_to_market(spot_map, future_price_map, day)
        pnl_total = pnl_path[-1] + pnl_day
        pnl_path.append(pnl_total)

        if pnl_day > 0:
            wins += 1

        grm.update("equity_arb", pnl_total, 0.0, float(open_contracts) * 100000.0)
        risk = grm.check_risk()
        if risk["status"] == "KILLED":
            logger.critical("DAY %03d | GLOBAL KILL SWITCH", day)
            break

        row = next((r for r in scan if r["ticker"] == "GGAL" and r["days"] == 30), None)
        if row:
            tna = row["implied_rate"] if row["implied_rate"] is not None else 0.0
            logger.info(
                "DAY %03d | %-6s | %8.2f | %8.2f | %6.2f%% | %-18s | %+10.2f | %+10.2f",
                day,
                "GGAL",
                row["spot"],
                row["future_price"],
                tna,
                row["signal"],
                pnl_day,
                pnl_total,
            )

    arr = np.asarray(pnl_path, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = arr - peak
    max_dd = float(np.min(dd))

    logger.info("")
    logger.info("============================================================")
    logger.info("RESUMEN SIMULACION - Equity Arb (%d dias)", days)
    logger.info("============================================================")
    logger.info("Trades ejecutados          : %d", trades)
    logger.info("Dias con PnL positivo      : %d", wins)
    logger.info("Win rate diario            : %.1f%%", (wins / max(days, 1)) * 100.0)
    logger.info("PnL total ARS              : %s", f"{arr[-1]:+,.2f}")
    logger.info("Max drawdown ARS           : %s", f"{max_dd:+,.2f}")
    logger.info("Posiciones abiertas final  : %d", len(engine.positions))
    logger.info("============================================================")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulacion Equity Arb")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_simulation(days=args.days, seed=args.seed)
