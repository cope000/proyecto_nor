"""Implied volatility surface builder for DLR options."""

from __future__ import annotations

from datetime import date
from typing import Any

from utils import setup_logger
from .greeks import GreeksCalculator

logger = setup_logger("vol_surface")


class VolSurface:
    """Builds and queries implied volatility surface by expiry and strike."""

    def __init__(self, greeks_calc: GreeksCalculator, risk_free_rate: float) -> None:
        self.greeks = greeks_calc
        self.r = risk_free_rate
        self.surface: dict[int, dict[str, Any]] = {}
        self.last_underlying: float = 0.0

    def build_surface(self, options_data: list[dict]) -> dict[int, dict[str, Any]]:
        """Builds vol surface using option market prices and Black-76 implied vol."""
        self.surface = {}
        if not options_data:
            return self.surface

        first = options_data[0]
        self.last_underlying = float(first.get("future_price", 0.0) or 0.0)

        for opt in options_data:
            strike = float(opt.get("strike", 0.0) or 0.0)
            expiry_days = int(opt.get("days_to_expiry", 0) or 0)
            option_type = str(opt.get("option_type", "")).upper()
            future_price = float(opt.get("future_price", self.last_underlying) or 0.0)
            bid = opt.get("bid")
            ask = opt.get("ask")
            last = opt.get("last")

            if strike <= 0.0 or expiry_days <= 0 or option_type not in ("C", "P") or future_price <= 0.0:
                continue

            market_price = 0.0
            if bid and ask and bid > 0 and ask > 0:
                market_price = 0.5 * (float(bid) + float(ask))
            elif last and last > 0:
                market_price = float(last)
            elif bid and bid > 0:
                market_price = float(bid)
            elif ask and ask > 0:
                market_price = float(ask)

            if market_price <= 0.0:
                continue

            T = expiry_days / 365.0
            iv = self.greeks.implied_vol(market_price, future_price, strike, T, self.r, option_type)
            if iv <= 0.0:
                continue

            bucket = self.surface.setdefault(expiry_days, {
                "expiry_days": expiry_days,
                "expiry_label": str(opt.get("expiry_label", f"{expiry_days}d")),
                "future_price": future_price,
                "strikes": {},
            })
            by_strike = bucket["strikes"].setdefault(strike, {"C": None, "P": None})
            by_strike[option_type] = iv

        for expiry_days, bucket in self.surface.items():
            fwd = float(bucket["future_price"])
            for strike, ivs in bucket["strikes"].items():
                iv_values = [v for v in (ivs.get("C"), ivs.get("P")) if isinstance(v, float)]
                avg_iv = sum(iv_values) / len(iv_values) if iv_values else 0.0
                ivs["iv_avg"] = avg_iv
                ivs["moneyness"] = strike / fwd - 1.0 if fwd > 0 else 0.0
            logger.debug("Surface loaded expiry=%sd strikes=%d", expiry_days, len(bucket["strikes"]))
        return self.surface

    def get_atm_iv(self, expiry_days: int) -> float:
        """Returns ATM IV for nearest available expiry bucket."""
        if not self.surface:
            return 0.0
        expiry = self._nearest_expiry(expiry_days)
        bucket = self.surface[expiry]
        fwd = float(bucket["future_price"])
        strikes = list(bucket["strikes"].keys())
        if not strikes or fwd <= 0:
            return 0.0
        atm_strike = min(strikes, key=lambda k: abs(k - fwd))
        return float(bucket["strikes"][atm_strike].get("iv_avg", 0.0))

    def get_skew(self, expiry_days: int) -> float:
        """Returns simple 25-delta-like skew proxy: put OTM IV - call OTM IV."""
        if not self.surface:
            return 0.0
        expiry = self._nearest_expiry(expiry_days)
        bucket = self.surface[expiry]
        fwd = float(bucket["future_price"])
        strikes = sorted(bucket["strikes"].keys())
        if len(strikes) < 3 or fwd <= 0:
            return 0.0

        atm = min(strikes, key=lambda k: abs(k - fwd))
        puts_otm = [k for k in strikes if k < atm]
        calls_otm = [k for k in strikes if k > atm]
        if not puts_otm or not calls_otm:
            return 0.0

        put_k = max(puts_otm)
        call_k = min(calls_otm)
        put_iv = float(bucket["strikes"][put_k].get("P") or 0.0)
        call_iv = float(bucket["strikes"][call_k].get("C") or 0.0)
        if put_iv <= 0.0 or call_iv <= 0.0:
            return 0.0
        return put_iv - call_iv

    def get_term_structure(self) -> dict[int, float]:
        """Returns ATM IV by expiry (days)."""
        out: dict[int, float] = {}
        for expiry in sorted(self.surface.keys()):
            out[expiry] = self.get_atm_iv(expiry)
        return out

    def print_surface(self) -> None:
        """Prints a compact table of IV surface for available expiries."""
        if not self.surface:
            logger.info("VOL SURFACE: sin datos")
            return

        expiries = sorted(self.surface.keys())
        first_exp = expiries[0]
        fwd = self.surface[first_exp]["future_price"]

        all_strikes = sorted({k for exp in expiries for k in self.surface[exp]["strikes"].keys()})
        logger.info("============================================================")
        logger.info("VOL SURFACE DLR - %s", date.today().isoformat())
        logger.info("============================================================")

        header = "Strike | Moneyness | " + " | ".join(
            self.surface[e]["expiry_label"].ljust(8) for e in expiries
        )
        logger.info(header)

        for strike in all_strikes[:12]:
            mny = (strike / fwd - 1.0) if fwd > 0 else 0.0
            mny_txt = "ATM" if abs(mny) < 0.002 else f"{mny * 100:+.1f}%"
            row_ivs = []
            for exp in expiries:
                iv = self.surface[exp]["strikes"].get(strike, {}).get("iv_avg")
                row_ivs.append(f"{(iv * 100):5.1f}%" if iv else "  --- ")
            logger.info("%6.0f | %9s | %s", strike, mny_txt.rjust(9), " | ".join(row_ivs))

        atm_iv = self.get_atm_iv(first_exp)
        skew = self.get_skew(first_exp)
        logger.info("============================================================")
        logger.info("ATM IV: %.1f%% | Skew: %+.1f%%", atm_iv * 100.0, skew * 100.0)
        logger.info("============================================================")

    def _nearest_expiry(self, target_expiry_days: int) -> int:
        """Returns nearest expiry key in loaded surface."""
        return min(self.surface.keys(), key=lambda x: abs(x - target_expiry_days))
