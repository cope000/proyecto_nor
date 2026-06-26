"""Volatility-targeted position sizing utilities."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from tsmom_config import InstrumentConfig


class VolatilitySizer:
    """Computes realized volatility and target contract sizing."""

    def calculate_realized_vol(self, prices: list[float], lookback: int) -> float:
        """Returns annualized realized volatility from trailing daily returns."""
        if lookback <= 1 or len(prices) < lookback:
            return 0.0
        arr = np.asarray(prices[-lookback:], dtype=float)
        rets = np.diff(arr) / arr[:-1]
        if rets.size < 2:
            return 0.0
        return float(np.std(rets, ddof=1) * math.sqrt(252.0))

    def calculate_position_size(
        self,
        inst_config: "InstrumentConfig",
        signal: float,
        signal_strength: float,
        capital: float,
        realized_vol: float,
        contract_price: float,
        strength_min: float = 0.4,
        strength_max: float = 1.5,
    ) -> int:
        """Returns target contracts using per-instrument vol-target sizing with strength clamping.

        All instrument-specific params (allocation, vol_target, multiplier, leverage, max_contracts)
        come from inst_config. The position is split into two concerns:
          1. base_contracts: full-size position at the instrument's vol target and allocation.
          2. strength_scaler: clamps signal_strength to [strength_min, strength_max] so
             any active signal holds at least strength_min of the base position.
        """
        if signal == 0.0 or signal_strength <= 0.0 or capital <= 0 or inst_config.allocation <= 0:
            return 0
        if contract_price <= 0 or inst_config.contract_multiplier <= 0:
            return 0

        capital_allocated = capital * inst_config.allocation
        unit_notional = contract_price * inst_config.contract_multiplier
        base_contracts = (capital_allocated * inst_config.vol_target / max(realized_vol, 0.01)) / unit_notional
        strength_scaler = min(max(signal_strength, strength_min), strength_max)
        contracts = int(round(base_contracts * signal * strength_scaler))

        max_nocional = capital_allocated * inst_config.max_leverage
        max_contracts_dynamic = int(math.floor(max_nocional / unit_notional))
        max_contracts_effective = max(0, min(max_contracts_dynamic, inst_config.max_position_contracts))
        if abs(contracts) > max_contracts_effective:
            contracts = int(math.copysign(max_contracts_effective, contracts)) if max_contracts_effective > 0 else 0

        leverage = self.calculate_notional_leverage(
            contracts=contracts,
            contract_price=contract_price,
            contract_multiplier=inst_config.contract_multiplier,
            capital=capital,
            allocation=inst_config.allocation,
        )
        if leverage > inst_config.max_leverage and leverage > 0:
            scaled = inst_config.max_leverage / leverage
            contracts = int(round(contracts * scaled))

        if abs(contracts) > max_contracts_effective:
            contracts = int(math.copysign(max_contracts_effective, contracts)) if max_contracts_effective > 0 else 0
        return contracts

    def calculate_notional(self, contracts: int, contract_price: float, contract_multiplier: float) -> float:
        """Returns total position notional in ARS."""
        return abs(float(contracts)) * contract_price * contract_multiplier

    def calculate_notional_leverage(
        self,
        contracts: int,
        contract_price: float,
        contract_multiplier: float,
        capital: float,
        allocation: float,
    ) -> float:
        """Returns leverage based on notional over allocated capital."""
        denom = capital * allocation
        if denom <= 0:
            return 0.0
        return float(self.calculate_notional(contracts, contract_price, contract_multiplier) / denom)

    def get_leverage(
        self,
        contracts: int,
        contract_price: float,
        contract_multiplier: float,
        capital: float,
        allocation: float,
    ) -> float:
        """Returns absolute leverage implied by target contracts."""
        return self.calculate_notional_leverage(
            contracts=contracts,
            contract_price=contract_price,
            contract_multiplier=contract_multiplier,
            capital=capital,
            allocation=allocation,
        )

    def diagnose_sizing(
        self,
        instrument_name: str,
        signal: float,
        signal_strength: float,
        capital: float,
        inst_config: "InstrumentConfig",
        realized_vol: float,
        contract_price: float,
        strength_min: float = 0.4,
        strength_max: float = 1.5,
    ) -> dict[str, Any]:
        """Returns detailed sizing diagnostics using per-instrument vol_target from inst_config."""
        capital_allocated = capital * inst_config.allocation
        raw_exposure = capital_allocated * inst_config.vol_target / max(realized_vol, 0.01) if capital_allocated > 0 else 0.0
        unit_notional = contract_price * inst_config.contract_multiplier if contract_price > 0 else 0.0
        base_contracts = raw_exposure / unit_notional if unit_notional > 0 else 0.0
        strength_scaler = min(max(signal_strength, strength_min), strength_max) if signal_strength > 0.0 else 0.0
        adjusted_exposure = raw_exposure * signal * strength_scaler
        contracts_raw = base_contracts * signal * strength_scaler
        max_nocional = capital_allocated * inst_config.max_leverage
        max_contracts_by_notional = int(math.floor(max_nocional / unit_notional)) if unit_notional > 0 else 0
        max_contracts_by_config = inst_config.max_position_contracts
        contracts_clamped = self.calculate_position_size(
            inst_config=inst_config,
            signal=signal,
            signal_strength=signal_strength,
            capital=capital,
            realized_vol=realized_vol,
            contract_price=contract_price,
            strength_min=strength_min,
            strength_max=strength_max,
        )
        notional = self.calculate_notional(contracts_clamped, contract_price, inst_config.contract_multiplier)
        leverage = self.calculate_notional_leverage(
            contracts_clamped,
            contract_price,
            inst_config.contract_multiplier,
            capital,
            inst_config.allocation,
        )

        binding_constraint = "none"
        raw_abs = abs(int(round(contracts_raw)))
        effective_cap = min(max_contracts_by_notional, max_contracts_by_config)
        if max_contracts_by_notional < max_contracts_by_config and raw_abs > max_contracts_by_notional:
            binding_constraint = "notional_cap"
        elif raw_abs > max_contracts_by_config:
            binding_constraint = "config_cap"
        elif effective_cap > 0 and raw_abs > effective_cap:
            binding_constraint = "leverage_cap"

        return {
            "instrument_name": instrument_name,
            "capital_allocated": capital_allocated,
            "raw_exposure": raw_exposure,
            "base_contracts": base_contracts,
            "strength_scaler": strength_scaler,
            "adjusted_exposure": adjusted_exposure,
            "contracts_raw": contracts_raw,
            "contracts_clamped": contracts_clamped,
            "notional": notional,
            "leverage": leverage,
            "max_contracts_by_notional": max_contracts_by_notional,
            "max_contracts_by_config": max_contracts_by_config,
            "binding_constraint": binding_constraint,
        }
