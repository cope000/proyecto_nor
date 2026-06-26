"""Core market making logic for DLR futures."""

from __future__ import annotations

import math
import time
from collections import deque
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime
from statistics import stdev

from config.mm_config import MMConfig, InstrumentConfig
from core.order_manager import cancel_order, get_order_status, send_limit_order
from core.utils import setup_logger
from utils.adverse_selection import AdverseSelectionDetector
from .fair_value import FairValueCalculator, optimal_spread, reservation_price
from .inventory_manager import InventoryManager
from .mm_risk import MMRiskManager
from .risk_manager import RiskManager

logger = setup_logger("market_maker")


class SigmaTracker:
    """Rolling volatility tracker para el mid price."""

    def __init__(self, window: int = 50) -> None:
        self.window = max(int(window), 2)
        self._mids: deque[float] = deque(maxlen=self.window)

    def update(self, mid: float) -> None:
        if mid > 0:
            self._mids.append(float(mid))

    def sigma(self) -> float:
        """Retorna std de los ultimos N mids. Minimo 0.0."""
        if len(self._mids) < 2:
            return 0.0
        return max(0.0, float(stdev(self._mids)))


class MarketMaker:
    """Generates and manages two-sided quotes around fair value."""

    def __init__(
        self,
        config: MMConfig,
        fair_value_calc: FairValueCalculator,
        inventory_mgr: InventoryManager,
        risk_mgr: RiskManager,
        mm_risk: MMRiskManager | None = None,
        instrument: InstrumentConfig | None = None,
    ) -> None:
        self.config = config
        self.instrument = instrument
        self.fair_value_calc = fair_value_calc
        self.inventory_mgr = inventory_mgr
        self.risk_mgr = risk_mgr
        self.mm_risk = mm_risk or MMRiskManager()
        self.fill_logger = None

        self.active_bid_id: str | None = None
        self.active_bid_prop: str | None = None
        self.active_ask_id: str | None = None
        self.active_ask_prop: str | None = None

        self.last_bid_quote: float | None = None
        self.last_ask_quote: float | None = None
        self.last_spread_bps: float = 0.0
        self.last_market_spread_bps: float = 0.0
        self.last_position_in_book: str = "unknown"
        self._last_quote_ts: float = 0.0

        # FIX 4: track last PLACED prices to detect if quote changed
        self._placed_bid_price: float | None = None
        self._placed_ask_price: float | None = None
        self._last_circuit_breaker_state: bool = False
        self._last_circuit_breaker_log_ts: float = 0.0
        self._last_stale_warning_ts: float = 0.0
        self._last_eod_flatten_attempt_ts: float = 0.0
        self._max_inventory_limit_side: str | None = None
        self._cancel_fill_detections: int = 0
        self._is_simulation: bool = False
        self._initial_position_set: bool = False
        self._last_reservation_price: float | None = None
        self._last_fair_value: float = 0.0
        self._last_mid: float = 0.0
        sigma_window = self.instrument.sigma_window if self.instrument else 50
        self._sigma_tracker = SigmaTracker(window=sigma_window)

        # Adverse selection detector (optional, only if enabled in config)
        self._adverse_detector: AdverseSelectionDetector | None = None
        if self.instrument and getattr(self.instrument, "adverse_detection_enabled", False):
            self._adverse_detector = AdverseSelectionDetector(
                window=getattr(self.instrument, "adverse_window", 10),
                imbalance_threshold=getattr(self.instrument, "adverse_imbalance_threshold", 0.6),
                spread_multiplier=getattr(self.instrument, "adverse_spread_multiplier", 1.5),
                cooldown_fills=getattr(self.instrument, "adverse_cooldown_fills", 5),
            )

        # L2 book data — updated each tick from WS message
        self._bid_size_l1: int = 1
        self._ask_size_l1: int = 1
        self._bid_depth: int = 1
        self._ask_depth: int = 1

    def set_initial_position(self, pos: int) -> None:
        """Sets initial net position once at startup from recovered session state.

        This is idempotent by design: only the first invocation can set state.
        """
        if self._initial_position_set:
            return

        self._initial_position_set = True
        initial_pos = int(pos)
        if initial_pos == 0:
            return

        # InventoryManager has no dedicated bootstrap method; set net position
        # directly so inventory-aware quoting starts from recovered exposure.
        self.inventory_mgr.position = initial_pos
        logger.info("Initial position applied | pos=%d", initial_pos)

    def set_fill_logger(self, fill_logger) -> None:
        self.fill_logger = fill_logger

    def get_skew_state(self) -> dict:
        """Retorna estado actual del inventory skew y adverse selection para observabilidad."""
        state = {
            "skew_active": bool(getattr(self.instrument, "inventory_skew_enabled", False)),
            "reservation_price": self._last_reservation_price,
            "sigma": self._sigma_tracker.sigma() if hasattr(self, "_sigma_tracker") else None,
            "net_position": self.inventory_mgr.position,
        }
        if self._adverse_detector:
            state.update(self._adverse_detector.get_state())
        return state

    def _minutes_to_close(self, now_dt: datetime | None = None) -> float:
        """Returns minutes to configured market close for the active instrument."""
        if now_dt is None:
            now_dt = datetime.now()
        close_hour = self.instrument.market_close_hour if self.instrument else 15
        close_minute = self.instrument.market_close_minute if self.instrument else 0
        close_dt = now_dt.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        return (close_dt - now_dt).total_seconds() / 60.0

    def _time_to_close_fraction(self, now_dt: datetime | None = None) -> float:
        """Returns remaining fraction of trading session until configured market close (0..1)."""
        if now_dt is None:
            now_dt = datetime.now()
        minutes_to_close = self._minutes_to_close(now_dt)
        open_hour = self.instrument.market_open_hour if self.instrument else 10
        open_minute = self.instrument.market_open_minute if self.instrument else 0
        close_hour = self.instrument.market_close_hour if self.instrument else 15
        close_minute = self.instrument.market_close_minute if self.instrument else 0
        open_dt = now_dt.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
        close_dt = now_dt.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        session_minutes = max((close_dt - open_dt).total_seconds() / 60.0, 1.0)
        return max(0.0, min(1.0, minutes_to_close / session_minutes))

    def _get_mark_price(self, fair_value: float, bid: float | None, ask: float | None) -> float:
        """Marks inventory to the executable side of book when available."""
        position = self.inventory_mgr.position
        if position > 0 and bid is not None and bid > 0:
            return float(bid)
        if position < 0 and ask is not None and ask > 0:
            return float(ask)
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        return fair_value

    def _get_effective_quotes(
        self,
        bid_quote: float | None,
        ask_quote: float | None,
        close_only_mode: bool = False,
    ) -> tuple[float | None, float | None]:
        """Returns the quotes that are actually eligible to be sent."""
        position = self.inventory_mgr.position
        can_buy = position < self._max_position
        can_sell = position > -self._max_position

        max_inventory = self.instrument.max_inventory if self.instrument else self._max_position
        max_inventory = max(int(max_inventory), 1)

        current_limit_side: str | None = None
        if position >= max_inventory:
            can_buy = False
            current_limit_side = "buy"
        elif position <= -max_inventory:
            can_sell = False
            current_limit_side = "sell"

        if current_limit_side != self._max_inventory_limit_side:
            if current_limit_side is not None:
                logger.warning(
                    "Hard inventory limit reached | pos=%d | max_inventory=%d | blocked_side=%s",
                    position,
                    max_inventory,
                    current_limit_side,
                )
            self._max_inventory_limit_side = current_limit_side

        if close_only_mode:
            if position > 0:
                can_buy = False
            elif position < 0:
                can_sell = False
            else:
                can_buy = False
                can_sell = False

        effective_bid = bid_quote if can_buy else None
        effective_ask = ask_quote if can_sell else None
        return effective_bid, effective_ask

    def _clear_order_tracking(self, client_id: str, status: str) -> None:
        """Clears local active order tracking for terminal order statuses."""
        terminal_statuses = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
        if status not in terminal_statuses:
            return
        if self.active_bid_id == client_id:
            self.active_bid_id = None
            self.active_bid_prop = None
            self._placed_bid_price = None
            logger.info("Order tracking cleared | side=BUY | status=%s | id=%s", status, client_id)
        elif self.active_ask_id == client_id:
            self.active_ask_id = None
            self.active_ask_prop = None
            self._placed_ask_price = None
            logger.info("Order tracking cleared | side=SELL | status=%s | id=%s", status, client_id)

    # --- Instrument-aware parameter resolution ---
    @property
    def _tick_size(self) -> float:
        return self.instrument.tick_size if self.instrument else self.config.TICK_SIZE

    @property
    def _max_position(self) -> int:
        return self.instrument.max_position if self.instrument else self.config.MAX_POSITION

    @property
    def _order_size(self) -> int:
        return self.instrument.order_size if self.instrument else self.config.ORDER_SIZE

    @property
    def _base_spread_bps(self) -> float:
        return self.instrument.base_spread_bps if self.instrument else self.config.BASE_SPREAD_BPS

    @property
    def _min_edge_bps(self) -> float:
        return self.instrument.min_edge_bps if self.instrument else self.config.MIN_EDGE_BPS

    @property
    def _min_spread_bps(self) -> float:
        return self.instrument.min_spread_bps if self.instrument else self.config.MIN_SPREAD_BPS

    @property
    def _improve_spread_threshold_bps(self) -> float:
        return float(getattr(self.instrument, "improve_spread_threshold_bps", 0.0) or 0.0)

    @property
    def _improve_tick_offset(self) -> int:
        return max(int(getattr(self.instrument, "improve_tick_offset", 1) or 1), 1)

    def _round_bid(self, price: float) -> float:
        return self._snap_to_tick(price, side="BUY")

    def _round_ask(self, price: float) -> float:
        return self._snap_to_tick(price, side="SELL")

    def _snap_to_tick(self, price: float, side: str) -> float:
        tick_dec = Decimal(str(self._tick_size))
        if tick_dec <= 0:
            return max(0.0, float(price))

        px_dec = Decimal(str(max(0.0, float(price))))
        rounding = ROUND_FLOOR if side.upper() == "BUY" else ROUND_CEILING
        steps = (px_dec / tick_dec).to_integral_value(rounding=rounding)
        snapped = steps * tick_dec
        decimals = max(0, -tick_dec.as_tuple().exponent)
        quant = Decimal("1").scaleb(-decimals)
        return float(snapped.quantize(quant))

    # FIX 1: process fill via inventory manager and log it
    def _process_fill(self, side: str, fill_price: float, fill_qty: int) -> None:
        """Processes a detected fill: updates inventory and logs."""
        self.inventory_mgr.on_fill(side=side, price=fill_price, size=fill_qty)
        logger.info(
            "Fill processed | side=%s | price=%.2f | qty=%d | pos=%d | realized=%.2f",
            side,
            fill_price,
            fill_qty,
            self.inventory_mgr.position,
            self.inventory_mgr.realized_pnl,
        )
        if self.fill_logger is not None:
            self.fill_logger.log_fill(
                side=side,
                price=fill_price,
                qty=fill_qty,
                order_id="detected_via_cancel",
            )
        # Register fill with adverse selection detector
        if self._adverse_detector:
            self._adverse_detector.on_fill(
                side=side,
                price=fill_price,
                mid_at_fill=self._last_mid,
            )

    # FIX 1: called from run_mm.py on_order_report to clear WS-detected fills
    def calculate_quotes(
        self,
        fair_value: float,
        market_bid: float | None,
        market_ask: float | None,
        close_only_mode: bool = False,
        closing_edge_factor: float = 1.0,
        sigma_price: float = 0.0,
        time_to_close_fraction: float = 0.0,
    ) -> tuple[float | None, float | None, float, float, str]:
        """Returns (bid, ask, spread_bps, market_spread_bps, position_in_book)."""
        spread_bps = max(self._base_spread_bps, self._min_spread_bps)
        vol_bps = self.fair_value_calc.get_recent_volatility(self.config.VOL_WINDOW)
        if vol_bps > 0:
            spread_bps = max(spread_bps, vol_bps * self.config.VOL_SPREAD_MULTIPLIER)

        skew_bps = self.inventory_mgr.get_skew_bps()
        min_edge = fair_value * self._min_edge_bps / 10000.0
        half_spread = max(fair_value * spread_bps / 10000.0 / 2.0, min_edge)

        # Layer 4: volatility spread widening.
        vol_multiplier = self.mm_risk.get_spread_multiplier()
        if vol_multiplier > 1.0:
            half_spread *= vol_multiplier

        skew_amount = fair_value * skew_bps / 10000.0
        
        # Apply adverse selection spread multiplier if active
        adverse_mult = 1.0
        if self._adverse_detector:
            adverse_mult = self._adverse_detector.get_spread_multiplier()
            if adverse_mult > 1.0:
                logger.warning(
                    "AdverseSelection active | imbalance=%.2f | mult=%.2f",
                    self._adverse_detector.get_state()["imbalance"],
                    adverse_mult,
                )
        half_spread = half_spread * adverse_mult

        has_book = bool(market_bid and market_ask and market_bid > 0 and market_ask > 0 and market_ask > market_bid)
        market_spread_bps = 0.0
        position_in_book = "symmetric"

        if has_book:
            market_spread_bps = ((market_ask - market_bid) / fair_value) * 10000.0

        inventory_skew_enabled = bool(getattr(self.instrument, "inventory_skew_enabled", False))
        if inventory_skew_enabled:
            gamma = float(getattr(self.instrument, "gamma", 0.05))
            kappa = float(getattr(self.instrument, "kappa", 1.5))
            sigma_floor = float(getattr(self.instrument, "sigma_floor", 0.5))
            sigma_eff = max(float(sigma_price), sigma_floor)
            ttc = max(0.0, min(1.0, float(time_to_close_fraction)))

            r_price = reservation_price(
                mid=fair_value,
                # Uses inventory_mgr.position, which includes recovered startup
                # position after set_initial_position() in run_market_maker.
                net_position=self.inventory_mgr.position,
                gamma=gamma,
                sigma=sigma_eff,
                time_to_close=ttc,
            )
            self._last_reservation_price = r_price
            spread_opt = optimal_spread(
                gamma=gamma,
                sigma=sigma_eff,
                time_to_close=ttc,
                kappa=kappa,
            )
            min_half_spread = fair_value * self._min_spread_bps / 10000.0 / 2.0
            half_spread = max(min_half_spread, spread_opt / 2.0, min_edge)
            bid_price = r_price - half_spread
            ask_price = r_price + half_spread
            logger.debug(
                "InventorySkew | pos=%d | mid=%.4f | r=%.4f | sigma=%.4f | ttc=%.3f | spread_opt=%.4f | bid=%.4f | ask=%.4f",
                self.inventory_mgr.position,
                fair_value,
                r_price,
                sigma_eff,
                ttc,
                spread_opt,
                bid_price,
                ask_price,
            )
            position_in_book = "inventory_skew"
        else:
            self._last_reservation_price = None
            if self.config.AGGRESSIVE_MODE and has_book:
                midpoint = (market_bid + market_ask) / 2.0
                base_half = max(midpoint * self._base_spread_bps / 10000.0 / 2.0, min_edge)
                bid_price = midpoint - base_half - skew_amount
                ask_price = midpoint + base_half - skew_amount
            else:
                bid_price = fair_value - half_spread - skew_amount
                ask_price = fair_value + half_spread - skew_amount

        # Safety: never send a marketable order due to inventory skew pushing
        # bid above best ask or ask below best bid.
        if has_book:
            bid_price = min(bid_price, market_ask - self._tick_size)
            ask_price = max(ask_price, market_bid + self._tick_size)

        bid_quote = self._round_bid(max(0.0, bid_price))
        ask_quote = self._round_ask(max(0.0, ask_price))

        if self.config.IMPROVE_BEST and has_book and market_spread_bps > self._improve_spread_threshold_bps:
            improved_bid = market_bid + (self._improve_tick_offset * self._tick_size)
            improved_ask = market_ask - (self._improve_tick_offset * self._tick_size)
            improved_spread = improved_ask - improved_bid
            min_spread_abs = fair_value * self._min_spread_bps / 10000.0

            if improved_spread >= min_spread_abs:
                bid_quote = self._round_bid(max(0.0, improved_bid))
                ask_quote = self._round_ask(max(0.0, improved_ask))
                position_in_book = "improving"
                logger.info(
                    "Top of book improving | market_spread=%.2fbps | bid=%.2f ask=%.2f",
                    market_spread_bps,
                    bid_quote,
                    ask_quote,
                )

        if ask_quote <= bid_quote:
            ask_quote = self._round_ask(bid_quote + self._tick_size)

        # Check spread AFTER rounding to avoid float precision false positives.
        if (ask_quote - bid_quote) < (2.0 * min_edge):
            logger.info(
                "Spread too tight after rounding, skipping | bid_q=%.2f ask_q=%.2f min_edge=%.4f",
                bid_quote, ask_quote, min_edge,
            )
            self.last_bid_quote = None
            self.last_ask_quote = None
            self.last_spread_bps = 0.0
            self.last_market_spread_bps = market_spread_bps
            self.last_position_in_book = "skipped_tight"
            return None, None, 0.0, market_spread_bps, "skipped_tight"

        # Relax min edge for the side that closes inventory.
        position = self.inventory_mgr.position
        min_edge_bid = min_edge
        min_edge_ask = min_edge

        if position > 0:
            # Long inventory: SELL side closes risk.
            urgency = min(abs(position) / max(self._max_position, 1), 1.0)
            min_edge_ask = min_edge * (1.0 - urgency * 0.75)
            min_edge_ask *= closing_edge_factor
        elif position < 0:
            # Short inventory: BUY side closes risk.
            urgency = min(abs(position) / max(self._max_position, 1), 1.0)
            min_edge_bid = min_edge * (1.0 - urgency * 0.75)
            min_edge_bid *= closing_edge_factor

        bid_has_edge = (fair_value - bid_quote) >= min_edge_bid
        ask_has_edge = (ask_quote - fair_value) >= min_edge_ask

        if not bid_has_edge and not ask_has_edge:
            logger.info(
                "No edge on either side, skipping | bid_q=%s ask_q=%s fair=%.2f min_edge_bid=%.4f min_edge_ask=%.4f",
                bid_quote, ask_quote, fair_value, min_edge_bid, min_edge_ask,
            )
            self.last_bid_quote = None
            self.last_ask_quote = None
            self.last_spread_bps = 0.0
            self.last_market_spread_bps = market_spread_bps
            self.last_position_in_book = "skipped_edge"
            return None, None, 0.0, market_spread_bps, "skipped_edge"

        needs_buy_to_close = position < 0
        needs_sell_to_close = position > 0

        if not bid_has_edge and not needs_buy_to_close:
            if position_in_book != "improving":
                bid_quote = None

        if not ask_has_edge and not needs_sell_to_close:
            if position_in_book != "improving":
                ask_quote = None

        if close_only_mode:
            if position > 0:
                bid_quote = None
                position_in_book = "eod_close_only"
            elif position < 0:
                ask_quote = None
                position_in_book = "eod_close_only"
            else:
                self.last_bid_quote = None
                self.last_ask_quote = None
                self.last_spread_bps = 0.0
                self.last_market_spread_bps = market_spread_bps
                self.last_position_in_book = "eod_flat"
                return None, None, 0.0, market_spread_bps, "eod_flat"

        if bid_quote is not None and ask_quote is not None and ask_quote <= bid_quote:
            ask_quote = self._round_ask(bid_quote + self._tick_size)

        self.last_bid_quote = bid_quote
        self.last_ask_quote = ask_quote
        if bid_quote is not None and ask_quote is not None:
            self.last_spread_bps = ((ask_quote - bid_quote) / fair_value) * 10000.0
        else:
            self.last_spread_bps = 0.0
        self.last_market_spread_bps = market_spread_bps
        self.last_position_in_book = position_in_book
        return bid_quote, ask_quote, self.last_spread_bps, market_spread_bps, position_in_book

    def cancel_existing_quotes(self) -> None:
        """Cancels tracked active bid/ask orders.

        Fill accounting is handled centrally via order_report_handler in runner code.
        """
        if self.active_bid_id:
            _bid_id = self.active_bid_id
            _bid_prop = self.active_bid_prop
            resp = cancel_order(_bid_id, proprietary=_bid_prop)
            if isinstance(resp, dict):
                order = resp.get("order") if isinstance(resp.get("order"), dict) else {}
                status_upper = str(resp.get("status") or "").upper()
                order_status_upper = str(order.get("status") or "").upper()
                try:
                    order_cum_qty = int(float(order.get("cumQty") or 0))
                except (TypeError, ValueError):
                    order_cum_qty = 0

                # Mirror historical detection in cancel_order:
                # filled status with positive cumQty (or explicit ALREADY_FILLED marker).
                is_already_filled = status_upper == "ALREADY_FILLED" or (
                    order_status_upper == "FILLED" and order_cum_qty > 0
                )
                if is_already_filled:
                    try:
                        fill_px = float(order.get("avgPx") or order.get("price") or 0)
                    except (TypeError, ValueError):
                        fill_px = 0.0
                    self._cancel_fill_detections += 1
                    logger.info(
                        "[FILL_VALIDATION_CANCEL] order_id=%s side=BUY price=%.2f qty=%d",
                        _bid_id,
                        fill_px,
                        order_cum_qty,
                    )
            self._placed_bid_price = None
            self.active_bid_id = None
            self.active_bid_prop = None

        if self.active_ask_id:
            _ask_id = self.active_ask_id
            _ask_prop = self.active_ask_prop
            resp = cancel_order(_ask_id, proprietary=_ask_prop)
            if isinstance(resp, dict):
                order = resp.get("order") if isinstance(resp.get("order"), dict) else {}
                status_upper = str(resp.get("status") or "").upper()
                order_status_upper = str(order.get("status") or "").upper()
                try:
                    order_cum_qty = int(float(order.get("cumQty") or 0))
                except (TypeError, ValueError):
                    order_cum_qty = 0

                # Mirror historical detection in cancel_order:
                # filled status with positive cumQty (or explicit ALREADY_FILLED marker).
                is_already_filled = status_upper == "ALREADY_FILLED" or (
                    order_status_upper == "FILLED" and order_cum_qty > 0
                )
                if is_already_filled:
                    try:
                        fill_px = float(order.get("avgPx") or order.get("price") or 0)
                    except (TypeError, ValueError):
                        fill_px = 0.0
                    self._cancel_fill_detections += 1
                    logger.info(
                        "[FILL_VALIDATION_CANCEL] order_id=%s side=SELL price=%.2f qty=%d",
                        _ask_id,
                        fill_px,
                        order_cum_qty,
                    )
            self._placed_ask_price = None
            self.active_ask_id = None
            self.active_ask_prop = None

    def _calculate_order_size(self, side: str) -> int:
        """Dynamic sizing based on inventory pressure and book depth.

        Rules:
        1. Base = instrument.order_size (or 1).
        2. Inventory factor: scale up toward max_order_size on the side that
           closes risk; scale down on the side that grows risk near the limit.
        3. Liquidity cap: never send more than 30% of the available top-3 depth.
        Returns an int in [1, max_order_size].
        """
        base = int(getattr(self.instrument, "order_size", 1) if self.instrument else 1)
        max_size = int(getattr(self.instrument, "max_order_size", base) if self.instrument else base)
        max_size = max(max_size, base)  # safety: max_size >= base
        pos = self.inventory_mgr.position
        max_inv = int(getattr(self.instrument, "max_inventory", 10) if self.instrument else 10)
        max_inv = max(max_inv, 1)

        if side == "BUY":
            # Scale up when short (closing risk), down when long (adding risk)
            inv_ratio = max(0.0, 1.0 - max(pos, 0) / max_inv)
            depth = self._bid_depth
        else:
            # Scale up when long (closing risk), down when short (adding risk)
            inv_ratio = max(0.0, 1.0 - abs(min(pos, 0)) / max_inv)
            depth = self._ask_depth

        # Liquidity cap: at most 30% of top-3 depth, floor at 1
        max_by_liquidity = max(1, int(depth * 0.30))

        dynamic_size = max(1, min(
            max_size,
            int(base + round(inv_ratio * (max_size - base))),
            max_by_liquidity,
        ))
        return dynamic_size

    def place_quotes(self, ticker: str, bid_price: float | None, ask_price: float | None) -> None:
        """Places BUY/SELL limit quotes (single-sided allowed)."""
        if not self.config.ENABLE_TRADING:
            return

        pos = self.inventory_mgr.position
        can_buy = pos < self._max_position
        can_sell = pos > -self._max_position

        # FIX 5: send both orders before checking any status
        if bid_price is not None and can_buy:
            size_bid = self._calculate_order_size("BUY")
            bid_resp = send_limit_order(ticker=ticker, side="BUY", price=bid_price, size=size_bid)
            if bid_resp:
                self.active_bid_id = bid_resp.get("order", {}).get("clientId")
                self.active_bid_prop = bid_resp.get("proprietary") or bid_resp.get("order", {}).get("proprietary")
                logger.info("Order sent | side=BUY | price=%.2f | size=%d | bid_depth=%d", bid_price, size_bid, self._bid_depth)

        if ask_price is not None and can_sell:
            size_ask = self._calculate_order_size("SELL")
            ask_resp = send_limit_order(ticker=ticker, side="SELL", price=ask_price, size=size_ask)
            if ask_resp:
                self.active_ask_id = ask_resp.get("order", {}).get("clientId")
                self.active_ask_prop = ask_resp.get("proprietary") or ask_resp.get("order", {}).get("proprietary")
                logger.info("Order sent | side=SELL | price=%.2f | size=%d | ask_depth=%d", ask_price, size_ask, self._ask_depth)

    def _send_eod_flatten_order(self, ticker: str, side: str, price: float, size: int) -> bool:
        """Sends and tracks a one-sided EOD flatten order for the full residual position."""
        result = send_limit_order(ticker=ticker, side=side, price=price, size=size)
        if not result:
            return False

        if side == "BUY":
            self.active_bid_id = result.get("order", {}).get("clientId")
            self.active_bid_prop = result.get("proprietary") or result.get("order", {}).get("proprietary")
            self._placed_bid_price = price
            self._placed_ask_price = None
        else:
            self.active_ask_id = result.get("order", {}).get("clientId")
            self.active_ask_prop = result.get("proprietary") or result.get("order", {}).get("proprietary")
            self._placed_ask_price = price
            self._placed_bid_price = None

        logger.info("Order sent | side=%s | price=%.2f | size=%d", side, price, size)
        return True

    def _quote_unchanged(self, new_price: float | None, placed_price: float | None, tick_size: float) -> bool:
        """Retorna True si el cambio es menor a medio tick (0.5 * tick_size)."""
        if new_price is None and placed_price is None:
            return True
        if new_price is None or placed_price is None:
            return False
        return abs(new_price - placed_price) < (tick_size * 0.5)

    def on_market_data(
        self,
        ticker: str,
        bid: float | None,
        ask: float | None,
        last: float | None,
        bid_size_l1: int = 1,
        ask_size_l1: int = 1,
        bid_depth: int = 1,
        ask_depth: int = 1,
    ) -> None:
        """Processes market data, performs risk checks, and refreshes quotes."""
        # Store L2 book data for use in sizing
        self._bid_size_l1 = max(int(bid_size_l1 or 1), 1)
        self._ask_size_l1 = max(int(ask_size_l1 or 1), 1)
        self._bid_depth = max(int(bid_depth or 1), 1)
        self._ask_depth = max(int(ask_depth or 1), 1)

        fair_value = self.fair_value_calc.update(bid=bid, ask=ask, last=last)
        self._last_fair_value = fair_value
        self._last_mid = fair_value
        # Update adverse selection detector with current mid
        if self._adverse_detector:
            self._adverse_detector.on_mid_update(self._last_mid)
        if fair_value <= 0:
            now_ts = time.monotonic()
            if now_ts - self._last_stale_warning_ts >= 30.0:
                logger.warning("No fair value available yet. bid=%s ask=%s last=%s", bid, ask, last)
                self._last_stale_warning_ts = now_ts
            return

        if bid is not None and ask is not None and bid > 0 and ask > 0 and ask > bid:
            self._sigma_tracker.update((float(bid) + float(ask)) / 2.0)
        else:
            self._sigma_tracker.update(float(fair_value))

        # Feed fair value into MM-specific volatility tracker.
        self.mm_risk.on_price_update(fair_value)

        # Keep unrealized PnL up to date before MM-specific checks.
        mark_price = self._get_mark_price(fair_value, bid, ask)
        self.inventory_mgr.get_unrealized_pnl(mark_price)
        current_pnl = self.inventory_mgr.realized_pnl
        if hasattr(self.inventory_mgr, "unrealized_pnl"):
            current_pnl += self.inventory_mgr.unrealized_pnl

        minutes_to_close = self._minutes_to_close()
        flatten_before_close = self.instrument.flatten_before_close_minutes if self.instrument else 15
        aggressive_flatten_before_close = (
            self.instrument.flatten_aggressive_minutes_before_close if self.instrument else flatten_before_close
        )
        aggressive_tick_offset = self.instrument.flatten_aggressive_tick_offset if self.instrument else 2
        force_flatten_before_close = self.instrument.force_flatten_before_close_minutes if self.instrument else 5
        close_only_mode = self.inventory_mgr.position != 0 and 0 <= minutes_to_close <= flatten_before_close
        aggressive_flatten_mode = self.inventory_mgr.position != 0 and 0 <= minutes_to_close <= aggressive_flatten_before_close
        force_flatten_mode = self.inventory_mgr.position != 0 and 0 <= minutes_to_close <= force_flatten_before_close

        if aggressive_flatten_mode:
            flatten_side = "SELL" if self.inventory_mgr.position > 0 else "BUY"
            flatten_price: float | None = None
            flatten_tick_offset = aggressive_tick_offset if force_flatten_mode else 0
            if self.inventory_mgr.position > 0 and bid is not None and bid > 0:
                flatten_price = max(self._tick_size, bid - (flatten_tick_offset * self._tick_size))
            elif self.inventory_mgr.position < 0 and ask is not None and ask > 0:
                flatten_price = ask + (flatten_tick_offset * self._tick_size)

            if flatten_price is None:
                logger.warning(
                    "EOD_FLATTEN waiting for book | side=%s | pos=%d | minutes_to_close=%.1f",
                    flatten_side,
                    self.inventory_mgr.position,
                    minutes_to_close,
                )
                return

            flatten_price = self._round_bid(flatten_price) if flatten_side == "SELL" else self._round_ask(flatten_price)

            active_flatten_order = (
                (flatten_side == "SELL" and self.active_ask_id is not None)
                or (flatten_side == "BUY" and self.active_bid_id is not None)
            )
            now_ts = time.time()
            if active_flatten_order and (now_ts - self._last_eod_flatten_attempt_ts) < 30.0:
                self._log_cycle(
                    fair_value=fair_value,
                    market_bid=bid,
                    market_ask=ask,
                    spread_bps=0.0,
                    market_spread_bps=self.last_market_spread_bps,
                    position_in_book="eod_force_flatten_held" if force_flatten_mode else "eod_aggressive_flatten_held",
                )
                return

            self.cancel_existing_quotes()
            flatten_qty = abs(self.inventory_mgr.position)
            self._last_eod_flatten_attempt_ts = now_ts
            self._send_eod_flatten_order(
                ticker=ticker,
                side=flatten_side,
                price=flatten_price,
                size=flatten_qty,
            )
            if force_flatten_mode:
                logger.critical(
                    "[CRITICAL] EOD force flatten | pos=%d | side=%s | price=%.2f",
                    self.inventory_mgr.position,
                    flatten_side,
                    flatten_price,
                )
            else:
                logger.warning(
                    "[WARNING] EOD aggressive flatten | pos=%d | side=%s | price=%.2f",
                    self.inventory_mgr.position,
                    flatten_side,
                    flatten_price,
                )
            self._log_cycle(
                fair_value=fair_value,
                market_bid=bid,
                market_ask=ask,
                spread_bps=0.0,
                market_spread_bps=self.last_market_spread_bps,
                position_in_book="eod_force_flatten" if force_flatten_mode else "eod_aggressive_flatten",
            )
            return

        if 0 <= minutes_to_close <= flatten_before_close and self.inventory_mgr.position == 0:
            self.cancel_existing_quotes()
            self._placed_bid_price = None
            self._placed_ask_price = None
            self._log_cycle(
                fair_value=fair_value,
                market_bid=bid,
                market_ask=ask,
                spread_bps=0.0,
                market_spread_bps=self.last_market_spread_bps,
                position_in_book="eod_flat",
            )
            return

        if self.inventory_mgr.position == 0:
            self._last_eod_flatten_attempt_ts = 0.0
            self.mm_risk.on_flatten_success()

        # Layer 5: emergency flatten has highest priority.
        if self.mm_risk.should_flatten(self.inventory_mgr.position, current_pnl):
            try:
                self.cancel_existing_quotes()
                if self.inventory_mgr.position != 0 and bid and ask:
                    flatten_price = self.mm_risk.get_flatten_price(
                        self.inventory_mgr.position,
                        bid,
                        ask,
                        self._tick_size,
                    )
                    flatten_side = "SELL" if self.inventory_mgr.position > 0 else "BUY"
                    flatten_price = self._round_bid(flatten_price) if flatten_side == "SELL" else self._round_ask(flatten_price)
                    flatten_qty = abs(self.inventory_mgr.position)
                    logger.warning(
                        "EMERGENCY FLATTEN | side=%s | price=%.2f | qty=%d",
                        flatten_side,
                        flatten_price,
                        flatten_qty,
                    )
                    result = send_limit_order(
                        ticker=ticker,
                        side=flatten_side,
                        price=flatten_price,
                        size=flatten_qty,
                    )
                    success = result is not None and str(result.get("status", "")).upper() == "OK"
                    self.mm_risk.on_flatten_attempt(success)
                else:
                    self.mm_risk.on_flatten_attempt(False)
            except Exception as e:
                logger.error("[EMERGENCY_FLATTEN_FAILED] reason=%s | activating kill_switch", str(e))
                self.mm_risk.on_flatten_attempt(False)
                self.risk_mgr.kill_switch()
            return

        # Layer 2: max daily loss.
        if not self.mm_risk.check_daily_loss(current_pnl):
            self.cancel_existing_quotes()
            logger.error("MM stopped: daily loss limit reached. PnL=%.2f", current_pnl)
            return

        # Layer 1: volatility circuit breaker with hysteresis.
        if not self.mm_risk.check_circuit_breaker():
            self.cancel_existing_quotes()
            self._placed_bid_price = None
            self._placed_ask_price = None
            now_ts = time.time()
            cb_changed = not self._last_circuit_breaker_state
            if cb_changed or (now_ts - self._last_circuit_breaker_log_ts >= 30.0):
                self._last_circuit_breaker_log_ts = now_ts
                self._log_cycle(
                    fair_value=fair_value,
                    market_bid=bid,
                    market_ask=ask,
                    spread_bps=0.0,
                    market_spread_bps=0.0,
                    position_in_book="circuit_breaker",
                )
            self._last_circuit_breaker_state = True
            return

        if self._last_circuit_breaker_state:
            logger.info("Circuit breaker cleared, resuming normal operation")
        self._last_circuit_breaker_state = False

        # Layer 3: position hold-time check (warning path; flatten handled above).
        self.mm_risk.check_position_time(self.inventory_mgr.position)

        risk_ok = self.risk_mgr.check_risk(self.inventory_mgr, fair_value)
        if not risk_ok:
            self.cancel_existing_quotes()
            logger.error("Risk check failed. Quotes canceled.")
            return

        now_ts = time.time()
        if now_ts - self._last_quote_ts < self.config.QUOTE_REFRESH_SECONDS:
            # FIX 2: within refresh window, skip silently - no log spam
            return

        self._last_quote_ts = now_ts
        close_window = max(flatten_before_close - force_flatten_before_close, 1)
        if close_only_mode:
            closing_edge_factor = max(0.0, min(1.0, (minutes_to_close - force_flatten_before_close) / close_window))
        else:
            closing_edge_factor = 1.0
        bid_quote, ask_quote, spread_bps, market_spread_bps, position_in_book = self.calculate_quotes(
            fair_value=fair_value,
            market_bid=bid,
            market_ask=ask,
            close_only_mode=close_only_mode,
            closing_edge_factor=closing_edge_factor,
            sigma_price=self._sigma_tracker.sigma(),
            time_to_close_fraction=self._time_to_close_fraction(),
        )

        effective_bid_quote, effective_ask_quote = self._get_effective_quotes(
            bid_quote,
            ask_quote,
            close_only_mode=close_only_mode,
        )

        if effective_bid_quote is None and effective_ask_quote is None:
            # FIX 2: skipped tight - cancel and log exactly one line per refresh cycle
            self.cancel_existing_quotes()
            self._placed_bid_price = None
            self._placed_ask_price = None
            self._log_cycle(
                fair_value=fair_value,
                market_bid=bid,
                market_ask=ask,
                spread_bps=0.0,
                market_spread_bps=market_spread_bps,
                position_in_book=position_in_book,
            )
            return

        # FIX 4: hold time priority if quote unchanged and both orders still active
        if (
            self._quote_unchanged(effective_bid_quote, self._placed_bid_price, self._tick_size)
            and self._quote_unchanged(effective_ask_quote, self._placed_ask_price, self._tick_size)
            and ((effective_bid_quote is None) or self.active_bid_id is not None)
            and ((effective_ask_quote is None) or self.active_ask_id is not None)
        ):
            self._log_cycle(
                fair_value=fair_value,
                market_bid=bid,
                market_ask=ask,
                spread_bps=spread_bps,
                market_spread_bps=market_spread_bps,
                position_in_book="held_priority",
            )
            return

        self.cancel_existing_quotes()
        self._placed_bid_price = effective_bid_quote
        self._placed_ask_price = effective_ask_quote
        self.place_quotes(ticker=ticker, bid_price=effective_bid_quote, ask_price=effective_ask_quote)

        if not self.config.ENABLE_TRADING:
            logger.info("TRADING DISABLED (dry-run). Quotes not sent.")

        self._log_cycle(
            fair_value=fair_value,
            market_bid=bid,
            market_ask=ask,
            spread_bps=spread_bps,
            market_spread_bps=market_spread_bps,
            position_in_book=position_in_book,
        )

    def _log_cycle(
        self,
        fair_value: float,
        market_bid: float | None,
        market_ask: float | None,
        spread_bps: float | None = None,
        market_spread_bps: float | None = None,
        position_in_book: str | None = None,
    ) -> None:
        """Logs one ASCII-only status line per cycle."""
        if spread_bps is None:
            spread_bps = self.last_spread_bps
        if market_spread_bps is None:
            market_spread_bps = self.last_market_spread_bps
        if position_in_book is None:
            position_in_book = self.last_position_in_book

        total_pnl = self.inventory_mgr.realized_pnl
        if hasattr(self.inventory_mgr, 'unrealized_pnl'):
            total_pnl += self.inventory_mgr.unrealized_pnl
        
        # PASO 4: Obtener OFI para loggeo
        ofi = self.fair_value_calc.get_ofi()
        size_bid = self._calculate_order_size("BUY")
        size_ask = self._calculate_order_size("SELL")

        logger.info(
            "MM cycle | market_bid=%s | market_ask=%s | market_spread_bps=%.2f | fair=%.2f | ofi=%.3f | bid_q=%s | ask_q=%s | our_spread_bps=%.2f | position_in_book=%s | pos=%d | pnl=%.2f | bid_size=%d | ask_size=%d | bid_depth=%d | ask_depth=%d",
            f"{market_bid:.2f}" if market_bid is not None else "NA",
            f"{market_ask:.2f}" if market_ask is not None else "NA",
            market_spread_bps,
            fair_value,
            ofi,
            f"{self.last_bid_quote:.2f}" if self.last_bid_quote is not None else "NA",
            f"{self.last_ask_quote:.2f}" if self.last_ask_quote is not None else "NA",
            spread_bps,
            position_in_book,
            self.inventory_mgr.position,
            total_pnl,
            size_bid,
            size_ask,
            self._bid_depth,
            self._ask_depth,
        )
