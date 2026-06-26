"""Risk checks and kill switch for market making."""

from __future__ import annotations

from typing import Any

from core.utils import setup_logger
from .inventory_manager import InventoryManager

logger = setup_logger("risk_manager")


class RiskManager:
    """Applies daily loss and position risk limits."""

    def __init__(self, max_daily_loss: float, max_position: int) -> None:
        self.max_daily_loss = max_daily_loss
        self.max_position = max_position
        self.daily_pnl: float = 0.0
        self.is_killed: bool = False

    def check_risk(self, inventory: InventoryManager, current_price: float) -> bool:
        """Returns True if risk limits are respected, False otherwise."""
        unreal = inventory.get_unrealized_pnl(current_price)
        self.daily_pnl = inventory.realized_pnl + unreal

        if -self.daily_pnl > self.max_daily_loss:
            self.kill_switch()
            logger.error(
                "Risk breach: daily loss %.2f exceeds max %.2f",
                -self.daily_pnl,
                self.max_daily_loss,
            )
            return False

        if abs(inventory.position) > self.max_position:
            logger.error(
                "Risk breach: position %d exceeds max %d",
                inventory.position,
                self.max_position,
            )
            return False

        return not self.is_killed

    def kill_switch(self) -> None:
        """Activates kill switch to stop trading actions."""
        if not self.is_killed:
            logger.error("KILL SWITCH ACTIVATED")
        self.is_killed = True

    def get_status(self, inventory: InventoryManager) -> dict[str, Any]:
        """Returns summary dictionary of current risk state."""
        return {
            "daily_pnl": self.daily_pnl,
            "realized_pnl": inventory.realized_pnl,
            "unrealized_pnl": inventory.unrealized_pnl,
            "position": inventory.position,
            "is_killed": self.is_killed,
            "max_daily_loss": self.max_daily_loss,
            "max_position": self.max_position,
        }
