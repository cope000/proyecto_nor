"""Equity futures vs spot arbitrage engine for A3 markets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils import setup_logger
from .implied_rate import ImpliedRateCalculator

logger = setup_logger("equity_arb")


@dataclass(slots=True)
class EquityArbPosition:
    """Open arbitrage position tracking."""

    ticker: str
    future_ticker: str
    side: str  # CASH_CARRY or REVERSE_CASH_CARRY
    entry_spot: float
    entry_future: float
    contracts: int
    entry_spread: float
    entry_day: int = 0


class EquityArbEngine:
    """Scans equity futures, generates and executes basis arbitrage signals."""

    def __init__(self, reference_rate_tna: float, min_spread_bps: int, contract_multiplier: float) -> None:
        self.reference_rate_tna = reference_rate_tna
        self.min_spread_pct = min_spread_bps / 100.0
        self.contract_multiplier = contract_multiplier
        self.rate_calc = ImpliedRateCalculator()
        self.positions: list[EquityArbPosition] = []
        self.realized_pnl: float = 0.0

    def scan_equity_futures(
        self,
        instruments_config: list[str],
        futures_data: dict[str, list[dict[str, Any]]],
        spot_data: dict[str, float],
    ) -> list[dict[str, Any]]:
        """Calculates implied rates and spreads for configured equities."""
        rows: list[dict[str, Any]] = []

        for symbol in instruments_config:
            spot = float(spot_data.get(symbol, 0.0) or 0.0)
            futs = futures_data.get(symbol, [])
            if spot <= 0.0:
                rows.append({
                    "ticker": symbol,
                    "spot": 0.0,
                    "future_ticker": "",
                    "future_price": 0.0,
                    "days": 0,
                    "implied_rate": None,
                    "basis": 0.0,
                    "spread_vs_ref": None,
                    "signal": "NO_DATA",
                })
                continue

            for fut in futs:
                fut_ticker = str(fut.get("ticker", ""))
                fut_px = float(fut.get("price", 0.0) or 0.0)
                days = int(fut.get("days", 0) or 0)
                if fut_px <= 0.0 or days <= 0:
                    continue

                implied = self.rate_calc.calculate_implied_rate(spot, fut_px, days)
                basis = self.rate_calc.calculate_basis(spot, fut_px)
                spread = implied - self.reference_rate_tna if implied is not None else None

                signal = "HOLD"
                if spread is not None:
                    if spread > self.min_spread_pct:
                        signal = "CASH_CARRY"
                    elif spread < -self.min_spread_pct:
                        signal = "REVERSE_CASH_CARRY"

                rows.append({
                    "ticker": symbol,
                    "spot": spot,
                    "future_ticker": fut_ticker,
                    "future_price": fut_px,
                    "days": days,
                    "implied_rate": implied,
                    "basis": basis,
                    "spread_vs_ref": spread,
                    "signal": signal,
                })

        rows.sort(key=lambda x: (x["ticker"], x.get("days", 99999)))
        return rows

    def generate_signals(self, scan_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filters actionable arbitrage opportunities."""
        return [
            row for row in scan_results
            if row.get("signal") in ("CASH_CARRY", "REVERSE_CASH_CARRY")
        ]

    def execute_arb(self, signal: dict[str, Any], size: int, day: int = 0) -> dict[str, Any]:
        """Registers an opened arbitrage trade and logs details."""
        pos = EquityArbPosition(
            ticker=signal["ticker"],
            future_ticker=signal["future_ticker"],
            side=signal["signal"],
            entry_spot=float(signal["spot"]),
            entry_future=float(signal["future_price"]),
            contracts=int(size),
            entry_spread=float(signal.get("spread_vs_ref") or 0.0),
            entry_day=day,
        )
        self.positions.append(pos)

        action_fut = "SELL" if pos.side == "CASH_CARRY" else "BUY"
        logger.info(
            "EQUITY ARB: %s %s | %s %d @ %.2f | Spot: %.2f | Implied: %.1f%% | Ref: %.1f%% | Spread: %+0.1f%%",
            pos.side,
            pos.future_ticker,
            action_fut,
            pos.contracts,
            pos.entry_future,
            pos.entry_spot,
            float(signal.get("implied_rate") or 0.0),
            self.reference_rate_tna,
            float(signal.get("spread_vs_ref") or 0.0),
        )

        return {
            "ticker": pos.ticker,
            "future_ticker": pos.future_ticker,
            "side": pos.side,
            "contracts": pos.contracts,
            "entry_spot": pos.entry_spot,
            "entry_future": pos.entry_future,
            "entry_spread": pos.entry_spread,
        }

    def mark_to_market(self, spot_map: dict[str, float], future_map: dict[str, float], day: int) -> float:
        """Marks open positions and closes if spread converges or near expiry."""
        pnl_day = 0.0
        remaining: list[EquityArbPosition] = []

        for pos in self.positions:
            spot = float(spot_map.get(pos.ticker, 0.0) or 0.0)
            fut = float(future_map.get(pos.future_ticker, 0.0) or 0.0)
            if spot <= 0.0 or fut <= 0.0:
                remaining.append(pos)
                continue

            basis_entry = pos.entry_future - pos.entry_spot
            basis_now = fut - spot
            basis_change = basis_now - basis_entry

            # Cash&Carry gains when basis compresses (basis_now < basis_entry).
            side = -1.0 if pos.side == "CASH_CARRY" else 1.0
            pnl = side * basis_change * pos.contracts * self.contract_multiplier
            pnl_day += pnl

            hold_days = day - pos.entry_day
            converged = abs(basis_now) < abs(basis_entry) * 0.25
            timeout = hold_days >= 20

            if converged or timeout:
                self.realized_pnl += pnl
            else:
                remaining.append(pos)

        self.positions = remaining
        return pnl_day
