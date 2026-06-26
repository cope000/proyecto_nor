"""Configuration for the market making bot (multi-instrument)."""

import dataclasses
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class InstrumentConfig:
    """Configuration specific to one instrument."""

    name: str
    ticker_pattern: str
    tick_size: float
    contract_size: float
    contract_size_unit: str  # "USD" for DLR, "ARS_per_bp" for CAUC

    # Quoting parameters
    base_spread_bps: float = 7.0
    min_edge_bps: float = 1.0
    min_spread_bps: float = 3.0
    improve_spread_threshold_bps: float = 0.0
    improve_tick_offset: int = 1

    # Position limits
    max_position: int = 5
    order_size: int = 1
    max_order_size: int = 1

    # Fair value
    ema_alpha: float = 0.1
    direct_midpoint_max_ticks: float = 0.0

    # Inventory
    inventory_skew_factor: float = 5.0
    inventory_skew_enabled: bool = False
    gamma: float = 0.05
    kappa: float = 1.5
    sigma_window: int = 50
    sigma_floor: float = 0.5
    max_inventory: int = 10

    # Risk
    max_daily_loss: float = 100_000.0

    # Session / EOD
    market_open_hour: int = 10
    market_open_minute: int = 0
    market_close_hour: int = 17
    market_close_minute: int = 0
    flatten_before_close_minutes: int = 15
    flatten_aggressive_minutes_before_close: int = 15
    flatten_aggressive_tick_offset: int = 2
    force_flatten_before_close_minutes: int = 5
    eod_flatten_ticks: int = 2

    # Adverse Selection Detection
    adverse_detection_enabled: bool = False
    adverse_window: int = 10
    adverse_imbalance_threshold: float = 0.6
    adverse_spread_multiplier: float = 1.5
    adverse_cooldown_fills: int = 5


# --- DLR Futures ---
# PROTOCOLO DE VALIDACIÓN - inventory_skew_enabled=True
# Duración mínima: 60 minutos de rueda activa
#
# CHECK 1 - Arranque (primeros 5 min):
#   - Log muestra "Initial position applied" si hay posición recuperada
#   - Log muestra "InventorySkew" en DEBUG cada ciclo de quoting
#   - Dashboard muestra Skew activo: ✅
#
# CHECK 2 - Skew funcionando (min 10-30):
#   - Con posición neta = 0: reservation_price ≈ mid (diferencia < sigma_floor)
#   - Con posición neta > 0: reservation_price < mid (bot quiere vender)
#   - Con posición neta < 0: reservation_price > mid (bot quiere comprar)
#   - sigma crece con la volatilidad del libro, nunca baja de sigma_floor
#
# CHECK 3 - Hard limit (si se activa):
#   - Log muestra WARNING "Hard inventory limit reached"
#   - Bot deja de cotizar en el lado que agranda posición
#   - Bot sigue cotizando en el lado que reduce posición
#
# CHECK 4 - Cierre de sesión (últimos 10 min):
#   - ttc tiende a 0 → reservation_price converge al mid
#   - spread_opt se reduce (menos tiempo = menos riesgo)
#   - EOD flatten se activa normalmente según lógica existente
#
# Si algún CHECK falla: setear inventory_skew_enabled=False en config
# y reiniciar el bot. No hay riesgo de posición: el skew es solo
# desplazamiento de quotes, no modifica la lógica de flatten ni de órdenes.
DLR_CONFIG = InstrumentConfig(
    name="DLR Futures",
    ticker_pattern="DLR",
    tick_size=0.50,
    contract_size=1000.0,
    contract_size_unit="USD",
    base_spread_bps=4.5,
    min_edge_bps=1.0,
    min_spread_bps=3.0,
    improve_spread_threshold_bps=8.0,
    improve_tick_offset=1,
    max_position=5,
    order_size=1,
    max_order_size=2,
    ema_alpha=0.7,
    direct_midpoint_max_ticks=2.0,
    inventory_skew_factor=3.0,
    inventory_skew_enabled=True,
    gamma=0.02,
    kappa=8.0,
    sigma_window=50,
    sigma_floor=0.5,
    max_inventory=15,
    max_daily_loss=50_000.0,
    market_open_hour=10,
    market_open_minute=0,
    market_close_hour=15,
    market_close_minute=0,
    flatten_before_close_minutes=15,
    flatten_aggressive_minutes_before_close=15,
    flatten_aggressive_tick_offset=2,
    force_flatten_before_close_minutes=5,
    eod_flatten_ticks=2,
    adverse_detection_enabled=True,
    adverse_window=8,
    adverse_imbalance_threshold=0.7,
    adverse_spread_multiplier=2.0,
    adverse_cooldown_fills=3,
)

