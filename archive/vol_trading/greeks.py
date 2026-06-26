"""Black-76 Greeks and implied volatility utilities for futures options."""

from __future__ import annotations

import math

from scipy.stats import norm


class GreeksCalculator:
    """Numerically-stable Black-76 pricing and Greeks calculator."""

    _EPS: float = 1e-12

    def d1_d2(self, F: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
        """Returns Black-76 d1 and d2 for futures options."""
        F_safe = max(F, self._EPS)
        K_safe = max(K, self._EPS)
        T_safe = max(T, self._EPS)
        sigma_safe = max(sigma, self._EPS)

        vol_sqrt_t = sigma_safe * math.sqrt(T_safe)
        d1 = (math.log(F_safe / K_safe) + 0.5 * sigma_safe * sigma_safe * T_safe) / vol_sqrt_t
        d2 = d1 - vol_sqrt_t
        return d1, d2

    def call_price(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        """Returns Black-76 call option price."""
        if T <= self._EPS or sigma <= self._EPS:
            return math.exp(-r * max(T, 0.0)) * max(F - K, 0.0)
        d1, d2 = self.d1_d2(F, K, T, r, sigma)
        df = math.exp(-r * T)
        return df * (F * norm.cdf(d1) - K * norm.cdf(d2))

    def put_price(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        """Returns Black-76 put option price."""
        if T <= self._EPS or sigma <= self._EPS:
            return math.exp(-r * max(T, 0.0)) * max(K - F, 0.0)
        d1, d2 = self.d1_d2(F, K, T, r, sigma)
        df = math.exp(-r * T)
        return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))

    def delta(self, F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
        """Returns Black-76 delta for call ('C') or put ('P')."""
        opt = option_type.upper()
        if T <= self._EPS or sigma <= self._EPS:
            if opt == "C":
                return math.exp(-r * max(T, 0.0)) if F > K else 0.0
            return -math.exp(-r * max(T, 0.0)) if F < K else 0.0

        d1, _ = self.d1_d2(F, K, T, r, sigma)
        df = math.exp(-r * T)
        if opt == "C":
            return df * norm.cdf(d1)
        if opt == "P":
            return df * (norm.cdf(d1) - 1.0)
        raise ValueError("option_type must be 'C' or 'P'")

    def gamma(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        """Returns Black-76 gamma."""
        if T <= self._EPS or sigma <= self._EPS or F <= self._EPS:
            return 0.0
        d1, _ = self.d1_d2(F, K, T, r, sigma)
        df = math.exp(-r * T)
        return df * norm.pdf(d1) / (F * sigma * math.sqrt(T))

    def vega(self, F: float, K: float, T: float, r: float, sigma: float) -> float:
        """Returns Black-76 vega per 1.00 sigma unit."""
        if T <= self._EPS or sigma <= self._EPS:
            return 0.0
        d1, _ = self.d1_d2(F, K, T, r, sigma)
        df = math.exp(-r * T)
        return F * df * norm.pdf(d1) * math.sqrt(T)

    def theta(self, F: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
        """Returns 1-day theta using stable finite-difference approximation."""
        if T <= self._EPS:
            return 0.0
        dt = min(1.0 / 365.0, max(T - self._EPS, self._EPS))
        opt = option_type.upper()
        if opt == "C":
            p_now = self.call_price(F, K, T, r, sigma)
            p_next = self.call_price(F, K, max(T - dt, self._EPS), r, sigma)
        elif opt == "P":
            p_now = self.put_price(F, K, T, r, sigma)
            p_next = self.put_price(F, K, max(T - dt, self._EPS), r, sigma)
        else:
            raise ValueError("option_type must be 'C' or 'P'")
        return p_next - p_now

    def implied_vol(
        self,
        market_price: float,
        F: float,
        K: float,
        T: float,
        r: float,
        option_type: str,
        tol: float = 1e-4,
        max_iter: int = 100,
    ) -> float:
        """Computes implied volatility via robust bisection in [0.01, 5.0]."""
        if market_price <= 0.0 or T <= self._EPS or F <= self._EPS or K <= self._EPS:
            return 0.0

        low = 0.01
        high = 5.0
        opt = option_type.upper()

        def _price(sig: float) -> float:
            if opt == "C":
                return self.call_price(F, K, T, r, sig)
            if opt == "P":
                return self.put_price(F, K, T, r, sig)
            raise ValueError("option_type must be 'C' or 'P'")

        p_low = _price(low)
        p_high = _price(high)

        if market_price <= p_low:
            return low
        if market_price >= p_high:
            return high

        for _ in range(max_iter):
            mid = 0.5 * (low + high)
            p_mid = _price(mid)
            err = p_mid - market_price
            if abs(err) <= tol:
                return mid
            if err > 0:
                high = mid
            else:
                low = mid
        return 0.5 * (low + high)
