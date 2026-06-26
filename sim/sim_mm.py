"""Offline simulation mode for validating market making logic without reMarkets."""

from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Tuple

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.mm_config import MMConfig
from strategies import FairValueCalculator, InventoryManager, MarketMaker, RiskManager
from core.utils import setup_logger
from utils.fill_logger import FillLogger

logger = setup_logger("sim_mode")


@dataclass(slots=True)
class SimStats:
    """Holds runtime statistics for simulation summary."""

    total_ticks: int = 0
    quotes_calculated: int = 0
    quotes_skipped: int = 0
    buy_fills: int = 0
    sell_fills: int = 0
    max_drawdown: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    roundtrips_total: int = 0
    roundtrips_win: int = 0
    max_long_position: int = 0
    max_short_position: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0


class SimulatedMarketMaker(MarketMaker):
    """MarketMaker variant that does not send real orders and only tracks quote IDs."""

    def __init__(
        self,
        config: MMConfig,
        fair_value_calc: FairValueCalculator,
        inventory_mgr: InventoryManager,
        risk_mgr: RiskManager,
    ) -> None:
        super().__init__(config, fair_value_calc, inventory_mgr, risk_mgr)
        self._seq = 0
        self._is_simulation = True

    def cancel_existing_quotes(self) -> None:
        """Clears in-memory active quote IDs in simulation mode."""
        self.active_bid_id = None
        self.active_bid_prop = None
        self.active_ask_id = None
        self.active_ask_prop = None

    def place_quotes(self, ticker: str, bid_price: float, ask_price: float) -> None:
        """Stores synthetic quote IDs without external API calls."""
        if not self.config.ENABLE_TRADING:
            return

        pos = self.inventory_mgr.position
        can_buy = pos < self.config.MAX_POSITION
        can_sell = pos > -self.config.MAX_POSITION

        if can_buy:
            self._seq += 1
            self.active_bid_id = f"SIM-BID-{self._seq}"
            self.active_bid_prop = "SIM"
        else:
            self.active_bid_id = None
            self.active_bid_prop = None

        if can_sell:
            self._seq += 1
            self.active_ask_id = f"SIM-ASK-{self._seq}"
            self.active_ask_prop = "SIM"
        else:
            self.active_ask_id = None
            self.active_ask_prop = None


def _fmt_px(value: float | None) -> str:
    """Formats optional prices for ASCII logs."""
    if value is None:
        return "NA"
    return f"{value:.2f}"


