"""Implied rate utilities for DLR futures curve."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any


_MONTH_TO_NUM = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}


class ImpliedRateCalculator:
    """Calculates expiry days, basis, and implied rates for DLR futures."""

    def _parse_ticker(self, ticker: str) -> tuple[int, int] | None:
        """Parses a simple DLR ticker like DLR/ABR26 into (year, month)."""
        m = re.fullmatch(r"DLR/([A-Z]{3})(\d{2})", ticker.strip().upper())
        if not m:
            return None
        month_txt, year_txt = m.groups()
        month = _MONTH_TO_NUM.get(month_txt)
        if month is None:
            return None
        return 2000 + int(year_txt), month

    def calculate_days_to_expiry(self, ticker: str) -> int:
        """Returns calendar days to last business day of ticker month, or -1 if invalid."""
        parsed = self._parse_ticker(ticker)
        if parsed is None:
            return -1

        year, month = parsed
        last_day = calendar.monthrange(year, month)[1]
        expiry = date(year, month, last_day)
        while expiry.weekday() >= 5:
            expiry = date.fromordinal(expiry.toordinal() - 1)

        days = (expiry - date.today()).days
        return days

    def calculate_implied_rate(self, spot: float, future: float, days: int) -> float | None:
        """Returns implied annualized TNA percentage from spot/future relation."""
        if days <= 0 or spot <= 0 or future <= 0:
            return None
        return ((future / spot) - 1.0) * (365.0 / float(days)) * 100.0

    def calculate_basis(self, spot: float, future: float) -> float:
        """Returns basis in ARS as future - spot."""
        return future - spot

    def scan_all_rates(self, spot: float, futures_data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Builds implied rate curve entries from futures snapshots."""
        out: list[dict[str, Any]] = []
        for ticker, data in futures_data.items():
            days = int(data.get("days", self.calculate_days_to_expiry(ticker)))
            bid = data.get("bid")
            ask = data.get("ask")
            last = data.get("last")

            implied_bid = self.calculate_implied_rate(spot, float(bid), days) if bid else None
            implied_ask = self.calculate_implied_rate(spot, float(ask), days) if ask else None
            implied_last = self.calculate_implied_rate(spot, float(last), days) if last else None

            ref_px = float(last) if last else (float(bid) if bid else (float(ask) if ask else 0.0))
            basis = self.calculate_basis(spot, ref_px) if ref_px > 0 else 0.0

            out.append(
                {
                    "ticker": ticker,
                    "days": days,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "spot": spot,
                    "implied_rate_bid": implied_bid,
                    "implied_rate_ask": implied_ask,
                    "implied_rate_last": implied_last,
                    "basis": basis,
                }
            )

        out.sort(key=lambda x: x.get("days", 99999))
        return out
