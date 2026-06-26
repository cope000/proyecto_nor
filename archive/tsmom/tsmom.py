"""Core asymmetric time-series momentum logic."""

from __future__ import annotations

import numpy as np


class TSMOMSignal:
    """Builds asymmetric long/short momentum signals from price history."""

    def calculate_return(self, prices: list[float], window: int) -> float:
        """Returns period return price[-1] / price[-window] - 1, else 0.0."""
        if window <= 0 or len(prices) < window:
            return 0.0
        base = float(prices[-window])
        last = float(prices[-1])
        if base <= 0:
            return 0.0
        return (last / base) - 1.0

    def generate_signal(self, prices: list[float], long_window: int, short_window: int) -> float:
        """Returns -1, 0, or +1 using asymmetric slow-long / fast-short logic."""
        ret_long = self.calculate_return(prices, long_window)
        ret_short = self.calculate_return(prices, short_window)

        if ret_long > 0:
            return 1.0
        if ret_short < 0:
            return -1.0
        return 0.0

    def get_signal_strength(self, prices: list[float], long_window: int, short_window: int) -> float:
        """Returns volatility-normalized signal strength between 0.0 and 2.0."""
        if len(prices) < max(3, short_window):
            return 0.0

        signal = self.generate_signal(prices, long_window, short_window)
        if signal == 0.0:
            return 0.0

        returns = np.diff(np.asarray(prices, dtype=float)) / np.asarray(prices[:-1], dtype=float)
        realized_vol = float(np.std(returns, ddof=1) * np.sqrt(252.0)) if returns.size > 1 else 0.0
        realized_vol = max(realized_vol, 0.01)

        if signal > 0:
            raw = abs(self.calculate_return(prices, long_window)) / realized_vol
        else:
            raw = abs(self.calculate_return(prices, short_window)) / realized_vol
        return float(min(2.0, max(0.0, raw)))
