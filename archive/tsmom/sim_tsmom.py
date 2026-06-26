"""Offline TSMOM backtest for DLR and RFX20 synthetic futures."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

from strategies import RegimeFilter, TSMOMSignal, VolatilitySizer
from tsmom_config import TSMOMConfig
from utils import setup_logger

logger = setup_logger("sim_tsmom")


@dataclass(slots=True)
class InstrumentState:
    """Mutable backtest state per instrument."""

    name: str
    prices: list[float]
    positions: list[int]
    pnl_daily: list[float]
    signal_counts: dict[str, int]
    time_in_market_days: int = 0
    trade_count: int = 0
    current_contracts: int = 0
    regime_filter: RegimeFilter | None = None
    leverage_history: list[float] | None = None
    notional_history: list[float] | None = None
    sizing_debug_days: int = 0


def _parse_args() -> argparse.Namespace:
    """Parses CLI args for offline backtest."""
    parser = argparse.ArgumentParser(description="Synthetic TSMOM backtest")
    parser.add_argument("--days", type=int, default=252, help="Number of trading days")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    return parser.parse_args()


def _generate_series(days: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Generates correlated daily returns and price paths for DLR and RFX20."""
    rng = np.random.default_rng(seed)
    cov = np.array([[1.0, -0.3], [-0.3, 1.0]], dtype=float)
    z = rng.multivariate_normal(mean=np.zeros(2), cov=cov, size=days)

    dlr_returns = np.zeros(days, dtype=float)
    rfx_returns = np.zeros(days, dtype=float)

    for i in range(days):
        dlr_ret = 0.0012 + 0.008 * z[i, 0]
        if i > 0 and i % 60 == 0:
            dlr_ret += rng.uniform(0.05, 0.15)
        dlr_returns[i] = dlr_ret

        cycle = i % 105
        cycle_drift = 0.0015 if cycle < 80 else -0.0025
        rfx_ret = cycle_drift + 0.018 * z[i, 1]
        rfx_returns[i] = rfx_ret

    dlr_prices = np.zeros(days, dtype=float)
    rfx_prices = np.zeros(days, dtype=float)
    dlr_prices[0] = 1200.0
    rfx_prices[0] = 5000.0
    for i in range(1, days):
        dlr_prices[i] = max(100.0, dlr_prices[i - 1] * (1.0 + dlr_returns[i]))
        rfx_prices[i] = max(100.0, rfx_prices[i - 1] * (1.0 + rfx_returns[i]))

    return dlr_prices, rfx_prices, dlr_returns, rfx_returns


