"""Run script for market making bot on reMarkets (DLR / CAUC)."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ensure project root is in sys.path for absolute imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _ws_patch  # noqa: F401
import pyRofex

from core.connect import connect
from core import credentials as creds
from core.instruments import get_all_instruments, filter_instruments
from core.market_data import get_snapshot
from core.order_manager import cancel_order, get_order_status, send_limit_order
from config.mm_config import MMConfig, InstrumentConfig
from strategies import FairValueCalculator, InventoryManager, MarketMaker, RiskManager
from strategies.mm_risk import MMRiskConfig, MMRiskManager
from core.utils import setup_logger
from utils.fill_logger import FillLogger
from utils.heartbeat import write as _hb_write
from utils.ticker_roller import get_active_ticker

logger = setup_logger("run_mm")

_BOTS_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "bots_state.json"
_CB_MAX_REALIZED_LOSS_ARS = -200.0
_CB_MAX_RISK_BREACHES_60MIN = 100
_CB_RISK_BREACH_WINDOW_SEC = 60.0 * 60.0
_CB_MAX_POS_DRIFT = 1
_CB_DRIFT_CHECK_INTERVAL_SEC = 300.0


def _resolve_bot_id_for_state(instrument: InstrumentConfig, ticker: str) -> str | None:
    """Best-effort mapping from instrument/ticker to dashboard bot_id."""
    pattern = (instrument.ticker_pattern or "").upper()
    tk = (ticker or "").upper()
    if pattern == "DLR":
        return "mm_dlr_mini" if tk.endswith("M") else "mm_dlr"
    if pattern == "SOJ.ROS":
        return "mm_soj"
    if pattern == "SOJ.MIN":
        return "mm_soj_mini"
    return None


def _merge_skew_state(bot_id: str | None, skew_state: dict[str, Any]) -> None:
    """Merges skew observability fields into shared bots_state registry file."""
    if not bot_id:
        return

    try:
        _BOTS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _BOTS_STATE_FILE.exists():
            raw = _BOTS_STATE_FILE.read_text(encoding="utf-8").strip()
            state: dict[str, Any] = json.loads(raw) if raw else {}
            if not isinstance(state, dict):
                state = {}
        else:
            state = {}

        entry = state.get(bot_id, {})
        if not isinstance(entry, dict):
            entry = {}
        entry.update(skew_state)
        state[bot_id] = entry
        _BOTS_STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to merge skew_state for %s: %s", bot_id, exc)


class _TickerFilter(logging.Filter):
    """Injects active ticker into each log record."""

    def __init__(self, ticker: str) -> None:
        super().__init__()
        self._ticker = ticker

    def filter(self, record: logging.LogRecord) -> bool:
        record.ticker = self._ticker
        return True


def _configure_run_logging_for_ticker(ticker: str) -> None:
    """Configures UTF-8 file/stdout handlers once with stable format and ticker tag."""
    safe_ticker = ticker.replace("/", "-")
    project_root = Path(__file__).resolve().parent.parent
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_path = logs_dir / _ticker_log_file_name(ticker)

    # Force UTF-8 on stdout when runtime supports reconfigure (Py3.7+).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    fmt = logging.Formatter(
        "[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(ticker)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ticker_filter = _TickerFilter(safe_ticker)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicated lines after reconfiguration.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(ticker_filter)

    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(ticker_filter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    # Route all named loggers through root so formatting/ticker are uniform.
    logger_dict = logging.Logger.manager.loggerDict
    for obj in logger_dict.values():
        if isinstance(obj, logging.Logger):
            obj.handlers.clear()
            obj.propagate = True

    logger.info("Logging configured | file=%s", str(file_path))


class WatchdogState:
    """Thread-safe shared state for market-data watchdog."""

    def __init__(self, warning_threshold: float = 60.0, kill_threshold: float = 600.0) -> None:
        self.lock = threading.Lock()
        self.last_data_ts = time.time()
        self.running = True
        self.stop_event = threading.Event()
        self.reconnect_attempts = 0
        self.reconnect_event = threading.Event()

    def update(self) -> None:
        """Stores timestamp of latest valid market-data tick."""
        with self.lock:
            self.last_data_ts = time.time()

    def elapsed(self) -> float:
        """Returns seconds elapsed since last market-data update."""
        with self.lock:
            return time.time() - self.last_data_ts

    def increment_reconnect(self) -> int:
        """Increments reconnect attempt counter. Returns new count."""
        with self.lock:
            self.reconnect_attempts += 1
            return self.reconnect_attempts

    def reset_reconnect(self) -> None:
        """Resets reconnect counter (feed restored)."""
        with self.lock:
            self.reconnect_attempts = 0

    def get_reconnect_attempts(self) -> int:
        """Returns current reconnect attempt count."""
        with self.lock:
            return self.reconnect_attempts


def start_watchdog(
    state: WatchdogState,
    warning_threshold: float,
    kill_threshold: float,
    check_interval: float = 5.0,
    reconnect_callback: Any | None = None,
    selected_ticker: str = "",
) -> threading.Thread:
    """Starts a daemon thread that monitors market-data inactivity with reconnect.

    Params:
      state: WatchdogState with last_data_ts tracking
      warning_threshold: log WARNING if silent for this many seconds (60)
      kill_threshold: absolute cutoff for dead feed (600)
      check_interval: check interval in seconds (5)
      reconnect_callback: async function called to reconnect feed (takes selected_ticker)
      selected_ticker: ticker name for logging/reconnect
    """

    def _should_cancel_quotes(mm_obj: Any | None, min_quote_age_seconds: float = 10.0) -> bool:
        """Cancels quotes only when they are stale enough to avoid queue churn."""
        if mm_obj is None:
            return False
        try:
            last_quote_ts = float(getattr(mm_obj, "_last_quote_ts", 0.0) or 0.0)
        except Exception:
            last_quote_ts = 0.0
        if last_quote_ts <= 0.0:
            return True
        return (time.time() - last_quote_ts) >= float(min_quote_age_seconds)

    def _watchdog_loop() -> None:
        warning_logged = False
        reconnect_in_progress = False
        last_reconnect_attempt_ts = time.time()

        while state.running and not state.stop_event.wait(check_interval):
            elapsed = int(state.elapsed())
            current_reconnect_attempts = state.get_reconnect_attempts()

            # Absolute cutoff: 600s without any tick + reconexión failed or exhausted
            if elapsed > int(kill_threshold):
                if current_reconnect_attempts > 0:
                    logger.critical(
                        "No market data for %ds despite %d reconnect attempts. "
                        "Feed appears dead. Exiting for supervisor restart.",
                        elapsed,
                        current_reconnect_attempts,
                    )
                else:
                    logger.critical(
                        "No market data for %ds. Feed appears dead. Exiting for supervisor restart.",
                        elapsed,
                    )
                # Cancel orders before exit
                try:
                    if hasattr(state, "_mm_context"):
                        mm_obj = state._mm_context.get("mm")
                        if _should_cancel_quotes(mm_obj):
                            mm_obj.cancel_existing_quotes()
                except Exception as exc:
                    logger.warning("Failed to cancel orders before exit: %s", exc)
                os._exit(1)

            # Trigger reconnect attempt after ~60s of silence
            if elapsed > int(warning_threshold) and not warning_logged:
                logger.warning(
                    "No market data for %ds. Attempting reconnect...",
                    elapsed,
                )
                warning_logged = True

                if reconnect_callback is not None and not reconnect_in_progress:
                    reconnect_in_progress = True
                    last_reconnect_attempt_ts = time.time()

                    def _try_reconnect() -> None:
                        nonlocal reconnect_in_progress
                        try:
                            attempts = state.increment_reconnect()
                            if attempts > 3:
                                logger.warning(
                                    "Reconnect attempt %d exceeds max (3). Will wait for absolute timeout.",
                                    attempts,
                                )
                                reconnect_in_progress = False
                                return

                            logger.info("Reconnect attempt %d of 3...", attempts)

                            # Cancel orders before reconnecting
                            if hasattr(state, "_mm_context"):
                                mm = state._mm_context.get("mm")
                                if mm:
                                    try:
                                        if _should_cancel_quotes(mm):
                                            mm.cancel_existing_quotes()
                                        else:
                                            logger.info(
                                                "Feed drop: keeping live quotes (last quote < 10s) to preserve queue priority."
                                            )
                                    except Exception as exc:
                                        logger.warning("Failed to cancel orders before reconnect: %s", exc)

                            # Close existing connection
                            try:
                                pyRofex.close_websocket_connection()
                            except Exception as exc:
                                logger.debug("Error closing existing WS (may not be open): %s", exc)

                            time.sleep(3.0)

                            # Reconnect via callback
                            reconnect_callback(selected_ticker)
                            logger.info("Reconnect attempt %d completed.", attempts)
                        except Exception as exc:
                            logger.error("Reconnect attempt failed: %s", exc)
                        finally:
                            reconnect_in_progress = False

                    # Run reconnect in background thread to not block watchdog
                    reconnect_thread = threading.Thread(
                        target=_try_reconnect,
                        name="reconnect-worker",
                        daemon=True,
                    )
                    reconnect_thread.start()

            elif elapsed <= int(warning_threshold) and warning_logged:
                # Feed has recovered; reset warning flag
                warning_logged = False

            # Check if we should wait before next reconnect attempt
            if current_reconnect_attempts > 0 and not reconnect_in_progress:
                time_since_last_attempt = time.time() - last_reconnect_attempt_ts
                if time_since_last_attempt < 10.0:
                    # Space out reconnect attempts by ~10s

                    pass

    thread = threading.Thread(target=_watchdog_loop, name="md-watchdog", daemon=True)
    thread.start()
    return thread


def _ticker_log_file_name(ticker: str) -> str:
    """Builds per-ticker log file name, replacing '/' with '_' and lowercasing."""
    safe = ticker.replace("/", "_").lower()
    return f"run_mm_{safe}.log"


def _configure_ticker_file_logging(ticker: str) -> None:
    """Adds a direct file handler for ticker-specific runs in interactive executions."""
    # If stdout is already redirected by a launcher, avoid duplicated log lines.
    if not sys.stdout.isatty():
        return

    project_root = Path(__file__).resolve().parent.parent
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_path = logs_dir / _ticker_log_file_name(ticker)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(getattr(handler, "baseFilename", "")) == file_path:
            return

    file_handler = logging.FileHandler(file_path, encoding="utf-8")
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.INFO)
    logger.info("Ticker file logging enabled | file=%s", str(file_path))


def _extract_top_levels(
    message: dict[str, Any],
) -> tuple[float | None, float | None, float | None, int, int, int, int]:
    """Extracts BBO, last, and L2 size info from pyRofex market data message.

    Returns:
        bid, ask, last,
        bid_size_l1  -- size at best bid (level 0)
        ask_size_l1  -- size at best ask (level 0)
        bid_depth    -- cumulative size of top-3 bid levels
        ask_depth    -- cumulative size of top-3 ask levels
    """
    md = message.get("marketData", {})

    bids = md.get("BI", []) if isinstance(md.get("BI"), list) else []
    offers = md.get("OF", []) if isinstance(md.get("OF"), list) else []
    last_obj = md.get("LA", {})

    bid = bids[0].get("price") if bids else None
    ask = offers[0].get("price") if offers else None
    if isinstance(last_obj, dict):
        last = last_obj.get("price")
    else:
        last = last_obj

    bid_size_l1 = max(int(bids[0].get("size") or 1), 1) if bids else 1
    ask_size_l1 = max(int(offers[0].get("size") or 1), 1) if offers else 1
    bid_depth = max(sum(int(b.get("size") or 0) for b in bids[:3]), 1)
    ask_depth = max(sum(int(o.get("size") or 0) for o in offers[:3]), 1)

    return bid, ask, last, bid_size_l1, ask_size_l1, bid_depth, ask_depth


def _ticker_exists(ticker: str, instruments: list[dict[str, Any]]) -> bool:
    """Returns True if ticker exists in instrument list."""
    for inst in instruments:
        symbol = inst.get("instrumentId", {}).get("symbol")
        if symbol == ticker:
            return True
    return False


def _wait_for_market_open(instrument: InstrumentConfig) -> None:
    """Waits until market_open_hour:market_open_minute (ART timezone).

    - If > 300 s away: log WARNING and exit (supervisor launched too early).
    - If 1-300 s away: loop with a log every 10 s, then continue.
    - If already open (<=0 s): return immediately.
    """
    _ART = ZoneInfo("America/Argentina/Buenos_Aires")
    now = datetime.now(tz=_ART)
    open_today = now.replace(
        hour=instrument.market_open_hour,
        minute=instrument.market_open_minute,
        second=0,
        microsecond=0,
    )
    secs_to_open = (open_today - now).total_seconds()

    if secs_to_open <= 0:
        # Market already open — continue straight away.
        return

    if secs_to_open > 300:
        logger.warning(
            "Market opens in %.0fs (more than 5 min away). "
            "Exiting — supervisor should not have launched this early.",
            secs_to_open,
        )
        sys.exit(0)

    # Between 1 and 300 seconds: wait with periodic status messages.
    last_log_ts = -999.0
    while True:
        now = datetime.now(tz=_ART)
        remaining = (open_today - now).total_seconds()
        if remaining <= 0:
            break
        now_mono = time.monotonic()
        if now_mono - last_log_ts >= 10.0:
            logger.info("Waiting for market open in %.0fs", remaining)
            last_log_ts = now_mono
        time.sleep(min(1.0, remaining))

    logger.info("Market open. Starting MM loop.")


def _wait_for_valid_snapshot(
    ticker: str,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 1.0,
) -> tuple[float | None, float | None, float | None]:
    """Polls snapshot until both bid and ask are available or timeout expires."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        snap = get_snapshot(ticker)
        if snap:
            bid = snap.get("bid_price")
            ask = snap.get("ask_price")
            last = snap.get("last")
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                return bid, ask, last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_seconds, remaining))
    return None, None, None


