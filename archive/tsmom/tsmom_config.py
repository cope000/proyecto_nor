"""Configuration for Time-Series Momentum strategy."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class InstrumentConfig:
    """Instrument-specific sizing and contract settings."""

    name: str
    allocation: float
    contract_multiplier: float
    max_position_contracts: int
    max_leverage: float
    vol_target: float


@dataclass(slots=True)
class TSMOMConfig:
    """Parameters for TSMOM across DLR and RFX20 futures."""

    LONG_WINDOW: int = 63
    SHORT_WINDOW: int = 21
    SIGNAL_THRESHOLD: float = 0.0
    VOL_LOOKBACK: int = 21
    VOL_TARGET: float = 0.15
    MIN_LEVERAGE: float = 0.1
    STRENGTH_SCALE_MIN: float = 0.4
    STRENGTH_SCALE_MAX: float = 1.5
    CAPITAL_ARS: float = 10000000.0
    REGIME_FILTER_ENABLED: bool = True
    REGIME_MA_FAST: int = 10
    REGIME_MA_SLOW: int = 50
    REBALANCE_FREQUENCY: str = "daily"
    ENABLE_TRADING: bool = False
    INSTRUMENTS_CONFIG: dict[str, InstrumentConfig] = field(
        default_factory=lambda: {
            "DLR": InstrumentConfig(
                name="DLR",
                allocation=0.60,
                contract_multiplier=1000.0,
                max_position_contracts=5,
                max_leverage=2.0,
                vol_target=0.15,
            ),
            "RFX20": InstrumentConfig(
                name="RFX20",
                allocation=0.40,
                contract_multiplier=1.0,
                max_position_contracts=800,
                max_leverage=2.0,
                vol_target=0.45,
            ),
        }
    )

    @property
    def INSTRUMENTS(self) -> list[str]:
        """Returns instrument names preserving config keys order."""
        return list(self.INSTRUMENTS_CONFIG.keys())
