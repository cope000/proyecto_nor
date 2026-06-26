"""Live calendar spread scanner against reMarkets.

Connects to reMarkets, fetches current DLR futures prices, builds all valid
(near, far) calendar spread pairs, accumulates spread histories and generates
signals once sufficient data is available.

Usage:
    python run_cs.py
"""

from __future__ import annotations

import sys
import time
import datetime

from connect import connect
from instruments import get_futures_dollar
from market_data import get_snapshot
from utils import setup_logger, print_table
from cs_config import CalendarSpreadConfig
from strategies import CalendarSpreadEngine

logger = setup_logger("run_cs")


def _expiry_days(expiry_yyyymm: tuple[int, int, str]) -> int:
    """Returns calendar days from today to the expiry month-end estimate."""
    year, month, _ = expiry_yyyymm
    today = datetime.date.today()
    # Use the 15th as a proxy mid-month expiry; pyRofex gives actual dates via MD.
    target = datetime.date(year, month, 15)
    return max((target - today).days, 1)


def _price_from_snapshot(snap: dict) -> float:
    """Returns best available price from a snapshot dict."""
    if not snap:
        return 0.0
    last = snap.get("last") or 0.0
    bid = snap.get("bid_price") or 0.0
    ask = snap.get("ask_price") or 0.0
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
    return mid or last


def _build_futures_with_prices(config: CalendarSpreadConfig) -> list[dict]:
    """Fetches DLR futures, requests snapshots, and returns enriched list sorted by expiry."""
    raw = get_futures_dollar()
    enriched: list[dict] = []
    for inst in raw:
        ticker = inst.get("ticker") or inst.get("instrumentId", {}).get("symbol", "")
        if not ticker:
            continue
        snap = get_snapshot(ticker)
        price = _price_from_snapshot(snap)
        # Extract expiry tuple already parsed by instruments module
        expiry_key = inst.get("expiry_key")  # (year, month, str)
        if expiry_key is None:
            continue
        days = _expiry_days(expiry_key)
        enriched.append({
            "ticker": ticker,
            "price": price,
            "expiry_days": days,
            "expiry_key": expiry_key,
        })
    return enriched


def _print_spread_table(signals: list[dict]) -> None:
    """Renders a formatted spread signal table."""
    if not signals:
        logger.info("No spread pairs found.")
        return
    headers = ["Par", "Near Px", "Far Px", "Spread", "Fwd TNA%", "Z-Score", "N", "Señal"]
    rows = []
    for s in signals:
        rows.append([
            s["pair_id"],
            f"{s['near_price']:.2f}",
            f"{s['far_price']:.2f}",
            f"{s['spread']:.2f}",
            f"{s['fwd_rate_tna']:.1f}",
            f"{s['z_score']:+.2f}" if s["n_history"] >= 2 else "---",
            str(s["n_history"]),
            s["action"],
        ])
    print_table(headers, rows)


def main() -> None:
    cfg = CalendarSpreadConfig()
    engine = CalendarSpreadEngine(
        z_entry=cfg.Z_SCORE_ENTRY,
        z_exit=cfg.Z_SCORE_EXIT,
        lookback=cfg.LOOKBACK_WINDOW,
        max_contracts=cfg.MAX_CONTRACTS,
        max_open=cfg.MAX_OPEN_SPREADS,
        multiplier=cfg.CONTRACT_MULTIPLIER,
    )

    logger.info("Conectando a reMarkets...")
    connect()
    logger.info("Conexion establecida.")

    iteration = 0
    while True:
        iteration += 1
        now = datetime.datetime.now().strftime("%H:%M:%S")
        logger.info("=== Iteracion %d | %s ===", iteration, now)

        futures = _build_futures_with_prices(cfg)
        if len(futures) < 2:
            logger.warning("Datos insuficientes: solo %d futuros con precio.", len(futures))
            time.sleep(cfg.SCAN_INTERVAL_SECONDS)
            continue

        pairs = engine.identify_spread_pairs(futures)
        if not pairs:
            logger.warning("No se encontraron pares de spread validos.")
            time.sleep(cfg.SCAN_INTERVAL_SECONDS)
            continue

        signals = engine.generate_signals(pairs)
        _print_spread_table(signals)

        # Report actionable signals
        actionable = [s for s in signals if s["action"] in ("SELL_SPREAD", "BUY_SPREAD", "CLOSE")]
        if actionable:
            for sig in actionable:
                logger.info(
                    "SIGNAL: %s -> %s | Spread=%.2f | Z=%.2f | Fwd=%.1f%% TNA",
                    sig["pair_id"], sig["action"], sig["spread"], sig["z_score"],
                    sig["fwd_rate_tna"],
                )
                if cfg.ENABLE_TRADING:
                    logger.info("(ENABLE_TRADING=True -- implementar envio de ordenes aqui)")

        logger.info(
            "Posiciones abiertas: %d | PNL realizado: %+.2f ARS",
            len(engine.open_positions), engine.realized_pnl,
        )

        time.sleep(cfg.SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Scanner detenido por el usuario.")
        sys.exit(0)
