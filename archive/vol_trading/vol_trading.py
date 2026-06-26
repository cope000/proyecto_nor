"""Core volatility trading logic (IV vs RV + delta hedge) for DLR options."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from vol_config import VolTradingConfig
from .greeks import GreeksCalculator
from .vol_surface import VolSurface


class VolTrader:
    """Implements a volatility risk-premium strategy on futures options."""

    def __init__(
        self,
        config: VolTradingConfig,
        greeks_calc: GreeksCalculator,
        vol_surface: VolSurface,
    ) -> None:
        self.cfg = config
        self.greeks = greeks_calc
        self.surface = vol_surface

        self.position: dict[str, Any] | None = None
        self.realized_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self.open_trades: list[dict[str, Any]] = []

        self.theta_pnl: float = 0.0
        self.vega_pnl: float = 0.0
        self.gamma_pnl: float = 0.0
        self.hedge_pnl: float = 0.0
        self.last_close_day: int | None = None
        self.last_close_reason: str = ""

    def calculate_realized_vol(self, prices: list[float], lookback: int) -> float:
        """Returns annualized close-to-close realized volatility."""
        if len(prices) < lookback + 1:
            return 0.0
        arr = np.asarray(prices[-(lookback + 1):], dtype=float)
        arr = np.maximum(arr, 1e-9)
        log_ret = np.diff(np.log(arr))
        if len(log_ret) < 2:
            return 0.0
        return float(np.std(log_ret, ddof=1) * math.sqrt(252.0))

    def calculate_vrp(self, iv_atm: float, rv: float) -> float:
        """Volatility risk premium in vol points."""
        return iv_atm - rv

    def generate_signal(self, vrp: float, day: int | None = None) -> str:
        """Generates trading signal based on VRP thresholds."""
        if not self.position and self.last_close_day is not None and day is not None:
            if (day - self.last_close_day) < self.cfg.REOPEN_COOLDOWN_DAYS:
                return "COOLDOWN"
        if self.position and abs(vrp) < self.cfg.IV_RV_EXIT_THRESHOLD:
            return "CLOSE"
        if not self.position and vrp > self.cfg.IV_RV_ENTRY_THRESHOLD:
            return "SELL_VOL"
        if not self.position and vrp < -self.cfg.IV_RV_ENTRY_THRESHOLD:
            return "BUY_VOL"
        return "NEUTRAL"

    def construct_straddle(self, future_price: float, expiry_days: int, iv: float) -> dict[str, float]:
        """Constructs ATM straddle and aggregate Greeks for one unit."""
        strike = self._round_to_strike(future_price)
        T = max(expiry_days / 365.0, 1e-6)

        call_price = self.greeks.call_price(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv)
        put_price = self.greeks.put_price(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv)

        delta_net = (
            self.greeks.delta(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv, "C")
            + self.greeks.delta(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv, "P")
        )
        gamma = 2.0 * self.greeks.gamma(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv)
        vega = 2.0 * self.greeks.vega(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv)
        theta = (
            self.greeks.theta(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv, "C")
            + self.greeks.theta(future_price, strike, T, self.cfg.RISK_FREE_RATE, iv, "P")
        )

        return {
            "kind": "straddle",
            "strike": strike,
            "call_price": call_price,
            "put_price": put_price,
            "premium_total": call_price + put_price,
            "delta_net": delta_net,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
        }

    def construct_strangle(
        self,
        future_price: float,
        expiry_days: int,
        iv: float,
        target_delta: float,
    ) -> dict[str, float]:
        """Constructs OTM strangle using target option deltas."""
        T = max(expiry_days / 365.0, 1e-6)
        step = 10.0
        center = self._round_to_strike(future_price)
        candidates = [center + step * i for i in range(-20, 21)]
        candidates = [k for k in candidates if k > 0]

        call_strikes = [k for k in candidates if k >= future_price]
        put_strikes = [k for k in candidates if k <= future_price]
        if not call_strikes or not put_strikes:
            return self.construct_straddle(future_price, expiry_days, iv)

        call_k = min(
            call_strikes,
            key=lambda k: abs(self.greeks.delta(future_price, k, T, self.cfg.RISK_FREE_RATE, iv, "C") - target_delta),
        )
        put_k = min(
            put_strikes,
            key=lambda k: abs(abs(self.greeks.delta(future_price, k, T, self.cfg.RISK_FREE_RATE, iv, "P")) - target_delta),
        )

        call_price = self.greeks.call_price(future_price, call_k, T, self.cfg.RISK_FREE_RATE, iv)
        put_price = self.greeks.put_price(future_price, put_k, T, self.cfg.RISK_FREE_RATE, iv)

        delta_net = (
            self.greeks.delta(future_price, call_k, T, self.cfg.RISK_FREE_RATE, iv, "C")
            + self.greeks.delta(future_price, put_k, T, self.cfg.RISK_FREE_RATE, iv, "P")
        )
        gamma = (
            self.greeks.gamma(future_price, call_k, T, self.cfg.RISK_FREE_RATE, iv)
            + self.greeks.gamma(future_price, put_k, T, self.cfg.RISK_FREE_RATE, iv)
        )
        vega = (
            self.greeks.vega(future_price, call_k, T, self.cfg.RISK_FREE_RATE, iv)
            + self.greeks.vega(future_price, put_k, T, self.cfg.RISK_FREE_RATE, iv)
        )
        theta = (
            self.greeks.theta(future_price, call_k, T, self.cfg.RISK_FREE_RATE, iv, "C")
            + self.greeks.theta(future_price, put_k, T, self.cfg.RISK_FREE_RATE, iv, "P")
        )

        return {
            "kind": "strangle",
            "call_strike": call_k,
            "put_strike": put_k,
            "call_price": call_price,
            "put_price": put_price,
            "premium_total": call_price + put_price,
            "delta_net": delta_net,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
        }

    def calculate_delta_hedge(self, position_delta: float, future_price: float) -> int:
        """Returns futures contracts needed to offset option delta."""
        if future_price <= 0.0:
            return 0
        abs_delta = abs(position_delta)
        if abs_delta < self.cfg.DELTA_HEDGE_THRESHOLD:
            return 0
        units = max(1, int(round(abs_delta)))
        hedge_contracts = -units if position_delta > 0 else units
        return int(hedge_contracts)

    def on_new_data(
        self,
        future_price: float,
        options_data: list[dict],
        prices_history: list[float],
        day: int | None = None,
        default_rv: float | None = None,
    ) -> dict[str, Any]:
        """Processes one data step and returns signal, metrics and risk state."""
        surface = self.surface.build_surface(options_data)
        valid_expiries = [
            exp for exp in sorted(surface.keys())
            if self.cfg.DAYS_TO_EXPIRY_MIN <= exp <= self.cfg.DAYS_TO_EXPIRY_MAX
        ]

        rv = self.calculate_realized_vol(prices_history, self.cfg.RV_LOOKBACK)
        rv_default_used = False
        if len(prices_history) < self.cfg.RV_LOOKBACK + 1 and default_rv is not None:
            rv = float(default_rv)
            rv_default_used = True
        if not valid_expiries:
            return {
                "signal": "NO_DATA",
                "vrp": 0.0,
                "iv": 0.0,
                "rv": rv,
                "rv_default_used": rv_default_used,
                "position_greeks": None,
                "hedge_needed": 0,
                "risk_status": "NO_EXPIRY",
                "daily_pnl": 0.0,
            }

        expiry = min(valid_expiries, key=lambda x: abs(x - 30))
        iv_atm = self.surface.get_atm_iv(expiry)
        vrp = self.calculate_vrp(iv_atm, rv)

        signal = self.generate_signal(vrp, day=day)
        daily_pnl = 0.0
        hedge_needed = 0
        pnl_calc: dict[str, float] | None = None
        close_reason = ""
        opened = False
        opened_structure: dict[str, float] | None = None

        if self.position:
            daily_pnl, pnl_calc = self._mark_to_market_position(future_price, expiry, iv_atm)

            if self.cfg.DELTA_HEDGE_FREQUENCY.lower() == "daily":
                opt_delta_contracts = (
                    self.position["side"]
                    * self.position["greeks"]["delta_net"]
                    * self.position["contracts"]
                )
                current_total_delta = opt_delta_contracts + self.position["hedge_contracts"]
                if abs(current_total_delta) > self.cfg.DELTA_HEDGE_THRESHOLD:
                    new_hedge = self.calculate_delta_hedge(opt_delta_contracts, future_price)
                    hedge_needed = new_hedge - self.position["hedge_contracts"]
                    self.position["hedge_contracts"] = new_hedge

            hold_days = 0
            if day is not None and self.position.get("open_day") is not None:
                hold_days = int(day - self.position["open_day"])

            if signal == "CLOSE":
                close_reason = "vrp_exit"
            elif daily_pnl <= -self.cfg.MAX_DAILY_LOSS:
                close_reason = "max_loss"
            elif hold_days >= self.cfg.MAX_DAYS_IN_TRADE:
                close_reason = "max_days"

        if not self.position and signal in ("SELL_VOL", "BUY_VOL"):
            if self.cfg.PREFERRED_STRATEGY.lower() == "strangle":
                structure = self.construct_strangle(future_price, expiry, iv_atm, self.cfg.STRANGLE_DELTA)
            else:
                structure = self.construct_straddle(future_price, expiry, iv_atm)

            side = -1 if signal == "SELL_VOL" else 1
            self.position = {
                "side": side,
                "contracts": self.cfg.MAX_CONTRACTS,
                "expiry_days": expiry,
                "entry_vrp": vrp,
                "last_iv": iv_atm,
                "last_future": future_price,
                "last_mark": structure["premium_total"],
                "hedge_contracts": 0,
                "structure": structure,
                "greeks": {
                    "delta_net": structure["delta_net"],
                    "gamma": structure["gamma"],
                    "vega": structure["vega"],
                    "theta": structure["theta"],
                },
                "open_day": day,
            }
            opened = True
            opened_structure = structure
            self.open_trades.append({
                "day": day,
                "action": "OPEN",
                "signal": signal,
                "vrp": vrp,
            })

        pos_greeks = None
        if self.position:
            side = self.position["side"]
            contracts = self.position["contracts"]
            g = self.position["greeks"]
            pos_greeks = {
                "delta": side * g["delta_net"] * contracts,
                "gamma": side * g["gamma"] * contracts,
                "vega": side * g["vega"] * contracts,
                "theta": side * g["theta"] * contracts * self.cfg.CONTRACT_MULTIPLIER,
            }

        risk_status = self._risk_status(pos_greeks, daily_pnl)

        if not close_reason and risk_status != "OK" and risk_status != "FLAT":
            if "VEGA_LIMIT" in risk_status:
                close_reason = "vega_limit"
            elif "GAMMA_LIMIT" in risk_status:
                close_reason = "gamma_limit"
            elif "DAILY_LOSS" in risk_status:
                close_reason = "max_loss"

        if close_reason and self.position:
            self.position = None
            self.last_close_day = day
            self.last_close_reason = close_reason
            self.open_trades.append({
                "day": day,
                "action": "CLOSE",
                "signal": signal if signal == "CLOSE" else "RISK_CLOSE",
                "vrp": vrp,
                "reason": close_reason,
            })
            pos_greeks = None
            risk_status = "FLAT"
        elif signal == "CLOSE" and not close_reason:
            self.last_close_day = day
            self.last_close_reason = "vrp_exit"

        return {
            "signal": signal,
            "vrp": vrp,
            "iv": iv_atm,
            "rv": rv,
            "rv_default_used": rv_default_used,
            "position_greeks": pos_greeks,
            "hedge_needed": hedge_needed,
            "risk_status": risk_status,
            "daily_pnl": daily_pnl,
            "pnl_calc": pnl_calc,
            "opened": opened,
            "opened_structure": opened_structure,
            "close_reason": close_reason,
        }

    def _mark_to_market_position(
        self,
        future_price: float,
        expiry_days: int,
        iv: float,
    ) -> tuple[float, dict[str, float]]:
        """Marks current position and updates daily PnL components."""
        if not self.position:
            return 0.0, {
                "prev_premium": 0.0,
                "curr_premium": 0.0,
                "theta_pnl": 0.0,
                "vega_pnl": 0.0,
                "gamma_pnl": 0.0,
                "hedge_pnl": 0.0,
                "daily": 0.0,
            }

        side = self.position["side"]
        contracts = self.position["contracts"]
        mult = self.cfg.CONTRACT_MULTIPLIER
        prev_mark = float(self.position["last_mark"])
        prev_iv = float(self.position["last_iv"])
        prev_f = float(self.position["last_future"])
        prev_expiry_days = int(self.position.get("expiry_days", expiry_days))
        structure = self.position["structure"]

        rebuilt = self._reprice_structure(structure, future_price, expiry_days, iv)
        theta_only = self._reprice_structure(
            structure,
            prev_f,
            max(prev_expiry_days - 1, 1),
            prev_iv,
        )
        vega_only = self._reprice_structure(
            structure,
            prev_f,
            max(prev_expiry_days - 1, 1),
            iv,
        )

        new_mark = float(rebuilt["premium_total"])
        qty = abs(contracts)
        if side < 0:
            # Short options: premium decay (prev > curr) is profit.
            option_pnl = (prev_mark - new_mark) * qty * mult
        else:
            option_pnl = (new_mark - prev_mark) * qty * mult

        hedge_contracts = int(self.position["hedge_contracts"])
        hedge_pnl = hedge_contracts * (future_price - prev_f) * mult

        theta_mark = float(theta_only["premium_total"])
        vega_mark = float(vega_only["premium_total"])
        if side < 0:
            theta_pnl_day = (prev_mark - theta_mark) * qty * mult
            vega_pnl_day = (theta_mark - vega_mark) * qty * mult
            gamma_pnl_day = (vega_mark - new_mark) * qty * mult
        else:
            theta_pnl_day = (theta_mark - prev_mark) * qty * mult
            vega_pnl_day = (vega_mark - theta_mark) * qty * mult
            gamma_pnl_day = (new_mark - vega_mark) * qty * mult

        self.theta_pnl += theta_pnl_day
        self.vega_pnl += vega_pnl_day
        self.gamma_pnl += gamma_pnl_day
        self.hedge_pnl += hedge_pnl

        daily_pnl = option_pnl + hedge_pnl
        self.total_pnl += daily_pnl
        self.realized_pnl = self.total_pnl

        self.position["last_mark"] = new_mark
        self.position["last_iv"] = iv
        self.position["last_future"] = future_price
        self.position["greeks"] = {
            "delta_net": rebuilt["delta_net"],
            "gamma": rebuilt["gamma"],
            "vega": rebuilt["vega"],
            "theta": rebuilt["theta"],
        }
        self.position["structure"] = rebuilt
        self.position["expiry_days"] = expiry_days

        return daily_pnl, {
            "prev_premium": prev_mark,
            "curr_premium": new_mark,
            "theta_pnl": theta_pnl_day,
            "vega_pnl": vega_pnl_day,
            "gamma_pnl": gamma_pnl_day,
            "hedge_pnl": hedge_pnl,
            "daily": daily_pnl,
        }

    def _reprice_structure(
        self,
        structure: dict[str, float],
        future_price: float,
        expiry_days: int,
        iv: float,
    ) -> dict[str, float]:
        """Reprices the exact same option structure with fixed strikes."""
        T = max(expiry_days / 365.0, 1e-6)
        r = self.cfg.RISK_FREE_RATE

        if structure["kind"] == "strangle":
            call_k = float(structure["call_strike"])
            put_k = float(structure["put_strike"])
            call_price = self.greeks.call_price(future_price, call_k, T, r, iv)
            put_price = self.greeks.put_price(future_price, put_k, T, r, iv)
            delta_net = (
                self.greeks.delta(future_price, call_k, T, r, iv, "C")
                + self.greeks.delta(future_price, put_k, T, r, iv, "P")
            )
            gamma = (
                self.greeks.gamma(future_price, call_k, T, r, iv)
                + self.greeks.gamma(future_price, put_k, T, r, iv)
            )
            vega = (
                self.greeks.vega(future_price, call_k, T, r, iv)
                + self.greeks.vega(future_price, put_k, T, r, iv)
            )
            theta = (
                self.greeks.theta(future_price, call_k, T, r, iv, "C")
                + self.greeks.theta(future_price, put_k, T, r, iv, "P")
            )
            return {
                "kind": "strangle",
                "call_strike": call_k,
                "put_strike": put_k,
                "call_price": call_price,
                "put_price": put_price,
                "premium_total": call_price + put_price,
                "delta_net": delta_net,
                "gamma": gamma,
                "vega": vega,
                "theta": theta,
            }

        strike = float(structure["strike"])
        call_price = self.greeks.call_price(future_price, strike, T, r, iv)
        put_price = self.greeks.put_price(future_price, strike, T, r, iv)
        delta_net = (
            self.greeks.delta(future_price, strike, T, r, iv, "C")
            + self.greeks.delta(future_price, strike, T, r, iv, "P")
        )
        gamma = 2.0 * self.greeks.gamma(future_price, strike, T, r, iv)
        vega = 2.0 * self.greeks.vega(future_price, strike, T, r, iv)
        theta = (
            self.greeks.theta(future_price, strike, T, r, iv, "C")
            + self.greeks.theta(future_price, strike, T, r, iv, "P")
        )
        return {
            "kind": "straddle",
            "strike": strike,
            "call_price": call_price,
            "put_price": put_price,
            "premium_total": call_price + put_price,
            "delta_net": delta_net,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
        }

    def _risk_status(self, position_greeks: dict[str, float] | None, daily_pnl: float) -> str:
        """Evaluates main risk limits and returns status string."""
        if not position_greeks:
            return "FLAT"

        vega_ok = abs(position_greeks["vega"]) <= self.cfg.MAX_VEGA_EXPOSURE
        gamma_ok = abs(position_greeks["gamma"]) <= self.cfg.MAX_GAMMA_EXPOSURE
        daily_ok = daily_pnl >= -self.cfg.MAX_DAILY_LOSS

        if vega_ok and gamma_ok and daily_ok:
            return "OK"
        flags = []
        if not vega_ok:
            flags.append("VEGA_LIMIT")
        if not gamma_ok:
            flags.append("GAMMA_LIMIT")
        if not daily_ok:
            flags.append("DAILY_LOSS")
        return "|".join(flags)

    @staticmethod
    def _round_to_strike(price: float, step: float = 10.0) -> float:
        """Rounds a price to nearest listed strike increment."""
        return round(price / step) * step