def _flatten_inherited_position_before_quoting(
    ticker: str,
    inherited_position: int,
    inv_mgr: InventoryManager,
    fill_logger: FillLogger,
) -> None:
    """Attempts to flatten inherited position before the MM starts quoting."""
    if inherited_position == 0:
        logger.info("[INFO] No inherited position. Starting fresh.")
        return

    logger.warning(
        "[WARNING] Inherited position detected | pos=%d | Will flatten before quoting.",
        inherited_position,
    )

    bid, ask, _last = _wait_for_valid_snapshot(ticker=ticker, timeout_seconds=30.0)
    if bid is None or ask is None:
        logger.warning(
            "[WARNING] No valid snapshot within 30s. Starting MM with inherited position | pos=%d",
            inv_mgr.position,
        )
        return

    side = "SELL" if inherited_position > 0 else "BUY"
    qty = abs(inherited_position)
    price = float(bid) if inherited_position > 0 else float(ask)
    logger.warning(
        "[WARNING] Inherited flatten order sent | pos=%d | side=%s | price=%.2f",
        inherited_position,
        side,
        price,
    )

    response = send_limit_order(ticker=ticker, side=side, price=price, size=qty)
    if not response:
        logger.warning(
            "[WARNING] Flatten not confirmed, starting MM with inherited position | pos=%d",
            inv_mgr.position,
        )
        return

    order = response.get("order", {})
    order_id = str(order.get("clientId") or response.get("clientId") or "")
    proprietary = response.get("proprietary") or order.get("proprietary")
    accounted_qty = 0
    deadline = time.monotonic() + 20.0

    while time.monotonic() < deadline and order_id:
        status_resp = get_order_status(order_id, proprietary=proprietary)
        order_state = status_resp.get("order", {}) if status_resp else {}
        status = str(order_state.get("status") or status_resp.get("status") or "").upper()
        cum_qty_raw = order_state.get("cumQty") or status_resp.get("cumQty") or 0
        avg_px_raw = order_state.get("avgPx") or order_state.get("price") or status_resp.get("price") or price

        try:
            cum_qty = int(float(cum_qty_raw))
        except (TypeError, ValueError):
            cum_qty = 0
        try:
            avg_px = float(avg_px_raw)
        except (TypeError, ValueError):
            avg_px = float(price)

        if cum_qty > accounted_qty:
            delta_qty = cum_qty - accounted_qty
            inv_mgr.on_fill(side=side, price=avg_px, size=delta_qty)
            fill_logger.log_fill(
                side=side,
                price=avg_px,
                qty=delta_qty,
                order_id=order_id,
            )
            accounted_qty = cum_qty

        if status == "FILLED" or accounted_qty >= qty or inv_mgr.position == 0:
            logger.info("[INFO] Inherited position flattened successfully | pos=0")
            return

        time.sleep(1.0)

    if order_id:
        cancel_order(order_id, proprietary=proprietary)
    logger.warning(
        "[WARNING] Flatten not confirmed, starting MM with inherited position | pos=%d",
        inv_mgr.position,
    )


