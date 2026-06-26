"""Cash & Carry and Reverse Cash & Carry strategy core."""

from __future__ import annotations

from typing import Any

from config.cc_config import CCConfig
from core.order_manager import send_limit_order
from core.utils import setup_logger

logger = setup_logger("cash_carry")


class CashCarryStrategy:
    """Scans implied rate dislocations and manages synthetic positions."""

    def __init__(self, config: CCConfig) -> None:
        self.config = config
        self.positions: dict[str, list[dict[str, Any]]] = {}
        self.total_pnl: float = 0.0

    def _round_price(self, px: float) -> float:
        """Rounds to DLR tick size."""
        tick = self.config.TICK_SIZE
        return round(px / tick) * tick

    def _current_exposure(self) -> int:
        """Returns net contracts exposure across all tickers."""
        total = 0
        for trades in self.positions.values():
            for t in trades:
                total += int(t.get("size", 0))
        return total

    def scan_opportunities(self, rates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Returns opportunities where implied rate deviates enough from reference."""
        opportunities: list[dict[str, Any]] = []
        threshold_pct = self.config.MIN_SPREAD_BPS / 100.0

        for row in rates:
            implied = row.get("implied_rate_last")
            if implied is None:
                continue

            spread_vs_ref = float(implied) - self.config.REFERENCE_RATE_TNA
            signal: str | None = None
            if spread_vs_ref > threshold_pct:
                signal = "CASH_CARRY"
            elif spread_vs_ref < -threshold_pct:
                signal = "REVERSE_CASH_CARRY"

            if signal is None:
                continue

            exp_pnl = self.calculate_expected_pnl(
                spot=float(row.get("spot", 0.0)),
                future=float(row.get("last", 0.0)),
                days=int(row.get("days", 0)),
                contracts=1,
            )

            opportunities.append(
                {
                    "ticker": row["ticker"],
                    "signal": signal,
                    "implied_rate": float(implied),
                    "reference_rate": self.config.REFERENCE_RATE_TNA,
                    "spread": spread_vs_ref,
                    "expected_pnl_per_contract": exp_pnl,
                    "future_price": row.get("last"),
                    "spot": row.get("spot"),
                    "days": row.get("days"),
                }
            )

        return opportunities

    def calculate_expected_pnl(self, spot: float, future: float, days: int, contracts: int) -> float:
        """Estimates ARS PnL net of financing for a carry trade."""
        if spot <= 0 or future <= 0 or days <= 0 or contracts <= 0:
            return 0.0

        gross = abs(future - spot) * contracts * self.config.CONTRACT_MULTIPLIER
        financing = (
            spot
            * contracts
            * self.config.CONTRACT_MULTIPLIER
            * (self.config.REFERENCE_RATE_TNA / 100.0)
            * (days / 365.0)
        )
        return gross - financing

    def execute_trade(self, ticker: str, signal: str, price: float, size: int, implied_rate: float, spread: float, days: int, spot: float) -> dict[str, Any]:
        """Executes or simulates trade and records position."""
        exposure = self._current_exposure()
        if exposure + size > self.config.MAX_TOTAL_POSITION:
            return {"status": "REJECTED", "reason": "max_total_position"}

        side = "SELL" if signal == "CASH_CARRY" else "BUY"
        px = self._round_price(price)
        expected = self.calculate_expected_pnl(spot=spot, future=px, days=days, contracts=size)

        response: dict[str, Any] | None = None
        if self.config.ENABLE_TRADING:
            response = send_limit_order(ticker=ticker, side=side, price=px, size=size)

        trade = {
            "ticker": ticker,
            "signal": signal,
            "side": side,
            "size": size,
            "price": px,
            "implied_rate": implied_rate,
            "spread": spread,
            "expected_pnl": expected,
            "days": days,
            "spot": spot,
            "response": response,
        }
        self.positions.setdefault(ticker, []).append(trade)

        logger.info(
            "TRADE: %s %s | %s %d @ %.2f | Implied: %.2f%% | Ref: %.2f%% | Spread: %.2f%% | Expected PnL: %.2f",
            signal,
            ticker,
            side,
            size,
            px,
            implied_rate,
            self.config.REFERENCE_RATE_TNA,
            spread,
            expected,
        )

        return {"status": "OK", "trade": trade}

    def get_portfolio_summary(self) -> dict[str, Any]:
        """Returns a summary of open synthetic positions and estimated PnL."""
        per_ticker: dict[str, dict[str, Any]] = {}
        total_expected = 0.0
        total_contracts = 0

        for ticker, trades in self.positions.items():
            ticker_contracts = sum(int(t["size"]) for t in trades)
            ticker_expected = sum(float(t["expected_pnl"]) for t in trades)
            per_ticker[ticker] = {
                "trades": len(trades),
                "contracts": ticker_contracts,
                "estimated_pnl": ticker_expected,
            }
            total_contracts += ticker_contracts
            total_expected += ticker_expected

        return {
            "positions": per_ticker,
            "estimated_pnl_total": total_expected,
            "contracts_total": total_contracts,
            "reference_rate_tna": self.config.REFERENCE_RATE_TNA,
        }
