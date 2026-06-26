"""Calendar spread engine for DLR futures mean-reversion strategy."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from utils import setup_logger

logger = setup_logger("calendar_spread")


@dataclass(slots=True)
class SpreadPosition:
    """Represents an open calendar spread position."""

    pair_id: str
    signal: str  # "BUY_SPREAD" or "SELL_SPREAD"
    near_ticker: str
    far_ticker: str
    entry_near_price: float
    entry_far_price: float
    entry_spread: float
    entry_z_score: float
    contracts: int
    multiplier: float
    open_day: int


class CalendarSpreadEngine:
    """Identifies DLR calendar spread pairs, generates signals, and tracks P&L.

    Spread definition: spread = far_price - near_price
      - SELL_SPREAD: z_score > +threshold → spread is wide, expect compression
        → sell far leg, buy near leg
      - BUY_SPREAD: z_score < -threshold → spread is narrow, expect expansion
        → buy far leg, sell near leg
    """

    def __init__(self, z_entry: float, z_exit: float, lookback: int, max_contracts: int,
                 max_open: int, multiplier: float,
                 max_loss_per_spread: float = 100000.0,
                 max_strategy_mtm_loss: float = 500000.0,
                 max_nocional_total: float = 3000000.0,
                 max_holding_days: int = 30) -> None:
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.lookback = lookback
        self.max_contracts = max_contracts
        self.max_open = max_open
        self.multiplier = multiplier
        self.max_loss_per_spread = max_loss_per_spread
        self.max_strategy_mtm_loss = max_strategy_mtm_loss
        self.max_nocional_total = max_nocional_total
        self.max_holding_days = max_holding_days
        self.open_positions: list[SpreadPosition] = []
        self.realized_pnl: float = 0.0
        self.spread_histories: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ #
    #  Pair identification                                                 #
    # ------------------------------------------------------------------ #

    def identify_spread_pairs(self, futures: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Builds all valid (near, far) calendar spread pairs from a sorted futures list.

        Args:
            futures: Sorted list of dicts with keys 'ticker', 'price', 'expiry_days'.
                     'expiry_days' is calendar days from today to expiry.

        Returns:
            List of pair dicts with near/far metadata and current spread.
        """
        pairs: list[dict[str, Any]] = []
        valid = [f for f in futures if f.get("price") and f["price"] > 0]
        for i, near in enumerate(valid):
            for far in valid[i + 1:]:
                days_between = far["expiry_days"] - near["expiry_days"]
                if days_between <= 0:
                    continue
                spread = far["price"] - near["price"]
                fwd_rate = self.calculate_implied_forward_rate(
                    near["price"], far["price"], days_between
                )
                pair_id = f"{_ticker_label(near['ticker'])}-{_ticker_label(far['ticker'])}"
                pairs.append({
                    "pair_id": pair_id,
                    "near_ticker": near["ticker"],
                    "far_ticker": far["ticker"],
                    "near_price": near["price"],
                    "far_price": far["price"],
                    "spread": spread,
                    "days_between": days_between,
                    "fwd_rate_tna": fwd_rate,
                })
        return pairs

    # ------------------------------------------------------------------ #
    #  Statistics                                                          #
    # ------------------------------------------------------------------ #

    def calculate_spread_stats(
        self, spread_history: list[float], lookback: int
    ) -> dict[str, float]:
        """Computes mean, std, z-score and range over lookback window.

        Args:
            spread_history: Full history of daily spread observations.
            lookback: Number of trailing observations to use.

        Returns:
            Dict with mean, std, z_score, min, max. Returns zeros if insufficient data.
        """
        window = spread_history[-lookback:] if len(spread_history) >= lookback else []
        if len(window) < 2:
            return {"mean": 0.0, "std": 0.0, "z_score": 0.0, "min": 0.0, "max": 0.0, "n": len(window)}
        arr = np.asarray(window, dtype=float)
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))
        current = spread_history[-1]
        z_score = (current - mean) / std if std > 1e-9 else 0.0
        return {
            "mean": mean,
            "std": std,
            "z_score": z_score,
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "n": len(window),
        }

    # ------------------------------------------------------------------ #
    #  Signal generation                                                   #
    # ------------------------------------------------------------------ #

    def generate_signals(self, pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Generates entry/exit signals for each spread pair.

        Updates internal spread_histories with today's spread value.

        Args:
            pairs: Output of identify_spread_pairs().

        Returns:
            List of signal dicts with pair info, z_score, and action.
        """
        signals: list[dict[str, Any]] = []
        open_ids = {p.pair_id for p in self.open_positions}
        open_count = len(self.open_positions)  # Contador para evitar generar >MAX_OPEN en el mismo scan

        for pair in pairs:
            pid = pair["pair_id"]
            spread = pair["spread"]
            self.spread_histories.setdefault(pid, []).append(spread)

            stats = self.calculate_spread_stats(self.spread_histories[pid], self.lookback)
            z = stats["z_score"]
            n = stats["n"]
            has_history = n >= self.lookback

            action = "WAIT" if not has_history else "HOLD"

            if has_history:
                if pid in open_ids:
                    # Check exit condition
                    action = "CLOSE" if abs(z) < self.z_exit else "HOLD"
                else:
                    # Check entry condition (respecting open-spread and nocional limits)
                    nocional_new = pair["near_price"] * self.max_contracts * self.multiplier
                    nocional_ok = (self.get_nocional_total() + nocional_new) <= self.max_nocional_total
                    slots_ok = open_count < self.max_open  # Usar contador del scan, no len() que es stale
                    if z > self.z_entry and slots_ok and nocional_ok:
                        action = "SELL_SPREAD"
                        open_count += 1  # Incrementar contador para siguientes pares
                    elif z < -self.z_entry and slots_ok and nocional_ok:
                        action = "BUY_SPREAD"
                        open_count += 1  # Incrementar contador para siguientes pares

            signals.append({
                "pair_id": pid,
                "near_ticker": pair["near_ticker"],
                "far_ticker": pair["far_ticker"],
                "near_price": pair["near_price"],
                "far_price": pair["far_price"],
                "spread": spread,
                "fwd_rate_tna": pair["fwd_rate_tna"],
                "days_between": pair["days_between"],
                "z_score": z,
                "mean": stats["mean"],
                "std": stats["std"],
                "n_history": n,
                "action": action,
            })
        return signals

    # ------------------------------------------------------------------ #
    #  Forward rate                                                        #
    # ------------------------------------------------------------------ #

    def calculate_implied_forward_rate(
        self, near_price: float, far_price: float, days_between: int
    ) -> float:
        """Computes the annualised implied forward TNA between two DLR futures.

        Formula: ((far / near) - 1) * (365 / days_between) * 100

        Args:
            near_price: Price of the near-dated future.
            far_price: Price of the far-dated future.
            days_between: Calendar days between the two expiries.

        Returns:
            Annualised forward rate as a percentage (e.g. 28.5 means 28.5% TNA).
        """
        if near_price <= 0 or days_between <= 0:
            return 0.0
        return ((far_price / near_price) - 1.0) * (365.0 / days_between) * 100.0

    # ------------------------------------------------------------------ #
    #  Position management                                                 #
    # ------------------------------------------------------------------ #

    def on_fill_spread(
        self,
        pair_id: str,
        near_ticker: str,
        far_ticker: str,
        signal: str,
        near_price: float,
        far_price: float,
        days_between: int,
        size: int,
        current_day: int,
        z_score: float,
    ) -> None:
        """Records an opened spread position.

        Args:
            pair_id: Unique pair identifier string.
            near_ticker: Ticker of the near leg.
            far_ticker: Ticker of the far leg.
            signal: "BUY_SPREAD" or "SELL_SPREAD".
            near_price: Fill price of near leg.
            far_price: Fill price of far leg.
            days_between: Calendar days between legs.
            size: Number of contracts per leg.
            current_day: Simulation day index.
            z_score: Z-score at entry.
        """
        spread = far_price - near_price
        fwd = self.calculate_implied_forward_rate(near_price, far_price, days_between)
        pos = SpreadPosition(
            pair_id=pair_id,
            signal=signal,
            near_ticker=near_ticker,
            far_ticker=far_ticker,
            entry_near_price=near_price,
            entry_far_price=far_price,
            entry_spread=spread,
            entry_z_score=z_score,
            contracts=size,
            multiplier=self.multiplier,
            open_day=current_day,
        )
        self.open_positions.append(pos)
        logger.info(
            "SPREAD OPEN: %s %s | Near: %.2f | Far: %.2f | Spread: %.2f | Z: %.2f | Fwd Rate: %.1f%%",
            signal, pair_id, near_price, far_price, spread, z_score, fwd,
        )

    def on_close_spread(
        self,
        pair_id: str,
        near_price: float,
        far_price: float,
        current_day: int,
    ) -> float:
        """Closes an open spread position and books realized P&L.

        P&L logic:
          SELL_SPREAD: we were long near / short far → profit if spread narrows.
            pnl = (entry_spread - exit_spread) * contracts * multiplier
          BUY_SPREAD: we were short near / long far → profit if spread widens.
            pnl = (exit_spread - entry_spread) * contracts * multiplier

        Args:
            pair_id: Pair to close.
            near_price: Current near leg price.
            far_price: Current far leg price.
            current_day: Current simulation day.

        Returns:
            Realized P&L of the closed spread (ARS).
        """
        exit_spread = far_price - near_price
        pnl = 0.0
        remaining: list[SpreadPosition] = []
        for pos in self.open_positions:
            if pos.pair_id == pair_id:
                if pos.signal == "SELL_SPREAD":
                    pnl = (pos.entry_spread - exit_spread) * pos.contracts * pos.multiplier
                else:
                    pnl = (exit_spread - pos.entry_spread) * pos.contracts * pos.multiplier
                self.realized_pnl += pnl
                holding = current_day - pos.open_day
                logger.info(
                    "SPREAD CLOSE: %s | Entry spr=%.2f | Exit spr=%.2f | Holding=%d days | PNL=%+.2f",
                    pair_id, pos.entry_spread, exit_spread, holding, pnl,
                )
            else:
                remaining.append(pos)
        self.open_positions = remaining
        return pnl


    # ------------------------------------------------------------------ #
    #  Risk management                                                    #
    # ------------------------------------------------------------------ #

    def compute_spread_mtm(self, pos: SpreadPosition, near_price: float, far_price: float) -> float:
        """Mark-to-market PnL for a single open spread position.

        Args:
            pos: The open spread position.
            near_price: Current near leg market price.
            far_price: Current far leg market price.

        Returns:
            Unrealised PnL in ARS.
        """
        cur_spread = far_price - near_price
        if pos.signal == "SELL_SPREAD":
            return (pos.entry_spread - cur_spread) * pos.contracts * pos.multiplier
        return (cur_spread - pos.entry_spread) * pos.contracts * pos.multiplier

    def get_nocional_total(self) -> float:
        """Returns total notional (ARS) of all open spread positions (near leg only)."""
        return sum(p.entry_near_price * p.contracts * p.multiplier for p in self.open_positions)

    def check_risk_limits(
        self, price_map: dict[str, float], current_day: int
    ) -> list[tuple[str, str]]:
        """Checks per-spread and strategy-level risk limits.

        Evaluates in priority order:
          1. Strategy total MTM stop → close ALL positions.
          2. Per-spread stop-loss (MTM < -max_loss_per_spread).
          3. Time stop (holding > max_holding_days).

        Args:
            price_map: Mapping ticker → current price.
            current_day: Current simulation day index.

        Returns:
            List of (pair_id, reason) tuples for positions that must be closed.
        """
        if not self.open_positions:
            return []

        # Compute per-position and total MTM
        pos_mtm: dict[str, float] = {}
        total_mtm = 0.0
        for pos in self.open_positions:
            near_p = price_map.get(pos.near_ticker, pos.entry_near_price)
            far_p = price_map.get(pos.far_ticker, pos.entry_far_price)
            mtm = self.compute_spread_mtm(pos, near_p, far_p)
            pos_mtm[pos.pair_id] = mtm
            total_mtm += mtm

        # 1. Strategy MTM stop — close everything at once
        if total_mtm < -self.max_strategy_mtm_loss:
            logger.warning(
                "STRATEGY STOP: Total MTM=%+.0fK > limit=-%.0fK | CLOSING ALL SPREADS",
                total_mtm / 1000.0, self.max_strategy_mtm_loss / 1000.0,
            )
            return [(pos.pair_id, "strategy_mtm_stop") for pos in self.open_positions]

        # 2 & 3. Per-spread checks
        to_close: list[tuple[str, str]] = []
        for pos in self.open_positions:
            mtm = pos_mtm[pos.pair_id]
            holding = current_day - pos.open_day
            if mtm < -self.max_loss_per_spread:
                logger.warning(
                    "STOP-LOSS SPREAD %s | MTM=%+.0fK > limit=-%.0fK | CLOSING",
                    pos.pair_id, mtm / 1000.0, self.max_loss_per_spread / 1000.0,
                )
                to_close.append((pos.pair_id, "stop_loss"))
            elif holding >= self.max_holding_days:
                logger.warning(
                    "TIME-STOP SPREAD %s | Holding=%d days >= %d | CLOSING",
                    pos.pair_id, holding, self.max_holding_days,
                )
                to_close.append((pos.pair_id, "time_stop"))
        return to_close


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

def _ticker_label(ticker: str) -> str:
    """Extracts the expiry label from a DLR ticker, e.g. 'DLR/ABR26' → 'ABR26'."""
    return ticker.split("/", 1)[-1] if "/" in ticker else ticker
