"""Simple moving-average regime filter for TSMOM."""

from __future__ import annotations


class RegimeFilter:
    """Applies a soft trend regime filter using fast and slow moving averages."""

    def __init__(self, enabled: bool, ma_fast: int, ma_slow: int) -> None:
        self.enabled = enabled
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.prices: list[float] = []
        self._history: list[str] = []

    def update(self, price: float) -> str:
        """Updates internal price history and returns BULLISH, BEARISH, or NEUTRAL."""
        self.prices.append(float(price))
        fast_slice = self.prices[-self.ma_fast :] if len(self.prices) >= 1 else self.prices
        slow_slice = self.prices[-self.ma_slow :] if len(self.prices) >= 1 else self.prices
        ma_fast = sum(fast_slice) / len(fast_slice)
        ma_slow = sum(slow_slice) / len(slow_slice)

        if ma_slow <= 0:
            regime = "NEUTRAL"
        else:
            diff_pct = abs(ma_fast - ma_slow) / ma_slow
            if diff_pct < 0.005:
                regime = "NEUTRAL"
            elif ma_fast > ma_slow:
                regime = "BULLISH"
            else:
                regime = "BEARISH"

        self._history.append(regime)
        return regime

    def apply_filter(self, signal: float, regime: str) -> float:
        """Returns attenuated signal under adverse regime, otherwise unchanged."""
        if not self.enabled:
            return signal
        if signal == 1.0 and regime == "BEARISH":
            return 0.5
        if signal == -1.0 and regime == "BULLISH":
            return -0.5
        return signal

    def get_regime_history(self) -> list[str]:
        """Returns stored regime history."""
        return list(self._history)
