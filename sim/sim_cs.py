"""Offline simulation for Calendar Spread strategy (z-score mean reversion).

Generates two synthetic DLR futures prices with a cointegrated random walk,
feeds them into CalendarSpreadEngine, and checks signal/fill logic without
connecting to reMarkets.

Usage:
    python sim/sim_cs.py
    python sim/sim_cs.py --run-seconds 300 --near-price 1400 --far-price 1420 --seed 0
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.cs_config import CalendarSpreadConfig
from strategies.calendar_spread import CalendarSpreadEngine
from core.utils import setup_logger
from utils.fill_logger import FillLogger

logger = setup_logger("sim_cs")


# ------------------------------------------------------------------ #
#  Synthetic price generator                                           #
# ------------------------------------------------------------------ #

class _CointegratedPair:
    """Generates two cointegrated GBM price series with a mean-reverting spread."""

    def __init__(
        self,
        near_price: float,
        far_price: float,
        drift: float = 0.0,
        vol: float = 1.0,
        spread_mean: float | None = None,
        spread_vol: float = 0.5,
        spread_reversion: float = 0.15,
        seed: int = 42,
    ) -> None:
        random.seed(seed)
        self.near = near_price
        self.far = far_price
        self._drift = drift
        self._vol = vol
        self._spread_mean = spread_mean if spread_mean is not None else (near_price - far_price)
        self._spread_vol = spread_vol
        self._spread_reversion = spread_reversion
        self._spread_residual = self.near - self.far - self._spread_mean

    def step(self) -> tuple[float, float, float, float]:
        """Returns (near_bid, near_ask, far_bid, far_ask) for one synthetic tick."""
        # Common factor (DLR term structure drift)
        common_shock = random.gauss(self._drift, self._vol)
        self.near = max(self.near + common_shock, 1.0)

        # Mean-reverting spread residual (cointegration)
        self._spread_residual += (
            -self._spread_reversion * self._spread_residual
            + random.gauss(0.0, self._spread_vol)
        )
        implied_far = self.near - self._spread_mean - self._spread_residual
        self.far = max(implied_far, 1.0)

        # Synthetic bid/ask with small random half-spread
        near_half = random.uniform(0.1, 0.3)
        far_half = random.uniform(0.1, 0.3)
        near_bid = round(self.near - near_half, 2)
        near_ask = round(self.near + near_half, 2)
        far_bid = round(self.far - far_half, 2)
        far_ask = round(self.far + far_half, 2)
        return near_bid, near_ask, far_bid, far_ask


# ------------------------------------------------------------------ #
#  Simulated order execution                                           #
# ------------------------------------------------------------------ #

def _sim_execute(
    engine: CalendarSpreadEngine,
    cfg: CalendarSpreadConfig,
    fill_logger_near: FillLogger,
    fill_logger_far: FillLogger,
    signal_str: str,
    tick: int,
) -> None:
    """Simulates immediate fill at current synthetic prices."""
    qty = cfg.max_contracts
    near_bid = engine._near_bid or 0.0
    near_ask = engine._near_ask or 0.0
    far_bid = engine._far_bid or 0.0
    far_ask = engine._far_ask or 0.0

    if signal_str == "SELL_SPREAD":
        near_side, far_side = "SELL", "BUY"
        near_price, far_price = near_ask, far_bid
    elif signal_str == "BUY_SPREAD":
        near_side, far_side = "BUY", "SELL"
        near_price, far_price = near_bid, far_ask
    elif signal_str == "CLOSE":
        pos = engine._position
        if pos > 0:
            near_side, far_side = "SELL", "BUY"
            near_price, far_price = near_ask, far_bid
            qty = engine._entry_qty
        elif pos < 0:
            near_side, far_side = "BUY", "SELL"
            near_price, far_price = near_bid, far_ask
            qty = engine._entry_qty
        else:
            return
    else:
        return

    order_id = f"SIM-{tick}-{signal_str}"
    fill_logger_near.log_fill(side=near_side, price=near_price, qty=qty, order_id=order_id + "-N")
    fill_logger_far.log_fill(side=far_side, price=far_price, qty=qty, order_id=order_id + "-F")
    engine.on_fill(near_side, far_side, near_price, far_price, qty)

    logger.info(
        "SIM FILL | signal=%s | near=%s@%.2f | far=%s@%.2f | qty=%d",
        signal_str, near_side, near_price, far_side, far_price, qty,
    )


# ------------------------------------------------------------------ #
#  Main simulation loop                                                #
# ------------------------------------------------------------------ #

def run_simulation(
    run_seconds: int,
    near_price: float,
    far_price: float,
    seed: int,
    tick_interval: float,
) -> None:
    """Runs the offline CS simulation."""
    cfg = CalendarSpreadConfig(
        near_ticker="DLR/MAY26",
        far_ticker="DLR/JUN26",
        enable_trading=True,
        scan_interval_seconds=1,   # Fast scan for sim
    )
    engine = CalendarSpreadEngine(cfg)

    session_date = datetime.now().strftime("%Y%m%d")
    fill_logger_near = FillLogger(
        ticker=cfg.near_ticker,
        session_date=session_date,
        output_dir="logs/fills_sim",
    )
    fill_logger_far = FillLogger(
        ticker=cfg.far_ticker,
        session_date=session_date,
        output_dir="logs/fills_sim",
    )

    market = _CointegratedPair(
        near_price=near_price,
        far_price=far_price,
        vol=2.0,
        spread_vol=0.8,
        spread_reversion=0.10,
        seed=seed,
    )

    running = True
    tick = 0
    last_signal = "HOLD"

    def _stop(_sig: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)

    logger.info(
        "CS sim started | near=%s | far=%s | run_seconds=%d | tick_interval=%.1fs",
        cfg.near_ticker, cfg.far_ticker, run_seconds, tick_interval,
    )

    started = time.time()
    while running:
        elapsed = time.time() - started
        if run_seconds > 0 and elapsed >= run_seconds:
            break

        tick += 1
        nb, na, fb, fa = market.step()
        engine.update_prices(near_bid=nb, near_ask=na, near_last=(nb + na) / 2,
                             far_bid=fb, far_ask=fa, far_last=(fb + fa) / 2)
        engine.update_history()

        state = engine.get_state()
        sig = state["signal"]

        logger.info(
            "CS cycle | near=%.2f | far=%.2f | spread=%.2f | rate=%.1f%% "
            "| zscore=%s | signal=%s | pos=%d | pnl=%.2f",
            state["near_mid"],
            state["far_mid"],
            state["spread"],
            state["implied_rate"],
            f"{state['zscore']:+.2f}" if state["zscore"] is not None else "n/a",
            sig,
            state["position"],
            state["pnl_realized"] + state["pnl_unrealized"],
        )

        if sig in ("BUY_SPREAD", "SELL_SPREAD", "CLOSE") and sig != last_signal:
            _sim_execute(engine, cfg, fill_logger_near, fill_logger_far, sig, tick)

        last_signal = sig
        time.sleep(tick_interval)

    # Final summary
    state = engine.get_state()
    logger.info(
        "=== SIM COMPLETE | ticks=%d | pos=%d | pnl_realized=%.2f | pnl_unrealized=%.2f | history_len=%d ===",
        tick,
        state["position"],
        state["pnl_realized"],
        state["pnl_unrealized"],
        state["history_len"],
    )


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline CS simulation")
    parser.add_argument("--run-seconds", type=int, default=120, help="Duration in seconds (0=infinite)")
    parser.add_argument("--near-price", type=float, default=1400.0, help="Initial near-leg price")
    parser.add_argument("--far-price", type=float, default=1420.0, help="Initial far-leg price")
    parser.add_argument("--tick-interval", type=float, default=0.2, help="Seconds between ticks")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    run_simulation(
        run_seconds=args.run_seconds,
        near_price=args.near_price,
        far_price=args.far_price,
        seed=args.seed,
        tick_interval=args.tick_interval,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("CS simulation stopped by user.")
        sys.exit(0)