def _parse_expiry(symbol: str) -> tuple[int, int] | None:
    """Parses INSTR/MESYY ticker and returns (year, month) when valid."""
    month_map = {
        "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
    }
    if "/" not in symbol:
        return None
    token = symbol.split("/", 1)[1].strip().upper()
    if len(token) < 5:
        return None
    month = month_map.get(token[:3])
    year_raw = token[3:5]
    if month is None or not year_raw.isdigit():
        return None
    return (2000 + int(year_raw), month)


def _is_not_expired(symbol: str) -> bool:
    """Returns True when ticker expiry is current month or in the future."""
    expiry = _parse_expiry(symbol)
    if expiry is None:
        return False
    now = datetime.now()
    return (expiry[0], expiry[1]) >= (now.year, now.month)


def _auto_select_ticker(instrument: InstrumentConfig) -> str:
    """Selects most liquid non-expired future matching instrument pattern."""
    pattern = instrument.ticker_pattern
    try:
        all_insts = filter_instruments(pattern)

        def _is_clean_outright(sym: str) -> bool:
            """True for plain outright futures (e.g. DLR/ABR26), not month code M."""
            token = sym.split("/", 1)[1] if "/" in sym else ""
            return bool(token) and (not token.endswith("M"))

        candidates = []
        for inst in all_insts:
            sym = inst.get("instrumentId", {}).get("symbol", "")
            cfi = inst.get("cficode", "")
            if not sym or not sym.startswith(pattern + "/"):
                continue
            # Accept any futures cficode and only exclude options (OP*)
            if cfi and cfi.startswith("OP"):
                continue
            # Exclude option/spread-like symbols with embedded spaces
            if " " in sym:
                continue
            # Exclude calendar spreads (more than one "/")
            if sym.count("/") > 1:
                continue
            # Exclude adjustment contracts ending in A (e.g. DLR/ABR26A)
            token_after_slash = sym.split("/", 1)[1] if "/" in sym else ""
            if token_after_slash.endswith("A"):
                continue
            if not _is_not_expired(sym):
                continue
            candidates.append(sym)

        # Fallback candidate build without cficode filtering
        if not candidates:
            for inst in all_insts:
                sym = inst.get("instrumentId", {}).get("symbol", "")
                if not sym or not sym.startswith(pattern + "/"):
                    continue
                if " " in sym:
                    continue
                if sym.count("/") > 1:
                    continue
                token_after_slash = sym.split("/", 1)[1] if "/" in sym else ""
                if token_after_slash.endswith("A"):
                    continue
                if _is_not_expired(sym):
                    candidates.append(sym)

        # Prioritize nearest non-expired expiries first
        def sort_key(sym: str) -> tuple[int, int]:
            expiry = _parse_expiry(sym)
            if expiry is None:
                return (9999, 12)
            return expiry

        candidates.sort(key=sort_key)
        candidates = candidates[:5]

        if not candidates:
            now = datetime.now()
            month_map_rev = {
                1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
                7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
            }
            fallback_month = month_map_rev.get(now.month, "ABR")
            fallback_year = str(now.year)[2:]
            fallback = f"{pattern}/{fallback_month}{fallback_year}"
            logger.warning("Auto-select found no valid %s candidates. Using fallback %s", pattern, fallback)
            return fallback

        best_ticker = candidates[0]
        best_bid = None
        best_ask = None
        best_score = (-1, -1.0)
        all_scores_zero = True

        for ticker in candidates:
            snap = get_snapshot(ticker)
            if not snap:
                continue
            bid = snap.get("bid_price")
            ask = snap.get("ask_price")
            last = snap.get("last")
            bid_size = float(snap.get("bid_size") or 0.0)
            has_both = 1 if (bid is not None and ask is not None and bid > 0 and ask > 0) else 0
            has_last = 0.5 if (last is not None and last > 0) else 0
            score = (has_both, bid_size + has_last)
            logger.info(
                "Candidate %s | bid=%s ask=%s last=%s | score=%s",
                ticker,
                bid,
                ask,
                last,
                score,
            )
            if score != (0, 0.0):
                all_scores_zero = False
            if score > best_score:
                best_score = score
                best_ticker = ticker
                best_bid = bid
                best_ask = ask

        # Before opening, scores are often all zero. Prefer nearest clean outright
        # (e.g. DLR/ABR26 over DLR/ABR26M) while keeping nearest-expiry ordering.
        if all_scores_zero:
            clean_candidates = [sym for sym in candidates if _is_clean_outright(sym)]
            if clean_candidates:
                best_ticker = clean_candidates[0]
                best_bid = None
                best_ask = None

        logger.info(
            "Auto-selected ticker: %s (bid=%s, ask=%s)",
            best_ticker,
            best_bid,
            best_ask,
        )
        return best_ticker
    except Exception as exc:
        now = datetime.now()
        month_map_rev = {
            1: "ENE", 2: "FEB", 3: "MAR", 4: "ABR", 5: "MAY", 6: "JUN",
            7: "JUL", 8: "AGO", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DIC",
        }
        fallback_month = month_map_rev.get(now.month, "ABR")
        fallback_year = str(now.year)[2:]
        fallback = f"{pattern}/{fallback_month}{fallback_year}"
        logger.error("Auto-select ticker failed: %s. Using fallback %s", exc, fallback)
        return fallback