# --- CAUC Futures (Tasa de Caucion) ---
CAUC_CONFIG = InstrumentConfig(
    name="CAUC Futures (Tasa Caucion)",
    ticker_pattern="CAUC",
    tick_size=0.01,
    contract_size=8219.0,
    contract_size_unit="ARS_per_bp",
    base_spread_bps=15.0,
    min_edge_bps=2.0,
    min_spread_bps=5.0,
    max_position=10,
    order_size=1,
    ema_alpha=0.1,
    direct_midpoint_max_ticks=0.0,
    inventory_skew_factor=5.0,
    max_daily_loss=50_000.0,
    market_open_hour=10,
    market_open_minute=0,
    market_close_hour=15,
    market_close_minute=0,
    flatten_before_close_minutes=15,
    force_flatten_before_close_minutes=5,
    eod_flatten_ticks=2,
)

# --- SOJ.ROS Futures (Soja Rosario) ---
SOJ_CONFIG = InstrumentConfig(
    name="SOJ Rosario Mayo 2026",
    ticker_pattern="SOJ.ROS",
    tick_size=0.10,
    contract_size=100.0,
    contract_size_unit="USD_per_ton",
    base_spread_bps=4.5,
    min_edge_bps=1.0,
    min_spread_bps=8.0,
    improve_spread_threshold_bps=6.0,
    improve_tick_offset=1,
    max_position=5,
    order_size=1,
    max_order_size=1,
    ema_alpha=0.3,
    direct_midpoint_max_ticks=3.0,
    inventory_skew_factor=1.5,
    inventory_skew_enabled=True,
        gamma=0.01,
        kappa=8.0,
    sigma_window=40,
    sigma_floor=0.30,
    max_inventory=8,
    max_daily_loss=50_000.0,
    market_open_hour=11,
    market_open_minute=0,
    market_close_hour=17,
    market_close_minute=0,
    flatten_before_close_minutes=10,
    flatten_aggressive_minutes_before_close=15,
    flatten_aggressive_tick_offset=2,
    force_flatten_before_close_minutes=5,
    eod_flatten_ticks=2,
    adverse_detection_enabled=True,
    adverse_window=8,
    adverse_imbalance_threshold=0.70,
    adverse_spread_multiplier=1.5,
    adverse_cooldown_fills=4,
)

SOJ_MAY26_CONFIG = dataclasses.replace(
    SOJ_CONFIG,
    name="SOJ Rosario Mayo 2026",
)

SOJ_MIN_CONFIG = dataclasses.replace(
    SOJ_MAY26_CONFIG,
    name="SOJ Mini Mayo 2026",
    ticker_pattern="SOJ.MIN",
    contract_size=10.0,
    max_order_size=1,
    max_inventory=5,
    min_spread_bps=10.0,
    gamma=0.06,
    adverse_imbalance_threshold=0.75,
    adverse_spread_multiplier=1.8,
    adverse_cooldown_fills=3,
)

SOJ_MIN_MAY26_CONFIG = dataclasses.replace(
    SOJ_MIN_CONFIG,
    name="SOJ Mini Mayo 2026",
)


@dataclass
class MMConfig:
    """General MM configuration (shared across instruments)."""

    # Trading control
    ENABLE_TRADING: bool = True
    AGGRESSIVE_MODE: bool = True
    IMPROVE_BEST: bool = True

    # Timing
    QUOTE_REFRESH_SECONDS: float = 0.25
    RUN_SECONDS: int = 0  # 0 = indefinido

    # Volatility
    VOL_WINDOW: int = 20
    VOL_SPREAD_MULTIPLIER: float = 1.5

    # Risk (ajustado a fund de ARS 14M)
    FUND_SIZE_ARS: float = 14_000_000.0
    MAX_DAILY_LOSS_ARS: float = 140_000.0   # 1% del fund
    MAX_TOTAL_LOSS_PCT: float = 0.50        # 50% = desvinculacion del NOR

    # Instrument configs
    instruments: Dict[str, InstrumentConfig] = field(default_factory=lambda: {
        "DLR": DLR_CONFIG,
        "CAUC": CAUC_CONFIG,
        "SOJ": SOJ_MAY26_CONFIG,
        "SOJ_MIN": SOJ_MIN_MAY26_CONFIG,
    })

    # ---- Legacy accessors (for backward compat with sim_mm etc.) ----
    TICKER: str = "DLR/ABR26"
    BASE_SPREAD_BPS: float = 7.0
    MIN_SPREAD_BPS: float = 5.0
    MIN_EDGE_BPS: float = 1.0
    ORDER_SIZE: int = 1
    MAX_POSITION: int = 10
    INVENTORY_SKEW_FACTOR: float = 5.0
    EWMA_ALPHA: float = 0.7
    MAX_DAILY_LOSS: float = 100_000.0
    TICK_SIZE: float = 0.50
