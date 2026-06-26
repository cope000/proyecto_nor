"""Calendar spread engine — z-score mean reversion on a single near/far pair."""

from __future__ import annotations

import logging
import statistics
from collections import deque
from typing import Any

from config.cs_config import CalendarSpreadConfig
from utils.ticker_roller import days_to_expiry

logger = logging.getLogger(__name__)


class CalendarSpreadEngine:
    """Tracks prices, computes z-score signals, and manages position/PnL for a
    single calendar spread pair (near_ticker vs far_ticker).

    Spread definition: spread = near_mid - far_mid
      - SELL_SPREAD (z > +entry): spread is wide → sell near, buy far.
      - BUY_SPREAD  (z < -entry): spread is narrow → buy near, sell far.
    """

    def __init__(self, config: CalendarSpreadConfig) -> None:
        self._cfg = config
        self._near_bid: float | None = None
        self._near_ask: float | None = None
        self._near_last: float | None = None
        self._far_bid: float | None = None
        self._far_ask: float | None = None
        self._far_last: float | None = None
        self._history: deque[float] = deque(maxlen=config.lookback_window)
        # position > 0 means BUY_SPREAD open, < 0 means SELL_SPREAD open
        self._position: int = 0
        self._entry_spread: float = 0.0
        self._entry_qty: int = 0
        self._pnl_realized: float = 0.0

    # ------------------------------------------------------------------ #
    #  Price ingestion                                                     #
    # ------------------------------------------------------------------ #

    def update_prices(
        self,
        near_bid: float | None,
        near_ask: float | None,
        near_last: float | None,
        far_bid: float | None,
        far_ask: float | None,
        far_last: float | None,
    ) -> None:
        """Stores the latest prices for both legs."""
        if near_bid is not None:
            self._near_bid = near_bid
        if near_ask is not None:
            self._near_ask = near_ask
        if near_last is not None:
            self._near_last = near_last
        if far_bid is not None:
            self._far_bid = far_bid
        if far_ask is not None:
            self._far_ask = far_ask
        if far_last is not None:
            self._far_last = far_last

    # ------------------------------------------------------------------ #
    #  Derived prices                                                      #
    # ------------------------------------------------------------------ #

    def _near_mid(self) -> float | None:
        b, a = self._near_bid, self._near_ask
        if b and a and b > 0 and a > 0:
            return (b + a) / 2.0
        return self._near_last if self._near_last and self._near_last > 0 else None

    def _far_mid(self) -> float | None:
        b, a = self._far_bid, self._far_ask
        if b and a and b > 0 and a > 0:
            return (b + a) / 2.0
        return self._far_last if self._far_last and self._far_last > 0 else None

    # ------------------------------------------------------------------ #
    #  Public computed properties                                          #
    # ------------------------------------------------------------------ #

    def get_spread(self) -> float:
        """Returns near_mid - far_mid. Returns 0 if prices unavailable."""
        nm = self._near_mid()
        fm = self._far_mid()
        if nm is None or fm is None:
            return 0.0
        return nm - fm

    def get_implied_rate(self) -> float:
        """Annualised implied forward TNA between the two legs (%).
        Formula: (far_mid / near_mid - 1) × (365 / days_between) × 100
        """
        nm = self._near_mid()
        fm = self._far_mid()
        if nm is None or fm is None or nm <= 0:
            return 0.0
        try:
            near_exp = days_to_expiry(self._cfg.near_ticker)
            far_exp = days_to_expiry(self._cfg.far_ticker)
            days_between = far_exp - near_exp
        except Exception:
            days_between = 30  # fallback
        if days_between <= 0:
            return 0.0
        return ((fm / nm) - 1.0) * (365.0 / days_between) * 100.0

    # ------------------------------------------------------------------ #
    #  History & z-score                                                   #
    # ------------------------------------------------------------------ #

    def update_history(self) -> None:
        """Appends current spread to rolling history."""
        s = self.get_spread()
        if s != 0.0:
            self._history.append(s)

    def get_zscore(self) -> float | None:
        """Returns z-score if >= 5 observations exist, else None."""
        if len(self._history) < 5:
            return None
        window = list(self._history)
        mean = statistics.mean(window)
        try:
            std = statistics.stdev(window)
        except statistics.StatisticsError:
            return None
        if std < 1e-9:
            return None
        return (window[-1] - mean) / std

    # ------------------------------------------------------------------ #
    #  Signal                                                              #
    # ------------------------------------------------------------------ #

    def get_signal(self) -> str:
        """Returns trading signal based on z-score and current position.

        Returns:
            "BUY_SPREAD" | "SELL_SPREAD" | "CLOSE" | "HOLD"
        """
        # Filtro 1: z-score disponible
        zscore = self.get_zscore()
        if zscore is None:
            return "HOLD"

        # Filtro 2: tasa implícita dentro del rango normal.
        # get_implied_rate() devuelve 0.0 cuando los precios no están disponibles;
        # en ese caso omitimos el filtro para no bloquear la salida de posiciones.
        rate = self.get_implied_rate()
        if rate != 0.0:
            if rate < self._cfg.min_implied_rate:
                # Tasa muy baja — spread demasiado angosto, no hay oportunidad.
                return "HOLD"
            if rate > self._cfg.max_implied_rate:
                # Tasa muy alta — anomalía o error de precios, no entrar.
                return "HOLD"

        pos = self._position
        entry = self._cfg.z_score_entry
        exit_th = self._cfg.z_score_exit

        if abs(zscore) < exit_th and pos != 0:
            return "CLOSE"
        if zscore > entry and pos == 0:
            return "SELL_SPREAD"
        if zscore < -entry and pos == 0:
            return "BUY_SPREAD"
        return "HOLD"

    # ------------------------------------------------------------------ #
    #  Fill handling                                                       #
    # ------------------------------------------------------------------ #

    def on_fill(
        self,
        side_near: str,
        side_far: str,
        price_near: float,
        price_far: float,
        qty: int,
    ) -> None:
        """Updates position and realized PnL after a fill.

        Args:
            side_near: "BUY" or "SELL" for the near leg.
            side_far:  "BUY" or "SELL" for the far leg.
            price_near: Fill price of near leg.
            price_far:  Fill price of far leg.
            qty: Number of contracts per leg.
        """
        filled_spread = price_near - price_far

        if self._position == 0:
            # Opening a new spread position
            self._entry_spread = filled_spread
            self._entry_qty = qty
            if side_near == "BUY":
                self._position = qty   # BUY_SPREAD: long near / short far
            else:
                self._position = -qty  # SELL_SPREAD: short near / long far
        else:
            # Closing (or partially closing) existing position
            if self._position > 0:
                # Was BUY_SPREAD: pnl = (exit_spread - entry_spread) * qty * mult
                pnl = (filled_spread - self._entry_spread) * qty * self._cfg.contract_multiplier
            else:
                # Was SELL_SPREAD: pnl = (entry_spread - exit_spread) * qty * mult
                pnl = (self._entry_spread - filled_spread) * qty * self._cfg.contract_multiplier
            self._pnl_realized += pnl
            self._position = 0
            self._entry_spread = 0.0
            self._entry_qty = 0

    # ------------------------------------------------------------------ #
    #  Reset                                                               #
    # ------------------------------------------------------------------ #

    def reset_pnl(self) -> None:
        """Resets realized PnL and position to zero. Call on startup to discard
        stale state from unconfirmed fills of a previous session."""
        self._pnl_realized = 0.0
        self._position = 0
        self._entry_spread = 0.0
        self._entry_qty = 0
        logger.warning("Engine PnL and position reset")

    # ------------------------------------------------------------------ #
    #  State snapshot                                                      #
    # ------------------------------------------------------------------ #

    def get_state(self) -> dict[str, Any]:
        """Returns full observable state for logging and dashboard."""
        nm = self._near_mid() or 0.0
        fm = self._far_mid() or 0.0
        spread = self.get_spread()
        z = self.get_zscore()
        signal = self.get_signal()

        # Unrealised PnL
        pnl_unrealized = 0.0
        if self._position != 0 and self._entry_qty > 0:
            if self._position > 0:  # BUY_SPREAD
                pnl_unrealized = (spread - self._entry_spread) * self._entry_qty * self._cfg.contract_multiplier
            else:                   # SELL_SPREAD
                pnl_unrealized = (self._entry_spread - spread) * self._entry_qty * self._cfg.contract_multiplier

        rate = self.get_implied_rate()
        return {
            "near_mid": nm,
            "far_mid": fm,
            "spread": spread,
            "implied_rate": rate,
            "implied_rate_ok": (
                rate != 0.0
                and self._cfg.min_implied_rate <= rate <= self._cfg.max_implied_rate
            ),
            "zscore": z,
            "signal": signal,
            "position": self._position,
            "pnl_realized": self._pnl_realized,
            "pnl_unrealized": pnl_unrealized,
            "history_len": len(self._history),
        }
