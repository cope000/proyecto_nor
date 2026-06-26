"""Configuration for Calendar Spread strategy (version 1 — z-score mean reversion)."""

from __future__ import annotations

from dataclasses import dataclass
from utils.ticker_roller import get_active_ticker


@dataclass
class CalendarSpreadConfig:
    """All tunable parameters for a single DLR calendar spread pair."""

    # Par de contratos activo (patas explícitas)
    near_ticker: str = "DLR/MAY26"
    far_ticker: str = "DLR/JUN26"

    # Parámetros de señal z-score
    z_score_entry: float = 1.5
    z_score_exit: float = 0.3
    lookback_window: int = 30

    # Sizing
    max_contracts: int = 2
    max_open_spreads: int = 1

    # Risk limits (ARS)
    max_daily_loss: float = 20_000.0
    max_loss_per_spread: float = 8_000.0
    max_holding_days: int = 3

    # Filtro de tasa implícita para DLR MAY/JUN (% TNA)
    # Si la tasa implícita del spread está fuera de este rango, no entrar.
    min_implied_rate: float = 8.0
    max_implied_rate: float = 40.0

    # Límite de posición global compartida con MM en near_ticker
    # El CS no puede superar este límite combinado con el MM en DLR/MAY26
    near_global_limit: int = 10

    # Operativo
    enable_trading: bool = True
    scan_interval_seconds: int = 10
    contract_multiplier: float = 1_000.0

    # Log
    log_file: str = "logs/run_cs_dlr.log"
