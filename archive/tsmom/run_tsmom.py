"""Run TSMOM dashboard against reMarkets with limited live data."""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

from connect import connect
from instruments import get_all_instruments, get_futures_dollar
from market_data import get_snapshot
from order_manager import send_market_order
from strategies import RegimeFilter, TSMOMSignal, VolatilitySizer
from tsmom_config import TSMOMConfig
from utils import setup_logger

logger = setup_logger("run_tsmom")


def _pick_liquid_ticker(candidates: list[str]) -> tuple[str | None, dict[str, Any] | None]:
    """Returns first liquid ticker using bid+ask or highest bid size from candidates."""
    best_ticker: str | None = None
    best_snap: dict[str, Any] | None = None
    best_score = (-1, -1.0)
    for ticker in candidates[:5]:
        snap = get_snapshot(ticker)
        if not snap:
            continue
        has_both = 1 if (snap.get("bid_price") and snap.get("ask_price")) else 0
        bid_size = float(snap.get("bid_size") or 0.0)
        score = (has_both, bid_size)
        if score > best_score:
            best_score = score
            best_ticker = ticker
            best_snap = snap
    return best_ticker, best_snap


def _find_dlr_ticker() -> tuple[str | None, dict[str, Any] | None]:
    """Finds nearest liquid DLR future."""
    tickers = [x.get("instrumentId", {}).get("symbol", "") for x in get_futures_dollar()]
    tickers = [t for t in tickers if t]
    return _pick_liquid_ticker(tickers)


def _find_rfx20_ticker() -> tuple[str | None, dict[str, Any] | None]:
    """Finds nearest liquid RFX20 future using instrument search heuristics."""
    all_inst = get_all_instruments()
    candidates: list[str] = []
    for inst in all_inst:
        symbol = inst.get("instrumentId", {}).get("symbol", "")
        cfi = inst.get("cficode", "")
        if cfi != "FXXXSX":
            continue
        upper = symbol.upper()
        if "RFX20" in upper or upper.startswith("RFX/") or upper.startswith("RFX20/"):
            if " " not in upper:
                candidates.append(symbol)
    candidates = sorted(set(candidates))
    return _pick_liquid_ticker(candidates)


def _format_signal(sig: float) -> str:
    """Formats signal into dashboard label."""
    if sig > 0:
        return "LONG"
    if sig < 0:
        return "SHORT"
    return "FLAT"


def run_dashboard(config: TSMOMConfig) -> None:
    """Runs one-shot TSMOM dashboard against reMarkets live snapshots."""
    if not connect():
        raise RuntimeError("No se pudo conectar a reMarkets")

    signal_engine = TSMOMSignal()
    sizer = VolatilitySizer()
    regimes = {
        "DLR": RegimeFilter(config.REGIME_FILTER_ENABLED, config.REGIME_MA_FAST, config.REGIME_MA_SLOW),
        "RFX20": RegimeFilter(config.REGIME_FILTER_ENABLED, config.REGIME_MA_FAST, config.REGIME_MA_SLOW),
    }

    selected: dict[str, tuple[str | None, dict[str, Any] | None]] = {
        "DLR": _find_dlr_ticker(),
        "RFX20": _find_rfx20_ticker(),
    }

    rows: list[dict[str, Any]] = []
    for instrument in config.INSTRUMENTS:
        inst_cfg = config.INSTRUMENTS_CONFIG[instrument]
        ticker, snap = selected.get(instrument, (None, None))
        price = None
        if snap:
            price = snap.get("last") or snap.get("bid_price") or snap.get("ask_price")
        history = [float(price)] if price else []

        if len(history) < config.LONG_WINDOW:
            logger.info(
                "Insufficient data for TSMOM signal (%s, need %d days, have %d)",
                instrument,
                config.LONG_WINDOW,
                len(history),
            )

        signal = signal_engine.generate_signal(history, config.LONG_WINDOW, config.SHORT_WINDOW)
        strength = signal_engine.get_signal_strength(history, config.LONG_WINDOW, config.SHORT_WINDOW)
        regime = regimes[instrument].update(float(price)) if price else "NEUTRAL"
        filtered_signal = regimes[instrument].apply_filter(signal, regime)

        realized_vol = sizer.calculate_realized_vol(history, config.VOL_LOOKBACK)
        contracts = sizer.calculate_position_size(
            inst_config=inst_cfg,
            signal=filtered_signal,
            signal_strength=strength,
            capital=config.CAPITAL_ARS,
            realized_vol=realized_vol,
            contract_price=float(price or 0.0),
        )
        leverage = (
            sizer.calculate_notional_leverage(
                contracts,
                float(price or 0.0),
                inst_cfg.contract_multiplier,
                config.CAPITAL_ARS,
                inst_cfg.allocation,
            )
            if price
            else 0.0
        )
        notional = sizer.calculate_notional(contracts, float(price or 0.0), inst_cfg.contract_multiplier) if price else 0.0

        rows.append(
            {
                "instrument": instrument,
                "ticker": ticker or "N/A",
                "price": price,
                "signal": _format_signal(filtered_signal),
                "strength": strength,
                "regime": regime,
                "contracts": contracts,
                "leverage": leverage,
                "notional": notional,
            }
        )

        if config.ENABLE_TRADING and ticker and contracts != 0:
            side = "BUY" if contracts > 0 else "SELL"
            send_market_order(ticker=ticker, side=side, size=abs(contracts))

    print("=" * 76)
    print(f"TSMOM DASHBOARD - {datetime.now().date().isoformat()}")
    print("=" * 76)
    print("Instrumento | Ticker        | Precio | Signal | Strength | Regime  | Contracts | Notional | Leverage")
    for row in rows:
        price_txt = f"{row['price']:.2f}" if row['price'] else "---"
        contracts_txt = f"{row['contracts']:+d}"
        notional_txt = f"{row['notional'] / 1_000_000:.2f}M"
        print(
            f"{row['instrument']:<11} | {row['ticker']:<13} | {price_txt:>6} | {row['signal']:<6} | {row['strength']:>8.2f} | "
            f"{row['regime']:<7} | {contracts_txt:>9} | {notional_txt:>8} | {row['leverage']:>7.2f}x"
        )
    print("=" * 76)
    print(
        f"Capital: ${config.CAPITAL_ARS:,.0f} | Regime Filter: {'ON' if config.REGIME_FILTER_ENABLED else 'OFF'} | "
        f"VolTarget DLR: {config.INSTRUMENTS_CONFIG['DLR'].vol_target * 100:.0f}% / RFX20: {config.INSTRUMENTS_CONFIG['RFX20'].vol_target * 100:.0f}%"
    )
    print("=" * 76)


def _parse_args() -> argparse.Namespace:
    """Parses CLI arguments."""
    parser = argparse.ArgumentParser(description="Run TSMOM dashboard on reMarkets")
    return parser.parse_args()


if __name__ == "__main__":
    _parse_args()
    run_dashboard(TSMOMConfig())