def run_backtest(days: int, seed: int) -> None:
    """Runs daily TSMOM backtest on synthetic DLR and RFX20 futures."""
    cfg = TSMOMConfig()
    signal_engine = TSMOMSignal()
    sizer = VolatilitySizer()

    dlr_prices, rfx_prices, dlr_returns, rfx_returns = _generate_series(days, seed)

    states = {
        "DLR": InstrumentState(
            name="DLR",
            prices=[float(dlr_prices[0])],
            positions=[],
            pnl_daily=[],
            signal_counts={"LONG": 0, "SHORT": 0, "FLAT": 0},
            regime_filter=RegimeFilter(cfg.REGIME_FILTER_ENABLED, cfg.REGIME_MA_FAST, cfg.REGIME_MA_SLOW),
            leverage_history=[],
            notional_history=[],
        ),
        "RFX20": InstrumentState(
            name="RFX20",
            prices=[float(rfx_prices[0])],
            positions=[],
            pnl_daily=[],
            signal_counts={"LONG": 0, "SHORT": 0, "FLAT": 0},
            regime_filter=RegimeFilter(cfg.REGIME_FILTER_ENABLED, cfg.REGIME_MA_FAST, cfg.REGIME_MA_SLOW),
            leverage_history=[],
            notional_history=[],
        ),
    }

    total_pnl = 0.0
    equity_curve: list[float] = []
    portfolio_daily_returns: list[float] = []

    for day in range(days):
        if day > 0:
            states["DLR"].prices.append(float(dlr_prices[day]))
            states["RFX20"].prices.append(float(rfx_prices[day]))

        pnl_dlr = states["DLR"].current_contracts * (states["DLR"].prices[-1] - states["DLR"].prices[-2]) * cfg.INSTRUMENTS_CONFIG["DLR"].contract_multiplier if len(states["DLR"].prices) > 1 else 0.0
        pnl_rfx = states["RFX20"].current_contracts * (states["RFX20"].prices[-1] - states["RFX20"].prices[-2]) * cfg.INSTRUMENTS_CONFIG["RFX20"].contract_multiplier if len(states["RFX20"].prices) > 1 else 0.0
        pnl_day = pnl_dlr + pnl_rfx
        total_pnl += pnl_day
        portfolio_daily_returns.append(pnl_day / cfg.CAPITAL_ARS)

        for name in cfg.INSTRUMENTS:
            inst_cfg = cfg.INSTRUMENTS_CONFIG[name]
            state = states[name]
            price = state.prices[-1]
            regime = state.regime_filter.update(price) if state.regime_filter else "NEUTRAL"
            signal = signal_engine.generate_signal(state.prices, cfg.LONG_WINDOW, cfg.SHORT_WINDOW)
            strength = signal_engine.get_signal_strength(state.prices, cfg.LONG_WINDOW, cfg.SHORT_WINDOW)
            filtered_signal = state.regime_filter.apply_filter(signal, regime) if state.regime_filter else signal
            realized_vol = sizer.calculate_realized_vol(state.prices, cfg.VOL_LOOKBACK)
            diag = sizer.diagnose_sizing(
                instrument_name=name,
                signal=filtered_signal,
                signal_strength=strength,
                capital=cfg.CAPITAL_ARS,
                inst_config=inst_cfg,
                realized_vol=realized_vol,
                contract_price=price,
                strength_min=cfg.STRENGTH_SCALE_MIN,
                strength_max=cfg.STRENGTH_SCALE_MAX,
            )
            target_contracts = sizer.calculate_position_size(
                inst_config=inst_cfg,
                signal=filtered_signal,
                signal_strength=strength,
                capital=cfg.CAPITAL_ARS,
                realized_vol=realized_vol,
                contract_price=price,
                strength_min=cfg.STRENGTH_SCALE_MIN,
                strength_max=cfg.STRENGTH_SCALE_MAX,
            )

            if filtered_signal > 0:
                state.signal_counts["LONG"] += 1
            elif filtered_signal < 0:
                state.signal_counts["SHORT"] += 1
            else:
                state.signal_counts["FLAT"] += 1

            if target_contracts != state.current_contracts:
                state.trade_count += 1
            state.current_contracts = target_contracts
            if target_contracts != 0:
                state.time_in_market_days += 1
            state.positions.append(target_contracts)
            current_notional = sizer.calculate_notional(target_contracts, price, inst_cfg.contract_multiplier)
            current_leverage = sizer.calculate_notional_leverage(
                target_contracts,
                price,
                inst_cfg.contract_multiplier,
                cfg.CAPITAL_ARS,
                inst_cfg.allocation,
            )
            state.notional_history.append(current_notional)
            state.leverage_history.append(current_leverage)

            if abs(filtered_signal) > 0 and state.sizing_debug_days < 5:
                state.sizing_debug_days += 1
                logger.info(
                    "SIZING %s | cap_alloc=%.2fM | base_cts=%.2f | scaler=%.2f | cts_raw=%.2f | cts=%d | noc=%.2fM | lev=%.2fx | constraint=%s",
                    name,
                    diag["capital_allocated"] / 1_000_000.0,
                    diag["base_contracts"],
                    diag["strength_scaler"],
                    diag["contracts_raw"],
                    diag["contracts_clamped"],
                    diag["notional"] / 1_000_000.0,
                    diag["leverage"],
                    diag["binding_constraint"],
                )

        states["DLR"].pnl_daily.append(pnl_dlr)
        states["RFX20"].pnl_daily.append(pnl_rfx)

        equity = cfg.CAPITAL_ARS + total_pnl
        equity_curve.append(equity)
        peak = max(equity_curve)
        dd = peak - equity

        dlr_pos = states["DLR"].current_contracts
        rfx_pos = states["RFX20"].current_contracts
        dlr_cfg = cfg.INSTRUMENTS_CONFIG["DLR"]
        rfx_cfg = cfg.INSTRUMENTS_CONFIG["RFX20"]
        dlr_sig = signal_engine.generate_signal(states["DLR"].prices, cfg.LONG_WINDOW, cfg.SHORT_WINDOW)
        rfx_sig = signal_engine.generate_signal(states["RFX20"].prices, cfg.LONG_WINDOW, cfg.SHORT_WINDOW)
        dlr_str = signal_engine.get_signal_strength(states["DLR"].prices, cfg.LONG_WINDOW, cfg.SHORT_WINDOW)
        rfx_str = signal_engine.get_signal_strength(states["RFX20"].prices, cfg.LONG_WINDOW, cfg.SHORT_WINDOW)
        dlr_noc = sizer.calculate_notional(dlr_pos, states["DLR"].prices[-1], dlr_cfg.contract_multiplier)
        rfx_noc = sizer.calculate_notional(rfx_pos, states["RFX20"].prices[-1], rfx_cfg.contract_multiplier)
        dlr_lev = sizer.calculate_notional_leverage(dlr_pos, states["DLR"].prices[-1], dlr_cfg.contract_multiplier, cfg.CAPITAL_ARS, dlr_cfg.allocation)
        rfx_lev = sizer.calculate_notional_leverage(rfx_pos, states["RFX20"].prices[-1], rfx_cfg.contract_multiplier, cfg.CAPITAL_ARS, rfx_cfg.allocation)
        logger.info(
            "DAY %03d | DLR: %.2f SIG=%+0.0f STR=%.2f POS=%+d NOC=%.2fM LEV=%.2fx | RFX: %.2f SIG=%+0.0f STR=%.2f POS=%+d NOC=%.2fM LEV=%.2fx | PNL_D=%+.2f | PNL_T=%+.2f | DD=%.2f",
            day + 1,
            states["DLR"].prices[-1],
            dlr_sig,
            dlr_str,
            dlr_pos,
            dlr_noc / 1_000_000.0,
            dlr_lev,
            states["RFX20"].prices[-1],
            rfx_sig,
            rfx_str,
            rfx_pos,
            rfx_noc / 1_000_000.0,
            rfx_lev,
            pnl_day,
            total_pnl,
            dd,
        )

    pnl_by_instrument = {
        "DLR": float(np.sum(states["DLR"].pnl_daily)),
        "RFX20": float(np.sum(states["RFX20"].pnl_daily)),
    }
    ending_equity = cfg.CAPITAL_ARS + total_pnl
    cagr = (ending_equity / cfg.CAPITAL_ARS) ** (252.0 / max(days, 1)) - 1.0 if ending_equity > 0 else -1.0
    daily_ret = np.asarray(portfolio_daily_returns, dtype=float)
    sharpe = float(np.mean(daily_ret) / np.std(daily_ret, ddof=1) * np.sqrt(252.0)) if daily_ret.size > 1 and np.std(daily_ret, ddof=1) > 0 else 0.0
    equity_arr = np.asarray(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(equity_arr)
    dd_arr = running_max - equity_arr
    max_dd_ars = float(np.max(dd_arr)) if dd_arr.size else 0.0
    max_dd_pct = max_dd_ars / cfg.CAPITAL_ARS if cfg.CAPITAL_ARS > 0 else 0.0
    calmar = cagr / max_dd_pct if max_dd_pct > 0 else 0.0
    corr = float(np.corrcoef(dlr_returns[1:], rfx_returns[1:])[0, 1]) if days > 2 else 0.0
    total_trades = states["DLR"].trade_count + states["RFX20"].trade_count
    avg_lev_dlr = float(np.mean(states["DLR"].leverage_history)) if states["DLR"].leverage_history else 0.0
    avg_lev_rfx = float(np.mean(states["RFX20"].leverage_history)) if states["RFX20"].leverage_history else 0.0
    avg_noc_dlr = float(np.mean(states["DLR"].notional_history)) if states["DLR"].notional_history else 0.0
    avg_noc_rfx = float(np.mean(states["RFX20"].notional_history)) if states["RFX20"].notional_history else 0.0
    max_noc_dlr = float(np.max(states["DLR"].notional_history)) if states["DLR"].notional_history else 0.0
    max_noc_rfx = float(np.max(states["RFX20"].notional_history)) if states["RFX20"].notional_history else 0.0
    noc_ratio = (avg_noc_dlr / avg_noc_rfx) if avg_noc_rfx > 0 else 0.0

    logger.info("TSMOM summary start")
    logger.info("PnL total: %.2f", total_pnl)
    logger.info("CAGR: %.4f", cagr)
    logger.info("Sharpe ratio: %.4f", sharpe)
    logger.info("Max Drawdown: %.2f", max_dd_ars)
    logger.info("Calmar: %.4f", calmar)
    logger.info("PnL DLR: %.2f", pnl_by_instrument["DLR"])
    logger.info("PnL RFX20: %.2f", pnl_by_instrument["RFX20"])
    logger.info("Signals DLR: %s", states["DLR"].signal_counts)
    logger.info("Signals RFX20: %s", states["RFX20"].signal_counts)
    logger.info("Time in market DLR: %.2f%%", 100.0 * states["DLR"].time_in_market_days / days)
    logger.info("Time in market RFX20: %.2f%%", 100.0 * states["RFX20"].time_in_market_days / days)
    logger.info("Average leverage DLR: %.2fx", avg_lev_dlr)
    logger.info("Average leverage RFX20: %.2fx", avg_lev_rfx)
    logger.info("Average notional DLR: %.2fM", avg_noc_dlr / 1_000_000.0)
    logger.info("Average notional RFX20: %.2fK", avg_noc_rfx / 1_000.0)
    logger.info("Max notional DLR: %.2fM", max_noc_dlr / 1_000_000.0)
    logger.info("Max notional RFX20: %.2fK", max_noc_rfx / 1_000.0)
    logger.info("Ratio nocional promedio DLR/RFX20: %.2fx", noc_ratio)
    logger.info("Correlation DLR/RFX20 returns: %.4f", corr)
    logger.info("Drawdown max ARS: %.2f", max_dd_ars)
    logger.info("Trades totales: %d", total_trades)
    logger.info("TSMOM summary end")


if __name__ == "__main__":
    args = _parse_args()
    run_backtest(days=args.days, seed=args.seed)
