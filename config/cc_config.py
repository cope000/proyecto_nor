"""Configuration for Cash & Carry strategy."""

from dataclasses import dataclass


@dataclass(slots=True)
class CCConfig:
    """Parameters for Cash & Carry and Reverse Cash & Carry scans."""

    REFERENCE_RATE_TNA: float = 30.0
    MIN_SPREAD_BPS: int = 200
    MAX_CONTRACTS_PER_TRADE: int = 5
    MAX_TOTAL_POSITION: int = 20
    MAX_DAILY_LOSS: float = 50000.0
    SCAN_INTERVAL_SECONDS: float = 10.0
    ENABLE_TRADING: bool = False
    MONTHS_AHEAD_MIN: int = 1
    MONTHS_AHEAD_MAX: int = 12
    CONTRACT_MULTIPLIER: int = 1000
    TICK_SIZE: float = 0.50
