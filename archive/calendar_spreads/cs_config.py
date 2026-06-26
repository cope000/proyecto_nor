"""Configuration for Calendar Spread strategy on DLR futures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CalendarSpreadConfig:
    """All tunable parameters for the DLR calendar spread strategy."""

    INSTRUMENT: str = "DLR"
    # Near leg: how many months ahead from today (inclusive range)
    NEAR_MONTHS_RANGE: tuple[int, int] = (1, 3)
    # Far leg is this many months after the near leg
    FAR_MONTHS_OFFSET: int = 3
    # Entry z-score threshold (enter when |z| > Z_SCORE_ENTRY)
    Z_SCORE_ENTRY: float = 2.0
    # Exit z-score threshold (close when |z| < Z_SCORE_EXIT)
    Z_SCORE_EXIT: float = 0.5
    # Lookback window in trading days to compute spread mean/std
    LOOKBACK_WINDOW: int = 20
    # Max contracts per leg
    MAX_CONTRACTS: int = 3
    # Max simultaneous open spreads
    MAX_OPEN_SPREADS: int = 2
    # Kill-switch: max daily loss in ARS
    MAX_DAILY_LOSS: float = 50000.0
    # Stop-loss: max MTM loss per individual spread (ARS)
    MAX_LOSS_PER_SPREAD: float = 100000.0
    # Stop-loss: max total strategy MTM loss before closing all (ARS)
    MAX_STRATEGY_MTM_LOSS: float = 500000.0
    # Max total notional of open spreads (ARS).
    # DLR contract: precio ~1200-1500 * 3 contratos * 1000 multiplier = ~4M/spread.
    # Con 5M se permite 1 spread activo y se bloquea el 2do (limite efectivo = 1 a la vez).
    MAX_NOCIONAL_TOTAL: float = 5000000.0
    # Time stop: max days to hold a spread before forcing close
    MAX_HOLDING_DAYS: int = 30
    # Scan interval in seconds (for live runner)
    SCAN_INTERVAL_SECONDS: float = 10.0
    # Set to True to send real orders
    ENABLE_TRADING: bool = False
    # Notional per contract (ARS per unit of price)
    CONTRACT_MULTIPLIER: float = 1000.0
