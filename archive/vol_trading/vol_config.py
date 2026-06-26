"""Configuration for DLR options volatility trading strategy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class VolTradingConfig:
    """Runtime parameters for volatility trading on DLR options."""

    UNDERLYING: str = "DLR"
    RISK_FREE_RATE: float = 0.30
    RV_LOOKBACK: int = 21
    IV_RV_ENTRY_THRESHOLD: float = 0.03
    IV_RV_EXIT_THRESHOLD: float = 0.005
    PREFERRED_STRATEGY: str = "straddle"  # straddle | strangle
    STRANGLE_DELTA: float = 0.25
    MAX_CONTRACTS: int = 3
    MAX_VEGA_EXPOSURE: float = 500000.0
    MAX_GAMMA_EXPOSURE: float = 100000.0
    DELTA_HEDGE_THRESHOLD: float = 0.10
    DELTA_HEDGE_FREQUENCY: str = "daily"
    MAX_DAILY_LOSS: float = 200000.0
    DAYS_TO_EXPIRY_MIN: int = 15
    DAYS_TO_EXPIRY_MAX: int = 90
    MAX_DAYS_IN_TRADE: int = 30
    REOPEN_COOLDOWN_DAYS: int = 1
    ENABLE_TRADING: bool = False
    CONTRACT_MULTIPLIER: float = 1000.0
