"""Fair value and volatility estimation utilities."""

from __future__ import annotations

from collections import deque
import math
from statistics import stdev

from core.utils import setup_logger

logger = setup_logger("fair_value")


class FairValueCalculator:
    """Calculates a smoothed fair value from top-of-book prices."""

    def __init__(self, alpha: float, tick_size: float = 0.0, direct_midpoint_max_ticks: float = 0.0) -> None:
        self.alpha = alpha
        self.tick_size = tick_size
        self.direct_midpoint_max_ticks = direct_midpoint_max_ticks
        self._fair_value: float | None = None
        self._last_price: float | None = None
        self._mids: deque[float] = deque(maxlen=2000)
        self._last_source: str = "stale"
        # OFI (Order Flow Imbalance) tracking with tick rule
        self._ofi_window: int = 10  # últimos N ticks para OFI
        self._last_prices: deque[float] = deque(maxlen=self._ofi_window + 1)
        self._ofi_alpha: float = 0.3  # sensibilidad del ajuste OFI
        self._current_ofi: float = 0.0  # OFI actual en rango [-1, +1]

    def set_last_price(self, last_price: float | None) -> None:
        """Stores latest last-trade price used as fallback."""
        if last_price is not None and last_price > 0:
            self._last_price = float(last_price)

    def update(self, bid: float | None, ask: float | None, last: float | None = None) -> float:
        """Updates fair value using robust single-side logic and EWMA smoothing."""
        self.set_last_price(last)

        mid: float | None = None
        bid_ok = bid is not None and bid > 0
        ask_ok = ask is not None and ask > 0
        last_ok = self._last_price is not None and self._last_price > 0
        use_direct_midpoint = False

        if bid_ok and ask_ok:
            mid = (float(bid) + float(ask)) / 2.0
            if self.tick_size > 0:
                spread_ticks = (float(ask) - float(bid)) / self.tick_size
                use_direct_midpoint = spread_ticks <= self.direct_midpoint_max_ticks
            self._last_source = "mid_direct" if use_direct_midpoint else "bid+ask"
        elif bid_ok:
            mid = float(bid)
            if last_ok:
                mid = 0.7 * mid + 0.3 * float(self._last_price)
            self._last_source = "bid_only"
        elif ask_ok:
            mid = float(ask)
            if last_ok:
                mid = 0.7 * mid + 0.3 * float(self._last_price)
            self._last_source = "ask_only"
        elif last_ok:
            mid = float(self._last_price)
            self._last_source = "last_only"

        if mid is None:
            self._last_source = "stale"
            logger.debug("fair value source=stale fair=%.2f", self._fair_value or 0.0)
            return self._fair_value or 0.0

        self._mids.append(mid)
        if use_direct_midpoint:
            self._fair_value = mid
        elif self._fair_value is None:
            self._fair_value = mid
        else:
            self._fair_value = self.alpha * mid + (1.0 - self.alpha) * self._fair_value

        # PASO 2: Tick rule para Order Flow Imbalance (OFI)
        self._last_prices.append(last if last is not None else mid)
        
        ofi = 0.0
        if len(self._last_prices) >= 2:
            ticks: list[int] = []
            prices = list(self._last_prices)
            for i in range(1, len(prices)):
                if prices[i] > prices[i - 1]:
                    ticks.append(1)
                elif prices[i] < prices[i - 1]:
                    ticks.append(-1)
                else:
                    ticks.append(ticks[-1] if ticks else 0)
            if ticks:
                ofi = float(sum(ticks)) / float(len(ticks))  # rango [-1, +1]
        
        self._current_ofi = ofi
        
        # Ajustar fair value con OFI (usar tick_size=0.50 fijo para DLR)
        tick_size_ofi = self.tick_size if self.tick_size > 0 else 0.50
        ofi_adjustment = self._ofi_alpha * ofi * tick_size_ofi
        self._fair_value = self._fair_value + ofi_adjustment
        
        logger.info("fair value source=%s fair=%.2f ofi=%.3f adjustment=%.4f", self._last_source, self._fair_value, ofi, ofi_adjustment)
        return self._fair_value

    def get_fair_value(self) -> float:
        """Returns latest fair value."""
        return self._fair_value or 0.0

    def get_recent_volatility(self, window: int) -> float:
        """Returns recent volatility in bps based on percent mid-price changes."""
        if window < 3 or len(self._mids) < window:
            return 0.0

        mids = list(self._mids)[-window:]
        returns: list[float] = []
        for i in range(1, len(mids)):
            prev = mids[i - 1]
            curr = mids[i]
            if prev <= 0:
                continue
            returns.append((curr - prev) / prev)

        if len(returns) < 2:
            return 0.0

        return stdev(returns) * 10000.0

    def get_last_source(self) -> str:
        """Returns the source used in the last update call."""
        return self._last_source

    def get_ofi(self) -> float:
        """PASO 3: Retorna el OFI actual en rango [-1, +1] para observabilidad."""
        return self._current_ofi


def reservation_price(
    mid: float,
    net_position: int,
    gamma: float,
    sigma: float,
    time_to_close: float,
) -> float:
    """Calcula reservation price segun Avellaneda-Stoikov.

    Args:
        mid: Fair value actual del instrumento.
        net_position: Inventario neto (positivo=long, negativo=short).
        gamma: Aversion al riesgo (tipicamente 0.01 a 0.1).
        sigma: Volatilidad reciente del mid (unidades de precio).
        time_to_close: Fraccion de dia restante hasta cierre (0.0 a 1.0).

    Returns:
        Precio de reserva (reservation price).
    """
    ttc = max(0.0, min(1.0, float(time_to_close)))
    return float(mid) - float(net_position) * float(gamma) * (float(sigma) ** 2) * ttc


def optimal_spread(
    gamma: float,
    sigma: float,
    time_to_close: float,
    kappa: float = 1.5,
) -> float:
    """Calcula spread optimo segun Avellaneda-Stoikov simplificado.

    Args:
        gamma: Aversion al riesgo (>0).
        sigma: Volatilidad reciente del mid (unidades de precio).
        time_to_close: Fraccion de dia restante hasta cierre (0.0 a 1.0).
        kappa: Parametro de profundidad/llegada de ordenes (>0).

    Returns:
        Spread total optimo (ask-bid) en unidades de precio.
    """
    gamma_eff = max(float(gamma), 1e-9)
    kappa_eff = max(float(kappa), 1e-9)
    ttc = max(0.0, min(1.0, float(time_to_close)))
    sigma_sq = float(sigma) ** 2

    return (gamma_eff * sigma_sq * ttc) + ((2.0 / gamma_eff) * math.log(1.0 + (gamma_eff / kappa_eff)))