def _clamp01(value: float) -> float:
    """Clamps a float to [0, 1]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _simulate_fill_decisions(
    bid_quote: float | None,
    ask_quote: float | None,
    market_bid: float | None,
    market_ask: float | None,
) -> tuple[bool, bool]:
    """Returns (buy_fill, sell_fill) using cross, passive, and sweep rules."""
    buy_fill = False
    sell_fill = False

    # Market sweep: aggressive flow can hit/lift regardless of exact price.
    if random.random() < 0.05:
        if bid_quote is not None:
            buy_fill = True
        if ask_quote is not None:
            sell_fill = True

    # Cross fill: deterministic when crossing market.
    if bid_quote is not None and market_ask is not None and bid_quote >= market_ask:
        buy_fill = True
    if ask_quote is not None and market_bid is not None and ask_quote <= market_bid:
        sell_fill = True

    # Passive fill inside spread with aggressiveness-based probability.
    if (
        not buy_fill
        and bid_quote is not None
        and market_bid is not None
        and market_ask is not None
        and market_bid < bid_quote < market_ask
    ):
        width = market_ask - market_bid
        if width > 0:
            p_buy = 0.15 + 0.35 * ((bid_quote - market_bid) / width)
            if random.random() < _clamp01(p_buy):
                buy_fill = True

    if (
        not sell_fill
        and ask_quote is not None
        and market_bid is not None
        and market_ask is not None
        and market_bid < ask_quote < market_ask
    ):
        width = market_ask - market_bid
        if width > 0:
            p_sell = 0.15 + 0.35 * ((market_ask - ask_quote) / width)
            if random.random() < _clamp01(p_sell):
                sell_fill = True

    return buy_fill, sell_fill


def _apply_fill(
    inv_mgr: InventoryManager,
    stats: SimStats,
    fill_logger: FillLogger,
    side: str,
    price: float,
    size: int,
) -> None:
    """Applies a fill, updates stats, and logs fill details."""
    prev_pos = inv_mgr.position
    prev_realized = inv_mgr.realized_pnl
    inv_mgr.on_fill(side=side, price=price, size=size)
    inv_mgr.get_unrealized_pnl(price)

    if side == "BUY":
        stats.buy_fills += size
        stats.buy_notional += price * size
    else:
        stats.sell_fills += size
        stats.sell_notional += price * size

    delta_realized = inv_mgr.realized_pnl - prev_realized
    if abs(delta_realized) > 0:
        stats.roundtrips_total += 1
        if delta_realized > 0:
            stats.roundtrips_win += 1
            stats.gross_profit += delta_realized
        else:
            stats.gross_loss += abs(delta_realized)

    stats.max_long_position = max(stats.max_long_position, inv_mgr.position)
    stats.max_short_position = min(stats.max_short_position, inv_mgr.position)

    logger.info(
        ">>> FILL %s %d @ %.2f | POS: %d -> %d | PNL_R: %.2f | PNL_U: %.2f",
        side,
        size,
        price,
        prev_pos,
        inv_mgr.position,
        inv_mgr.realized_pnl,
        inv_mgr.unrealized_pnl,
    )
    fill_logger.log_fill(
        side=side,
        price=price,
        qty=size,
        order_id=f"SIM-{stats.total_ticks}-{side}-{price:.2f}",
    )


def _parse_args() -> argparse.Namespace:
    """Parses CLI args for offline simulation."""
    parser = argparse.ArgumentParser(description="Offline simulation for DLR market maker")
    parser.add_argument("--run-seconds", type=int, default=120, help="Simulation duration in seconds")
    parser.add_argument("--ticker", type=str, default="DLR/ABR26", help="Ticker to simulate")
    parser.add_argument("--base-price", type=float, default=None, help="Deprecated alias for start price")
    parser.add_argument("--start-price", type=float, default=None, help="Initial synthetic price")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible results")
    return parser.parse_args()


def _infer_instrument_key(ticker: str) -> str:
    symbol = str(ticker or "").upper()
    if symbol.startswith("SOJ.MIN"):
        return "SOJ_MIN"
    if symbol.startswith("SOJ.ROS"):
        return "SOJ"
    if symbol.startswith("CAUC"):
        return "CAUC"
    return "DLR"


def _default_start_price(ticker: str) -> float:
    symbol = str(ticker or "").upper()
    if symbol.startswith("SOJ.MIN"):
        return 324.6
    if symbol.startswith("SOJ.ROS"):
        return 324.6
    if symbol.startswith("CAUC"):
        return 35.0
    return 1400.0


def run_simulation(run_seconds: int, ticker: str, start_price: float | None, seed: int) -> None:
    """Runs offline MM simulation with synthetic market data and fill model."""
    random.seed(seed)

    instrument_key = _infer_instrument_key(ticker)
    instrument_cfg = MMConfig().instruments[instrument_key]
    initial_price = float(start_price if start_price is not None else _default_start_price(ticker))

    cfg = MMConfig(
        ENABLE_TRADING=True,
        QUOTE_REFRESH_SECONDS=1.0,
    )
    cfg.TICKER = ticker
    cfg.BASE_SPREAD_BPS = instrument_cfg.base_spread_bps
    cfg.MIN_SPREAD_BPS = instrument_cfg.min_spread_bps
    cfg.MIN_EDGE_BPS = instrument_cfg.min_edge_bps
    cfg.ORDER_SIZE = instrument_cfg.order_size
    cfg.MAX_POSITION = instrument_cfg.max_position
    cfg.INVENTORY_SKEW_FACTOR = instrument_cfg.inventory_skew_factor
    cfg.EWMA_ALPHA = instrument_cfg.ema_alpha
    cfg.MAX_DAILY_LOSS = instrument_cfg.max_daily_loss
    cfg.TICK_SIZE = instrument_cfg.tick_size

    fv_calc = FairValueCalculator(alpha=cfg.EWMA_ALPHA)
    inv_mgr = InventoryManager(max_position=cfg.MAX_POSITION, skew_factor_bps=cfg.INVENTORY_SKEW_FACTOR)
    risk_mgr = RiskManager(max_daily_loss=cfg.MAX_DAILY_LOSS, max_position=cfg.MAX_POSITION)
    mm = SimulatedMarketMaker(cfg, fv_calc, inv_mgr, risk_mgr)
    fill_logger = FillLogger(
        ticker=cfg.TICKER,
        session_date=datetime.now().strftime("%Y%m%d"),
        output_dir="logs/fills_sim",
    )

    running = True

    def _stop_handler(_sig: int, _frame: object) -> None:
        nonlocal running
        running = False
        logger.info("Stop signal received. Finishing simulation loop.")

    signal.signal(signal.SIGINT, _stop_handler)

    tick_price = initial_price
    mids_history: list[float] = [initial_price]
    spread_samples: list[float] = []
    equity_peak = 0.0
    stats = SimStats()
    last_trade_price = initial_price

    started = time.time()
    while running:
        elapsed = time.time() - started
        if run_seconds > 0 and elapsed >= run_seconds:
            break

        stats.total_ticks += 1

        # Random walk around current price plus periodic shock events.
        tick_price += random.gauss(0.0, 0.5)
        if stats.total_ticks % 20 == 0:
            tick_price += random.gauss(0.0, 3.0)

        spread = random.uniform(0.5, 3.0)
        if stats.total_ticks % 30 == 0:
            spread += random.uniform(2.0, 5.0)

        bid = round(tick_price - spread / 2.0, 2)
        ask = round(tick_price + spread / 2.0, 2)

        r = random.random()
        if r < 0.05:
            bid = None
            ask = None
        elif r < 0.15:
            ask = None

        # Last trade price with lag 1-3 ticks.
        lag = random.randint(1, 3)
        if len(mids_history) > lag:
            last = mids_history[-lag]
        else:
            last = mids_history[-1]
        last = 0.7 * last + 0.3 * last_trade_price

        if bid is not None and ask is not None:
            mids_history.append((bid + ask) / 2.0)
        elif bid is not None:
            mids_history.append(bid)
        elif ask is not None:
            mids_history.append(ask)
        else:
            mids_history.append(mids_history[-1])

        mm.on_market_data(ticker=cfg.TICKER, bid=bid, ask=ask, last=last)

        fair = fv_calc.get_fair_value()
        if fair > 0:
            stats.quotes_calculated += 1
            spread_samples.append(mm.last_spread_bps)
        else:
            stats.quotes_skipped += 1

        fill_count = 0
        buy_quote = mm.last_bid_quote
        sell_quote = mm.last_ask_quote

        # Simulated fills with cross, passive, and sweep logic.
        if cfg.ENABLE_TRADING:
            buy_fill, sell_fill = _simulate_fill_decisions(
                bid_quote=buy_quote,
                ask_quote=sell_quote,
                market_bid=bid,
                market_ask=ask,
            )

            if buy_fill and buy_quote is not None and inv_mgr.position < cfg.MAX_POSITION:
                _apply_fill(inv_mgr, stats, fill_logger, "BUY", buy_quote, cfg.ORDER_SIZE)
                last_trade_price = buy_quote
                fill_count += cfg.ORDER_SIZE

            if sell_fill and sell_quote is not None and inv_mgr.position > -cfg.MAX_POSITION:
                _apply_fill(inv_mgr, stats, fill_logger, "SELL", sell_quote, cfg.ORDER_SIZE)
                last_trade_price = sell_quote
                fill_count += cfg.ORDER_SIZE

        risk_mgr.check_risk(inv_mgr, fair if fair > 0 else mids_history[-1])
        total_pnl = inv_mgr.realized_pnl + inv_mgr.unrealized_pnl
        equity_peak = max(equity_peak, total_pnl)
        stats.max_drawdown = max(stats.max_drawdown, equity_peak - total_pnl)

        logger.info(
            "TICK %03d | MKT bid=%s ask=%s | FV=%.2f | Q bid=%s ask=%s | SPR=%.2fbps | POS=%d | PNL=%.2f | FILLS=%d",
            stats.total_ticks,
            _fmt_px(bid),
            _fmt_px(ask),
            fair,
            _fmt_px(buy_quote),
            _fmt_px(sell_quote),
            mm.last_spread_bps,
            inv_mgr.position,
            total_pnl,
            fill_count,
        )

        if risk_mgr.is_killed:
            logger.error("Risk manager killed strategy. Stopping simulation.")
            break

        time.sleep(1.0)

    avg_spread = sum(spread_samples) / len(spread_samples) if spread_samples else 0.0
    total_fills = stats.buy_fills + stats.sell_fills
    turnover = total_fills
    avg_buy_fill = stats.buy_notional / stats.buy_fills if stats.buy_fills > 0 else 0.0
    avg_sell_fill = stats.sell_notional / stats.sell_fills if stats.sell_fills > 0 else 0.0
    win_rate = (100.0 * stats.roundtrips_win / stats.roundtrips_total) if stats.roundtrips_total > 0 else 0.0
    profit_factor = (stats.gross_profit / stats.gross_loss) if stats.gross_loss > 0 else 0.0

    logger.info("Simulation summary start")
    logger.info("Total ticks processed: %d", stats.total_ticks)
    logger.info("Total quotes calculated: %d", stats.quotes_calculated)
    logger.info("Total quotes skipped: %d", stats.quotes_skipped)
    logger.info("Total fills buy: %d", stats.buy_fills)
    logger.info("Total fills sell: %d", stats.sell_fills)
    logger.info("Total fills: %d", total_fills)
    logger.info("Turnover contracts: %d", turnover)
    logger.info("Average fill buy price: %.2f", avg_buy_fill)
    logger.info("Average fill sell price: %.2f", avg_sell_fill)
    logger.info("Win rate roundtrips: %.2f%%", win_rate)
    logger.info("Profit factor: %.4f", profit_factor)
    logger.info("Max position reached long: %d", stats.max_long_position)
    logger.info("Max position reached short: %d", stats.max_short_position)
    logger.info("Final position: %d", inv_mgr.position)
    logger.info("Realized PnL: %.2f", inv_mgr.realized_pnl)
    logger.info("Unrealized PnL: %.2f", inv_mgr.unrealized_pnl)
    logger.info("Total PnL: %.2f", inv_mgr.realized_pnl + inv_mgr.unrealized_pnl)
    logger.info("Max drawdown: %.2f", stats.max_drawdown)
    logger.info("Average quoted spread (bps): %.2f", avg_spread)
    logger.info("Risk status: %s", risk_mgr.get_status(inv_mgr))
    logger.info("Simulation summary end")


if __name__ == "__main__":
    args = _parse_args()
    explicit_start_price = args.start_price if args.start_price is not None else args.base_price
    run_simulation(
        run_seconds=args.run_seconds,
        ticker=args.ticker,
        start_price=explicit_start_price,
        seed=args.seed,
    )
