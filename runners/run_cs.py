"""Calendar spread runner for DLR MAY26/JUN26 on reMarkets."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _ws_patch  # noqa: F401
import pyRofex

from core.connect import connect
from core.order_manager import send_limit_order
from core.utils import setup_logger
from config.cs_config import CalendarSpreadConfig
from strategies.calendar_spread import CalendarSpreadEngine
from utils.fill_logger import FillLogger
from utils.global_risk import global_risk
from utils.ticker_roller import get_active_ticker

logger = setup_logger("run_cs")

_ART = ZoneInfo("America/Argentina/Buenos_Aires")
_MARKET_OPEN_HOUR = 10
_MARKET_CLOSE_HOUR = 15


# ------------------------------------------------------------------ #
#  Logging setup                                                       #
# ------------------------------------------------------------------ #

def _configure_run_logging(near_ticker: str, far_ticker: str, log_file: str) -> None:
    """Configures UTF-8 file + stdout logging, mirroring run_mm.py style."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    label = f"{near_ticker.replace('/', '-')}_{far_ticker.replace('/', '-')}"
    fmt = logging.Formatter(
        "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    project_root = Path(__file__).resolve().parent.parent
    log_path = project_root / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setFormatter(fmt)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    for obj in logging.Logger.manager.loggerDict.values():
        if isinstance(obj, logging.Logger):
            obj.handlers.clear()
            obj.propagate = True

    logger.info("Logging configured | near=%s | far=%s | file=%s", near_ticker, far_ticker, str(log_path))


# ------------------------------------------------------------------ #
#  Market open gate                                                    #
# ------------------------------------------------------------------ #

def _wait_for_market_open(no_time_check: bool = False) -> None:
    """Waits until 10:00 ART. Exits if > 5 min away (supervisor launched too early)."""
    if no_time_check:
        logger.info("--no-time-check active: skipping market open gate.")
        return

    now = datetime.now(tz=_ART)
    open_today = now.replace(hour=_MARKET_OPEN_HOUR, minute=0, second=0, microsecond=0)
    secs_to_open = (open_today - now).total_seconds()

    if secs_to_open <= 0:
        return

    if secs_to_open > 300:
        logger.warning(
            "Market opens in %.0fs (> 5 min). Exiting — launched too early.", secs_to_open
        )
        sys.exit(0)

    last_log_ts = -999.0
    while True:
        now = datetime.now(tz=_ART)
        remaining = (open_today - now).total_seconds()
        if remaining <= 0:
            break
        mono = time.monotonic()
        if mono - last_log_ts >= 10.0:
            logger.info("Waiting for market open in %.0fs", remaining)
            last_log_ts = mono
        time.sleep(min(1.0, remaining))

    logger.info("Market open. Starting CS loop.")


# ------------------------------------------------------------------ #
#  Watchdog state                                                      #
# ------------------------------------------------------------------ #

class WatchdogState:
    """Thread-safe shared state for market-data watchdog."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_data_ts = time.time()
        self.running = True
        self.stop_event = threading.Event()
        self._reconnect_attempts = 0

    def update(self) -> None:
        with self._lock:
            self.last_data_ts = time.time()

    def elapsed(self) -> float:
        with self._lock:
            return time.time() - self.last_data_ts

    def increment_reconnect(self) -> int:
        with self._lock:
            self._reconnect_attempts += 1
            return self._reconnect_attempts

    def reset_reconnect(self) -> None:
        with self._lock:
            self._reconnect_attempts = 0

    def get_reconnect_attempts(self) -> int:
        with self._lock:
            return self._reconnect_attempts


def _start_watchdog(
    state: WatchdogState,
    reconnect_callback: Any,
    near_ticker: str,
    far_ticker: str,
    warning_threshold: float = 60.0,
    kill_threshold: float = 600.0,
    check_interval: float = 5.0,
) -> threading.Thread:
    """Daemon thread that monitors market-data inactivity with reconnect (≤3 attempts)."""

    def _loop() -> None:
        warning_logged = False
        reconnect_in_progress = False

        while state.running and not state.stop_event.wait(check_interval):
            elapsed = int(state.elapsed())
            attempts = state.get_reconnect_attempts()

            if elapsed > int(kill_threshold):
                logger.critical(
                    "No market data for %ds despite %d reconnect attempts. Exiting.",
                    elapsed, attempts,
                )
                os._exit(1)

            if elapsed > int(warning_threshold) and not warning_logged:
                logger.warning("No market data for %ds. Attempting reconnect...", elapsed)
                warning_logged = True

                if reconnect_callback is not None and not reconnect_in_progress:
                    reconnect_in_progress = True

                    def _try_reconnect() -> None:
                        nonlocal reconnect_in_progress
                        try:
                            attempt_n = state.increment_reconnect()
                            if attempt_n > 3:
                                logger.warning("Max reconnect attempts reached (%d). Waiting for kill.", attempt_n)
                                reconnect_in_progress = False
                                return
                            logger.info("Reconnect attempt %d of 3...", attempt_n)
                            try:
                                pyRofex.close_websocket_connection()
                            except Exception:
                                pass
                            time.sleep(3.0)
                            reconnect_callback()
                            logger.info("Reconnect attempt %d completed.", attempt_n)
                        except Exception as exc:
                            logger.error("Reconnect attempt failed: %s", exc)
                        finally:
                            reconnect_in_progress = False

                    threading.Thread(target=_try_reconnect, name="cs-reconnect", daemon=True).start()

            elif elapsed <= int(warning_threshold) and warning_logged:
                warning_logged = False

    t = threading.Thread(target=_loop, name="cs-watchdog", daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ #
#  L1 extractor                                                        #
# ------------------------------------------------------------------ #

def _extract_l1(message: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Returns (bid, ask, last) from a pyRofex market data message."""
    md = message.get("marketData", {})
    bids = md.get("BI", []) if isinstance(md.get("BI"), list) else []
    offers = md.get("OF", []) if isinstance(md.get("OF"), list) else []
    last_obj = md.get("LA", {})

    bid = bids[0].get("price") if bids else None
    ask = offers[0].get("price") if offers else None
    last = last_obj.get("price") if isinstance(last_obj, dict) else last_obj

    return bid, ask, last


# ------------------------------------------------------------------ #
#  Order execution helpers                                             #
# ------------------------------------------------------------------ #

def _send_spread_orders(
    engine: CalendarSpreadEngine,
    cfg: CalendarSpreadConfig,
    fill_logger_near: FillLogger,  # noqa: ARG001 — kept for signature consistency
    fill_logger_far: FillLogger,   # noqa: ARG001 — fill logging happens in on_order_report
    signal: str,
    pending_orders: dict,
) -> None:
    """Submits both legs of a calendar spread order.

    Fill confirmation is deferred: engine.on_fill() is called only when the
    WebSocket order-report handler receives FILLED status for both legs.
    Orders are tracked via *pending_orders* until then.
    """
    near = cfg.near_ticker
    far = cfg.far_ticker
    qty = cfg.max_contracts
    near_bid = engine._near_bid
    near_ask = engine._near_ask
    far_bid = engine._far_bid
    far_ask = engine._far_ask

    if signal == "SELL_SPREAD":
        # Sell near aggressively (at bid), buy far aggressively (at ask)
        if near_bid is None or far_ask is None:
            logger.warning("SELL_SPREAD: missing prices. Skipping.")
            return
        near_side, far_side = "SELL", "BUY"
        near_price, far_price = near_bid, far_ask
    elif signal == "BUY_SPREAD":
        # Buy near aggressively (at ask), sell far aggressively (at bid)
        if near_ask is None or far_bid is None:
            logger.warning("BUY_SPREAD: missing prices. Skipping.")
            return
        near_side, far_side = "BUY", "SELL"
        near_price, far_price = near_ask, far_bid
    elif signal == "CLOSE":
        pos = engine._position
        if pos > 0:  # Was BUY_SPREAD → close: sell near at bid, buy far at ask
            if near_bid is None or far_ask is None:
                logger.warning("CLOSE (BUY_SPREAD): missing prices. Skipping.")
                return
            near_side, far_side = "SELL", "BUY"
            near_price, far_price = near_bid, far_ask
            qty = engine._entry_qty
        elif pos < 0:  # Was SELL_SPREAD → close: buy near at ask, sell far at bid
            if near_ask is None or far_bid is None:
                logger.warning("CLOSE (SELL_SPREAD): missing prices. Skipping.")
                return
            near_side, far_side = "BUY", "SELL"
            near_price, far_price = near_ask, far_bid
            qty = engine._entry_qty
        else:
            logger.warning("CLOSE signal but position=0. Ignoring.")
            return
    else:
        return

    # Check global risk antes de enviar near leg
    if not global_risk.check(near, near_side, qty):
        logger.warning(
            "Global risk check failed | ticker=%s | side=%s | qty=%d | global_pos=%d",
            near, near_side, qty, global_risk.get_position(near),
        )
        return

    resp_near = send_limit_order(ticker=near, side=near_side, price=near_price, size=qty)
    resp_far = send_limit_order(ticker=far, side=far_side, price=far_price, size=qty)

    # Register pending orders; engine.on_fill() will be called from on_order_report
    # when the WS confirms FILLED status for both legs.
    if resp_near:
        near_client_id = str(resp_near.get("order", {}).get("clientId") or resp_near.get("clientId") or "")
        if near_client_id:
            pending_orders[near_client_id] = {
                "side": near_side,
                "ticker": near,
                "price": near_price,
                "qty": qty,
                "pata": "near",
                "signal": signal,
            }
    if resp_far:
        far_client_id = str(resp_far.get("order", {}).get("clientId") or resp_far.get("clientId") or "")
        if far_client_id:
            pending_orders[far_client_id] = {
                "side": far_side,
                "ticker": far,
                "price": far_price,
                "qty": qty,
                "pata": "far",
                "signal": signal,
            }

    logger.info(
        "Orders submitted | signal=%s | near=%s@%.2f | far=%s@%.2f | qty=%d"
        " | near_ok=%s | far_ok=%s",
        signal, near_side, near_price, far_side, far_price, qty,
        bool(resp_near), bool(resp_far),
    )
    if not resp_near or not resp_far:
        logger.warning(
            "Order issue | near_ok=%s | far_ok=%s | signal=%s",
            bool(resp_near), bool(resp_far), signal,
        )


# ------------------------------------------------------------------ #
#  Main runner                                                         #
# ------------------------------------------------------------------ #

def run_calendar_spread(cfg: CalendarSpreadConfig, no_time_check: bool = False) -> None:
    """Connects, subscribes to WS, and runs the CS event loop."""
    if not connect():
        raise RuntimeError("Connection to reMarkets failed")

    # Resolve tickers with automatic rolover: if base ticker is expired, get next
    base_near = cfg.near_ticker
    base_far = cfg.far_ticker
    near_ticker = get_active_ticker(base_near)
    far_ticker = get_active_ticker(base_far)
    if near_ticker != base_near or far_ticker != base_far:
        logger.info(
            "CS tickers resolved with rollover | near: %s → %s | far: %s → %s",
            base_near, near_ticker, base_far, far_ticker
        )

    engine = CalendarSpreadEngine(cfg)
    session_date = datetime.now().strftime("%Y%m%d")
    fill_logger_near = FillLogger(ticker=near_ticker, session_date=session_date)
    fill_logger_far = FillLogger(ticker=far_ticker, session_date=session_date)

    # Pending orders: client_id → {side, ticker, price, qty, pata, signal}
    # Mutated by _send_spread_orders() and on_order_report() via closure.
    _pending_orders: dict[str, dict] = {}
    # Mutable fill-tracking containers (no nonlocal needed since we mutate, not reassign).
    _filled_flags: list[bool] = [False, False]  # [0]=near, [1]=far
    _last_near_fill: dict = {}
    _last_far_fill: dict = {}

    # Reset any stale PnL/position accumulated from unconfirmed orders.
    engine.reset_pnl()

    # Registrar límite global de posición combinada para near_ticker (compartido con MM)
    global_risk.set_limit(near_ticker, cfg.near_global_limit)
    logger.info(
        "GlobalRisk | near_ticker=%s | near_global_limit=%d",
        near_ticker, cfg.near_global_limit,
    )

    running = True
    watchdog_state = WatchdogState()
    last_history_update_ts = 0.0

    _last_signal: list[str] = ["HOLD"]  # mutable container for closure

    def _subscribe() -> None:
        """(Re)subscribe to WS for both tickers."""
        pyRofex.init_websocket_connection(
            market_data_handler=on_market_data,
            order_report_handler=on_order_report,
            error_handler=on_error,
        )
        pyRofex.market_data_subscription(
            tickers=[near_ticker, far_ticker],
            entries=[
                pyRofex.MarketDataEntry.BIDS,
                pyRofex.MarketDataEntry.OFFERS,
                pyRofex.MarketDataEntry.LAST,
            ],
        )
        logger.info("Subscribed to WS | near=%s | far=%s", near_ticker, far_ticker)

    def on_market_data(message: dict[str, Any]) -> None:
        nonlocal last_history_update_ts

        ticker = message.get("instrumentId", {}).get("symbol", "")
        if ticker not in (near_ticker, far_ticker):
            return

        # Reset watchdog on any valid tick
        attempts = watchdog_state.get_reconnect_attempts()
        if attempts > 0:
            logger.info("Feed restored after %d reconnect attempt(s).", attempts)
            watchdog_state.reset_reconnect()
        watchdog_state.update()

        bid, ask, last = _extract_l1(message)

        if ticker == near_ticker:
            engine.update_prices(near_bid=bid, near_ask=ask, near_last=last,
                                 far_bid=None, far_ask=None, far_last=None)
        else:
            engine.update_prices(near_bid=None, near_ask=None, near_last=None,
                                 far_bid=bid, far_ask=ask, far_last=last)

        # Throttle history update and signal evaluation to scan_interval_seconds
        now_mono = time.monotonic()
        if now_mono - last_history_update_ts >= cfg.scan_interval_seconds:
            last_history_update_ts = now_mono
            engine.update_history()
            state = engine.get_state()
            signal = state["signal"]

            logger.info(
                "CS cycle | near=%.2f | far=%.2f | spread=%.2f | rate=%.1f%% "
                "| zscore=%s | signal=%s | pos=%d | pnl=%.2f",
                state["near_mid"],
                state["far_mid"],
                state["spread"],
                state["implied_rate"],
                f"{state['zscore']:+.2f}" if state["zscore"] is not None else "n/a",
                signal,
                state["position"],
                state["pnl_realized"] + state["pnl_unrealized"],
            )

            if cfg.enable_trading and signal in ("BUY_SPREAD", "SELL_SPREAD", "CLOSE"):
                # Avoid re-sending if last signal is the same action
                if signal != _last_signal[0]:
                    _last_signal[0] = signal
                    _send_spread_orders(engine, cfg, fill_logger_near, fill_logger_far, signal, _pending_orders)
            else:
                _last_signal[0] = signal

    def on_order_report(message: dict[str, Any]) -> None:
        # pyRofex wraps the order fields under "order" key in some versions.
        order = message.get("order", message)
        status = str(order.get("status", "")).upper()
        client_id = str(order.get("clOrdId") or order.get("clientId") or "")

        if status not in {"FILLED", "PARTIALLY_FILLED"}:
            return

        if client_id not in _pending_orders:
            logger.debug(
                "Order report | status=%s | unknown client_id=%s", status, client_id
            )
            return

        pending = _pending_orders.pop(client_id)
        fill_price = float(
            order.get("avgPx") or order.get("lastPx") or pending["price"]
        )
        fill_qty = int(
            order.get("cumQty") or order.get("lastQty") or pending["qty"]
        )

        logger.info(
            "Fill confirmed | pata=%s | side=%s | price=%.2f | qty=%d | signal=%s",
            pending["pata"], pending["side"], fill_price, fill_qty, pending["signal"],
        )

        if pending["pata"] == "near":
            fill_logger_near.log_fill(
                side=pending["side"],
                price=fill_price,
                qty=fill_qty,
                order_id=client_id,
            )
            _last_near_fill.clear()
            _last_near_fill.update(
                {"side": pending["side"], "price": fill_price, "qty": fill_qty}
            )
            _filled_flags[0] = True
        else:
            fill_logger_far.log_fill(
                side=pending["side"],
                price=fill_price,
                qty=fill_qty,
                order_id=client_id,
            )
            _last_far_fill.clear()
            _last_far_fill.update(
                {"side": pending["side"], "price": fill_price, "qty": fill_qty}
            )
            _filled_flags[1] = True

        if _filled_flags[0] and _filled_flags[1]:
            engine.on_fill(
                side_near=_last_near_fill["side"],
                side_far=_last_far_fill["side"],
                price_near=_last_near_fill["price"],
                price_far=_last_far_fill["price"],
                qty=_last_near_fill["qty"],
            )
            _filled_flags[0] = False
            _filled_flags[1] = False
            _last_near_fill.clear()
            _last_far_fill.clear()
            logger.info("Spread fill complete | engine updated")

    def on_error(message: dict[str, Any]) -> None:
        logger.error("WS error: %s", message)

    def _stop_handler(_sig: int, _frame: Any) -> None:
        nonlocal running
        running = False
        logger.info("Stop signal received. Shutting down CS runner.")

    signal.signal(signal.SIGINT, _stop_handler)

    _subscribe()
    logger.info(
        "CS runner started | near=%s | far=%s | trading=%s | scan_interval=%ds",
        near_ticker, far_ticker, cfg.enable_trading, cfg.scan_interval_seconds,
    )

    _start_watchdog(
        state=watchdog_state,
        reconnect_callback=_subscribe,
        near_ticker=near_ticker,
        far_ticker=far_ticker,
    )

    # Keep alive while WebSocket callbacks handle the logic
    while running:
        now_art = datetime.now(tz=_ART)
        if not no_time_check and now_art.hour >= _MARKET_CLOSE_HOUR:
            logger.info("Market close hour reached. Stopping CS runner.")
            running = False
            break
        time.sleep(1.0)

    watchdog_state.running = False
    watchdog_state.stop_event.set()
    try:
        pyRofex.close_websocket_connection()
    except Exception:
        pass
    logger.info("CS runner finished.")


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calendar Spread runner for DLR futures")
    parser.add_argument("--near", default="DLR/MAY26", help="Near-leg ticker (default: DLR/MAY26)")
    parser.add_argument("--far", default="DLR/JUN26", help="Far-leg ticker (default: DLR/JUN26)")
    parser.add_argument(
        "--no-time-check",
        action="store_true",
        default=False,
        help="Disable market hours check (for testing)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cfg = CalendarSpreadConfig(
        near_ticker=args.near,
        far_ticker=args.far,
    )
    _configure_run_logging(cfg.near_ticker, cfg.far_ticker, cfg.log_file)
    _wait_for_market_open(no_time_check=args.no_time_check)
    run_calendar_spread(cfg, no_time_check=args.no_time_check)
    sys.exit(0)  # Force exit if pyRofex WS thread keeps process alive


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("CS runner stopped by user.")
        sys.exit(0)
