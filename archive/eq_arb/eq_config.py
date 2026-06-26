"""Configuration for equity futures vs spot arbitrage strategy."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class EquityArbConfig:
    """Runtime parameters for equity future-spot arbitrage."""

    INSTRUMENTS: list[str] = field(default_factory=lambda: ["GGAL", "PAMP", "YPFD"])
    REFERENCE_RATE_TNA: float = 30.0
    MIN_SPREAD_BPS: int = 150
    MAX_CONTRACTS: int = 10
    MAX_TOTAL_POSITION: int = 30
    MAX_DAILY_LOSS: float = 100000.0
    SCAN_INTERVAL_SECONDS: float = 10.0
    ENABLE_TRADING: bool = False
    CONTRACT_MULTIPLIER: float = 100.0
    MAX_NOCIONAL_PER_TICKER: float = 3000000.0
