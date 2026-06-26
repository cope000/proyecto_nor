"""Inventory and PnL accounting for market making."""

from __future__ import annotations


class InventoryManager:
    """Tracks net position, average entry, and realized/unrealized PnL."""

    def __init__(self, max_position: int, skew_factor_bps: float) -> None:
        self.max_position = max_position
        self.skew_factor_bps = skew_factor_bps
        self.position: int = 0
        self.avg_entry_price: float = 0.0
        self.realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.traded_contracts: int = 0

    def on_fill(self, side: str, price: float, size: int) -> None:
        """Updates position and PnL from a fill event."""
        if size <= 0 or price <= 0:
            return

        trade_qty = size if side.upper() == "BUY" else -size
        old_pos = self.position
        old_avg = self.avg_entry_price
        new_pos = old_pos + trade_qty
        self.traded_contracts += size

        if old_pos == 0 or (old_pos > 0 and trade_qty > 0) or (old_pos < 0 and trade_qty < 0):
            total_qty = abs(old_pos) + abs(trade_qty)
            if total_qty > 0:
                self.avg_entry_price = ((abs(old_pos) * old_avg) + (abs(trade_qty) * price)) / total_qty
            self.position = new_pos
            if self.position == 0:
                self.avg_entry_price = 0.0
            return

        closed_qty = min(abs(old_pos), abs(trade_qty))
        if old_pos > 0 and trade_qty < 0:
            self.realized_pnl += (price - old_avg) * closed_qty
        elif old_pos < 0 and trade_qty > 0:
            self.realized_pnl += (old_avg - price) * closed_qty

        self.position = new_pos
        if self.position == 0:
            self.avg_entry_price = 0.0
        elif (old_pos > 0 > self.position) or (old_pos < 0 < self.position):
            self.avg_entry_price = price

    def get_skew_bps(self) -> float:
        """Returns inventory skew in bps."""
        return self.skew_factor_bps * float(self.position)

    def get_unrealized_pnl(self, current_price: float) -> float:
        """Returns unrealized PnL at current mark price."""
        if self.position == 0 or current_price <= 0 or self.avg_entry_price <= 0:
            self.unrealized_pnl = 0.0
            return self.unrealized_pnl

        if self.position > 0:
            self.unrealized_pnl = (current_price - self.avg_entry_price) * self.position
        else:
            self.unrealized_pnl = (self.avg_entry_price - current_price) * abs(self.position)
        return self.unrealized_pnl