def run_market_maker(
    config: MMConfig,
    instrument: InstrumentConfig,
    run_seconds: int = 0,
    forced_ticker: str | None = None,
) -> None:
    """Runs the market maker for the configured instrument."""
    if not connect():
        raise RuntimeError("Connection to reMarkets failed")

    all_instruments = get_all_instruments()
    base_ticker = forced_ticker if forced_ticker else _auto_select_ticker(instrument)
    selected_ticker = base_ticker

    try:
        active_ticker = get_active_ticker(base_ticker)
        if active_ticker != base_ticker:
            logger.warning(
                "Ticker rollover | original=%s | active=%s",
                base_ticker,
                active_ticker,
            )
        selected_ticker = active_ticker
    except Exception as exc:
        logger.error(
            "Ticker rollover failed | ticker=%s | error=%s | using_original",
            base_ticker,
            exc,
        )

    if forced_ticker:
        logger.info("Using forced ticker: %s", selected_ticker)
    if not _ticker_exists(selected_ticker, all_instruments):
        logger.error("Forced/configured ticker not found in instruments: %s", selected_ticker)
        raise RuntimeError(f"Configured/selected ticker not found: {selected_ticker}")

    _configure_run_logging_for_ticker(selected_ticker)
    _wait_for_market_open(instrument)
    fill_logger = FillLogger(
        ticker=selected_ticker,
        session_date=datetime.now().strftime("%Y%m%d"),
    )

    recovered_position = 0
    max_inventory_recovery = int(getattr(instrument, "max_inventory", 10) or 10)
    try:
        fills_today = fill_logger.get_session_fills()
        if fills_today:
            logger.info(
                "Found %d fills from today. Recovering position. | csv=%s",
                len(fills_today),
                str(fill_logger.file_path),
            )
            recovered_position = int(fill_logger.get_net_position())
            logger.info(
                "Recovered position from fills | ticker=%s | pos=%d",
                selected_ticker,
                recovered_position,
            )
        else:
            logger.info("No fills today. Starting fresh with position=0.")
    except Exception as exc:
        logger.warning("Could not read fills CSV: %s. Starting fresh.", exc)
        recovered_position = 0

    if recovered_position < -max_inventory_recovery or recovered_position > max_inventory_recovery:
        logger.warning(
            "Recovered position out of range | ticker=%s | recovered_pos=%d | allowed=[%d,%d] | fallback=0",
            selected_ticker,
            recovered_position,
            -max_inventory_recovery,
            max_inventory_recovery,
        )
        recovered_position = 0

    fv_calc = FairValueCalculator(
        alpha=instrument.ema_alpha,
        tick_size=instrument.tick_size,
        direct_midpoint_max_ticks=instrument.direct_midpoint_max_ticks,
    )
    inv_mgr = InventoryManager(
        max_position=instrument.max_position,
        skew_factor_bps=instrument.inventory_skew_factor,
    )
    risk_mgr = RiskManager(
        max_daily_loss=instrument.max_daily_loss,
        max_position=instrument.max_position,
    )
    mm_risk_config = MMRiskConfig(
        VOL_CIRCUIT_BREAKER_BPS=50.0,
        VOL_RESUME_BPS=30.0,
        MAX_DAILY_LOSS_ARS=instrument.max_daily_loss,
        MAX_POSITION_HOLD_SECONDS=600,
        VOL_WIDEN_THRESHOLD_BPS=20.0,
    )
    mm_risk = MMRiskManager(config=mm_risk_config)

    mm = MarketMaker(
        config=config,
        fair_value_calc=fv_calc,
        inventory_mgr=inv_mgr,
        risk_mgr=risk_mgr,
        mm_risk=mm_risk,
        instrument=instrument,
    )
    mm.set_fill_logger(fill_logger)
    mm.set_initial_position(recovered_position)
    _flatten_inherited_position_before_quoting(
        ticker=selected_ticker,
        inherited_position=recovered_position,
        inv_mgr=inv_mgr,
        fill_logger=fill_logger,
    )
    state_bot_id = _resolve_bot_id_for_state(instrument, selected_ticker)

    snap = get_snapshot(selected_ticker)
    if snap:
        bid = snap.get("bid_price")
        ask = snap.get("ask_price")
        last = snap.get("last")
        fv = fv_calc.update(bid=bid, ask=ask, last=last)
        logger.info(
            "Initial snapshot | ticker=%s | bid=%s | ask=%s | last=%s | fair=%.2f",
            selected_ticker,
            bid,
            ask,
            last,
            fv,
        )

    running = True
    cycles_count = 0
    fill_buy_qty = 0
    fill_sell_qty = 0
    fill_buy_notional = 0.0
    fill_sell_notional = 0.0
    ws_fill_events = 0
    ws_cum_filled_by_order: dict[str, int] = {}
    eod_closeout_mode = False
    risk_breach_timestamps: deque[float] = deque()
    last_drift_check_ts = 0.0

    warning_threshold = float(getattr(config, "MD_WARNING_SECONDS", 60.0))
    kill_threshold = float(getattr(config, "MD_KILL_SECONDS", 600.0))
    check_interval = float(getattr(config, "MD_WATCHDOG_CHECK_INTERVAL_SECONDS", 5.0))
    watchdog_state = WatchdogState(
        warning_threshold=warning_threshold,
        kill_threshold=kill_threshold,
    )

    def _stop_handler(_sig: int, _frame: Any) -> None:
        nonlocal running
        running = False
        logger.info("Stop signal received. Shutting down bot loop.")

    signal.signal(signal.SIGINT, _stop_handler)

    def _check_circuit_breakers(now_ts: float) -> tuple[bool, str | None]:
        nonlocal last_drift_check_ts

        # 1) Hard realized PnL guard for production protection.
        if inv_mgr.realized_pnl <= _CB_MAX_REALIZED_LOSS_ARS:
            return False, (
                "PnL guard breached | realized_pnl=%.2f <= %.2f"
                % (inv_mgr.realized_pnl, _CB_MAX_REALIZED_LOSS_ARS)
            )

        # 2) Risk breach storm: excessive breach signals inside rolling 60 minutes.
        current_total_pnl = float(inv_mgr.realized_pnl + inv_mgr.unrealized_pnl)
        risk_breach_now = (
            risk_mgr.is_killed
            or abs(int(inv_mgr.position)) > int(risk_mgr.max_position)
            or current_total_pnl < -float(risk_mgr.max_daily_loss)
        )
        if risk_breach_now:
            risk_breach_timestamps.append(now_ts)

        cutoff_ts = now_ts - _CB_RISK_BREACH_WINDOW_SEC
        while risk_breach_timestamps and risk_breach_timestamps[0] < cutoff_ts:
            risk_breach_timestamps.popleft()

        if len(risk_breach_timestamps) > _CB_MAX_RISK_BREACHES_60MIN:
            return False, (
                "Risk breach storm | breaches_60min=%d > %d"
                % (len(risk_breach_timestamps), _CB_MAX_RISK_BREACHES_60MIN)
            )

        # 3) Position drift versus persisted fills (CSV), checked every 5 minutes.
        if (now_ts - last_drift_check_ts) >= _CB_DRIFT_CHECK_INTERVAL_SEC:
            try:
                csv_position = int(fill_logger.get_net_position())
                drift = abs(int(inv_mgr.position) - csv_position)
                if drift > _CB_MAX_POS_DRIFT:
                    return False, (
                        "Position drift vs CSV | inv_pos=%d csv_pos=%d drift=%d > %d"
                        % (inv_mgr.position, csv_position, drift, _CB_MAX_POS_DRIFT)
                    )
            except Exception as exc:
                logger.warning("Circuit breaker drift check failed: %s", exc)
            finally:
                last_drift_check_ts = now_ts

        return True, None

    def on_market_data(message: dict[str, Any]) -> None:
        nonlocal cycles_count, eod_closeout_mode
        if message.get("instrumentId", {}).get("symbol") != selected_ticker:
            return
        # Tick arrived: reset reconnect counter and update last_data_ts
        current_attempts = watchdog_state.get_reconnect_attempts()
        if current_attempts > 0:
            logger.info(
                "Feed restored after %d reconnect attempt(s). Resuming normal operation.",
                current_attempts,
            )
            watchdog_state.reset_reconnect()
        watchdog_state.update()
        if eod_closeout_mode:
            return
        bid, ask, last, bid_size_l1, ask_size_l1, bid_depth, ask_depth = _extract_top_levels(message)
        mm.on_market_data(
            ticker=selected_ticker,
            bid=bid,
            ask=ask,
            last=last,
            bid_size_l1=bid_size_l1,
            ask_size_l1=ask_size_l1,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
        )
        cycles_count += 1

    def on_order_report(message: dict[str, Any]) -> None:
        nonlocal fill_buy_qty, fill_sell_qty, fill_buy_notional, fill_sell_notional, ws_fill_events
        order = message.get("order", message)
        status = str(order.get("status") or message.get("status") or "").upper()
        side = str(order.get("side") or message.get("side") or "").upper()
        ws_client_id = str(order.get("clOrdId") or order.get("clientId") or message.get("clOrdId") or message.get("clientId") or "")
        ws_order_id = str(order.get("orderId") or message.get("orderId") or "")
        order_key = ws_client_id or ws_order_id

        if ws_client_id:
            mm._clear_order_tracking(ws_client_id, status)

        if status not in {"FILLED", "PARTIALLY_FILLED"}:
            if order_key and status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                ws_cum_filled_by_order.pop(order_key, None)
            return

        try:
            cum_qty_i = int(float(order.get("cumQty") or message.get("cumQty") or 0))
        except (TypeError, ValueError):
            cum_qty_i = 0
        try:
            last_qty_i = int(float(order.get("lastQty") or message.get("lastQty") or 0))
        except (TypeError, ValueError):
            last_qty_i = 0

        prev_accounted = ws_cum_filled_by_order.get(order_key, 0)
        if cum_qty_i > 0:
            delta_qty = max(0, cum_qty_i - prev_accounted)
            new_accounted = max(prev_accounted, cum_qty_i)
        else:
            delta_qty = max(0, last_qty_i)
            new_accounted = prev_accounted + delta_qty

        if delta_qty <= 0:
            return

        try:
            px_f = float(
                order.get("lastPx")
                or order.get("avgPx")
                or order.get("price")
                or message.get("lastPx")
                or message.get("price")
                or 0
            )
        except (TypeError, ValueError):
            px_f = 0.0

        if side not in {"BUY", "SELL"} or px_f <= 0:
            logger.warning(
                "Order report fill ignored | status=%s | side=%s | price=%s | key=%s",
                status,
                side,
                px_f,
                order_key,
            )
            return

        if order_key:
            ws_cum_filled_by_order[order_key] = new_accounted

        inv_mgr.on_fill(side=side, price=px_f, size=delta_qty)
        fill_logger.log_fill(
            side=side,
            price=px_f,
            qty=delta_qty,
            order_id=order_key,
        )
        if side == "BUY":
            fill_buy_qty += delta_qty
            fill_buy_notional += px_f * delta_qty
        else:
            fill_sell_qty += delta_qty
            fill_sell_notional += px_f * delta_qty

        logger.info(
            "Fill update | side=%s | delta_qty=%d | cum_qty=%d | px=%.2f | pos=%d | realized=%.2f | id=%s",
            side,
            delta_qty,
            new_accounted,
            px_f,
            inv_mgr.position,
            inv_mgr.realized_pnl,
            order_key,
        )
        ws_fill_events += 1
        logger.info(
            "[FILL_VALIDATION_WS] order_id=%s side=%s price=%.2f qty=%d cum=%d status=%s",
            order_key,
            side,
            px_f,
            delta_qty,
            new_accounted,
            status,
        )

        if order_key and status == "FILLED":
            ws_cum_filled_by_order.pop(order_key, None)

    def on_error(message: dict[str, Any]) -> None:
        logger.error("WS error: %s", message)

    pyRofex.init_websocket_connection(
        market_data_handler=on_market_data,
        order_report_handler=on_order_report,
        error_handler=on_error,
    )
    account_id = creds.ACCOUNT
    try:
        pyRofex.order_report_subscription(account=account_id)
        logger.info("[DEBUG_WS] Handler order_report registrado. Suscripto: True (account=%s)", account_id)
    except Exception as exc:
        logger.error("[DEBUG_WS] Suscripci\u00f3n order_report fall\u00f3 | account=%s | error=%s", account_id, exc)
    pyRofex.market_data_subscription(
        tickers=[selected_ticker],
        entries=[
            pyRofex.MarketDataEntry.BIDS,
            pyRofex.MarketDataEntry.OFFERS,
            pyRofex.MarketDataEntry.LAST,
        ],
    )
    logger.info(
        "MM started | instrument=%s | ticker=%s | refresh=%.2fs | trading_enabled=%s | run_seconds=%d",
        instrument.name,
        selected_ticker,
        config.QUOTE_REFRESH_SECONDS,
        config.ENABLE_TRADING,
        run_seconds,
    )

    def _reconnect_feed(ticker: str) -> None:
        """Callback to reconnect the WebSocket feed (called from watchdog)."""
        try:
            logger.info("Re-initializing WebSocket connection...")
            pyRofex.init_websocket_connection(
                market_data_handler=on_market_data,
                order_report_handler=on_order_report,
                error_handler=on_error,
            )
            account_id = creds.ACCOUNT
            try:
                pyRofex.order_report_subscription(account=account_id)
                logger.info("[DEBUG_WS] Handler order_report registrado. Suscripto: True (account=%s)", account_id)
            except Exception as exc:
                logger.error("[DEBUG_WS] Suscripci\u00f3n order_report fall\u00f3 | account=%s | error=%s", account_id, exc)
            logger.info("Subscribing to %s market data...", ticker)
            pyRofex.market_data_subscription(
                tickers=[ticker],
                entries=[
                    pyRofex.MarketDataEntry.BIDS,
                    pyRofex.MarketDataEntry.OFFERS,
                    pyRofex.MarketDataEntry.LAST,
                ],
            )
            logger.info("Feed reconnection successful.")
        except Exception as exc:
            logger.error("Feed reconnection failed: %s", exc)

    # Attach mm context to watchdog for order cancellation
    watchdog_state._mm_context = {"mm": mm}

    watchdog_thread = start_watchdog(
        state=watchdog_state,
        warning_threshold=warning_threshold,
        kill_threshold=kill_threshold,
        check_interval=check_interval,
        reconnect_callback=_reconnect_feed,
        selected_ticker=selected_ticker,
    )

    started = time.time()
    _ART = ZoneInfo("America/Argentina/Buenos_Aires")
    _eod_twap_start_minutes = 30.0
    _eod_cross_only_last_minutes = 2.0
    _eod_hard_exit_minutes_after_close = 5.0
    _eod_twap_interval_seconds = max(3.0, float(config.QUOTE_REFRESH_SECONDS))
    _last_eod_twap_ts = 0.0
    try:
        while running:
            _cb_ok, _cb_reason = _check_circuit_breakers(time.time())
            if not _cb_ok:
                logger.error("[CIRCUIT_BREAKER] %s", _cb_reason)
                config.ENABLE_TRADING = False
                risk_mgr.kill_switch()
                try:
                    mm.cancel_existing_quotes()
                except Exception as exc:
                    logger.warning("Circuit breaker: could not cancel quotes: %s", exc)
                break

            if run_seconds > 0 and (time.time() - started) >= run_seconds:
                logger.info("Run time reached (%ds). Stopping.", run_seconds)
                break

            _now_art = datetime.now(tz=_ART)
            _close_art = _now_art.replace(
                hour=instrument.market_close_hour,
                minute=instrument.market_close_minute,
                second=0,
                microsecond=0,
            )
            _minutes_to_close = (_close_art - _now_art).total_seconds() / 60.0

            if _minutes_to_close <= _eod_twap_start_minutes:
                if not eod_closeout_mode:
                    eod_closeout_mode = True
                    logger.info(
                        "EOD TWAP mode enabled | ttc=%.1f min | pos=%d",
                        _minutes_to_close,
                        inv_mgr.position,
                    )
                    try:
                        mm.cancel_existing_quotes()
                    except Exception as exc:
                        logger.warning("EOD: could not cancel initial quotes: %s", exc)

                _pos = int(inv_mgr.position)
                if _pos != 0:
                    _now_ts = time.time()
                    if (_now_ts - _last_eod_twap_ts) >= _eod_twap_interval_seconds:
                        snap = get_snapshot(selected_ticker)
                        _bid = snap.get("bid_price") if snap else None
                        _ask = snap.get("ask_price") if snap else None
                        if _bid and _ask and float(_bid) > 0 and float(_ask) > 0:
                            _side = "SELL" if _pos > 0 else "BUY"
                            _remaining_qty = abs(_pos)
                            _seconds_to_close = max(_minutes_to_close * 60.0, 1.0)
                            _slices_left = max(int(_seconds_to_close // _eod_twap_interval_seconds), 1)
                            _qty = max(1, (_remaining_qty + _slices_left - 1) // _slices_left)
                            _qty = min(_qty, _remaining_qty)

                            _cross_mode = _minutes_to_close < _eod_cross_only_last_minutes
                            if _cross_mode:
                                _tick_offset = instrument.eod_flatten_ticks * instrument.tick_size
                                _price = (
                                    float(_bid) - _tick_offset
                                    if _pos > 0
                                    else float(_ask) + _tick_offset
                                )
                            else:
                                # Passive TWAP: join best level, avoid crossing before final 2 minutes.
                                _price = float(_ask) if _pos > 0 else float(_bid)

                            _price = max(_price, instrument.tick_size)
                            _resp = send_limit_order(
                                ticker=selected_ticker,
                                side=_side,
                                price=_price,
                                size=_qty,
                            )
                            if _resp:
                                logger.info(
                                    "EOD TWAP slice sent | side=%s | qty=%d | price=%.2f | pos=%d | ttc=%.2f min | cross_mode=%s",
                                    _side,
                                    _qty,
                                    _price,
                                    _pos,
                                    _minutes_to_close,
                                    _cross_mode,
                                )
                            else:
                                logger.warning(
                                    "EOD TWAP slice failed | side=%s | qty=%d | price=%.2f | pos=%d",
                                    _side,
                                    _qty,
                                    _price,
                                    _pos,
                                )
                            _last_eod_twap_ts = _now_ts
                        else:
                            logger.warning(
                                "EOD TWAP: no valid snapshot | pos=%d | bid=%s | ask=%s",
                                _pos,
                                _bid,
                                _ask,
                            )
                elif _minutes_to_close <= 0:
                    logger.info("EOD flatten complete (pos=0). Exiting cleanly.")
                    break

                if _minutes_to_close <= -_eod_hard_exit_minutes_after_close:
                    if inv_mgr.position != 0:
                        logger.warning(
                            "EOD hard-exit reached with open position | pos=%d | exiting to avoid orphan process.",
                            inv_mgr.position,
                        )
                    else:
                        logger.info("EOD hard-exit reached with flat position. Exiting.")
                    break

            skew_state = mm.get_skew_state()
            _merge_skew_state(state_bot_id, skew_state)
            if state_bot_id:
                _hb_write(
                    bot_id=state_bot_id,
                    ticker=selected_ticker,
                    position=inv_mgr.position,
                    pnl=float(inv_mgr.realized_pnl + inv_mgr.unrealized_pnl),
                    fill_buy_qty=fill_buy_qty,
                    fill_sell_qty=fill_sell_qty,
                    cycles=cycles_count,
                    status="running",
                )
            time.sleep(config.QUOTE_REFRESH_SECONDS)
    finally:
        watchdog_state.running = False
        watchdog_state.stop_event.set()
        # Daemon thread is intentionally not joined: shutdown must not block here.
        _ = watchdog_thread
        mm.cancel_existing_quotes()
        try:
            pyRofex.close_websocket_connection()
        except Exception as exc:
            logger.error("Error closing websocket: %s", exc)

        risk_status = risk_mgr.get_status(inv_mgr)
        avg_buy = (fill_buy_notional / fill_buy_qty) if fill_buy_qty > 0 else 0.0
        avg_sell = (fill_sell_notional / fill_sell_qty) if fill_sell_qty > 0 else 0.0
        roundtrip_qty = min(fill_buy_qty, fill_sell_qty)
        spread_captured = (avg_sell - avg_buy) if roundtrip_qty > 0 else 0.0
        logger.info("Final summary start")
        logger.info("Total cycles: %d", cycles_count)
        logger.info("Fills buys=%d sells=%d total=%d", fill_buy_qty, fill_sell_qty, fill_buy_qty + fill_sell_qty)
        if getattr(mm, "_is_simulation", False):
            logger.info("[FILL_VALIDATION] Skipped (sim mode)")
        else:
            logger.info("[FILL_VALIDATION] WS fill events: %d", ws_fill_events)
            logger.info("[FILL_VALIDATION] Cancel-path fill detections: %d", mm._cancel_fill_detections)
            if ws_fill_events != mm._cancel_fill_detections:
                logger.warning(
                    "[FILL_VALIDATION] Path mismatch detected - WS=%d vs CANCEL=%d - review logs",
                    ws_fill_events,
                    mm._cancel_fill_detections,
                )
            else:
                logger.info("[FILL_VALIDATION] Paths consistent - OK")
        logger.info("Average fill buy: %.2f", avg_buy)
        logger.info("Average fill sell: %.2f", avg_sell)
        logger.info("Spread captured: %.2f", spread_captured)
        logger.info("Contracts traded: %d", inv_mgr.traded_contracts)
        logger.info("Final position: %d", inv_mgr.position)
        logger.info("Realized PnL: %.2f", inv_mgr.realized_pnl)
        logger.info("Unrealized PnL: %.2f", inv_mgr.unrealized_pnl)
        logger.info("Total PnL: %.2f", inv_mgr.realized_pnl + inv_mgr.unrealized_pnl)
        logger.info("Risk status: %s", risk_status)
        logger.info("Final summary end")


def _parse_args() -> argparse.Namespace:
    """Parses CLI args for MM run."""
    parser = argparse.ArgumentParser(description="Run market maker on reMarkets")
    parser.add_argument(
        "--instrument",
        type=str,
        default="DLR",
        choices=["DLR", "CAUC", "SOJ", "SOJ_MIN"],
        help="Instrument to trade: DLR, CAUC, SOJ or SOJ_MIN",
    )
    parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="Force a specific ticker (e.g. DLR/ABR26M). Bypasses auto-select.",
    )
    parser.add_argument(
        "--run-seconds",
        type=int,
        default=0,
        help="Run duration in seconds. Use 0 for infinite loop.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.ticker:
        _configure_ticker_file_logging(args.ticker)
    cfg = MMConfig()
    instrument_cfg = cfg.instruments[args.instrument]
    logger.info("Starting MM for instrument: %s (%s)", args.instrument, instrument_cfg.name)
    run_market_maker(
        config=cfg,
        instrument=instrument_cfg,
        run_seconds=args.run_seconds,
        forced_ticker=args.ticker,
    )
