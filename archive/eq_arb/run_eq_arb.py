"""Live equity futures arbitrage scanner for reMarkets."""

from __future__ import annotations

import datetime as dt

from connect import connect
from eq_config import EquityArbConfig
from instruments import get_all_instruments
from market_data import get_snapshot
from strategies.equity_arb import EquityArbEngine
from utils import setup_logger

logger = setup_logger("run_eq_arb")


def _best_price(snapshot: dict | None) -> float:
    if not snapshot:
        return 0.0
    bid = snapshot.get("bid_price") or 0.0
    ask = snapshot.get("ask_price") or 0.0
    last = snapshot.get("last") or 0.0
    if bid > 0 and ask > 0:
        return 0.5 * (bid + ask)
    return float(last or bid or ask or 0.0)


def _collect_market_data(cfg: EquityArbConfig) -> tuple[dict[str, float], dict[str, list[dict]]]:
    """Collects spot and futures quotes for configured equity symbols."""
    all_inst = get_all_instruments()
    spot_map: dict[str, float] = {}
    futures_map: dict[str, list[dict]] = {sym: [] for sym in cfg.INSTRUMENTS}

    for sym in cfg.INSTRUMENTS:
        spot_candidates = [
            f"{sym}/SPOT",
            sym,
        ]
        for cand in spot_candidates:
            snap = get_snapshot(cand)
            px = _best_price(snap)
            if px > 0.0:
                spot_map[sym] = px
                break

    for inst in all_inst:
        symbol = inst.get("instrumentId", {}).get("symbol", "")
        cfi = inst.get("cficode", "")
        if cfi != "FXXXSX":
            continue
        for sym in cfg.INSTRUMENTS:
            if not symbol.startswith(f"{sym}/"):
                continue
            snap = get_snapshot(symbol)
            px = _best_price(snap)
            if px <= 0.0:
                continue
            # Parse month code as proxy for days.
            days = 30
            if "JUN" in symbol:
                days = 90
            elif "MAY" in symbol:
                days = 60
            elif "ABR" in symbol:
                days = 30
            futures_map[sym].append({"ticker": symbol, "price": px, "days": days})

    for sym in futures_map:
        futures_map[sym].sort(key=lambda x: x["days"])

    return spot_map, futures_map


def main() -> None:
    cfg = EquityArbConfig()
    engine = EquityArbEngine(
        reference_rate_tna=cfg.REFERENCE_RATE_TNA,
        min_spread_bps=cfg.MIN_SPREAD_BPS,
        contract_multiplier=cfg.CONTRACT_MULTIPLIER,
    )

    logger.info("============================================================")
    logger.info("EQUITY FUTURES ARB - %s", dt.date.today().isoformat())
    logger.info("============================================================")

    if not connect():
        logger.error("No se pudo conectar a reMarkets")
        return

    spot_map, futures_map = _collect_market_data(cfg)
    rows = engine.scan_equity_futures(cfg.INSTRUMENTS, futures_map, spot_map)

    logger.info("Ticker    | Spot    | Futuro      | Days | TNA      | Signal")
    for sym in cfg.INSTRUMENTS:
        sym_rows = [r for r in rows if r["ticker"] == sym]
        if not sym_rows:
            logger.info("%-9s | %-7s | %-11s | %-4s | %-8s | %s", sym, "---", "---", "---", "---", "NO DATA")
            continue
        first = sym_rows[0]
        tna_txt = f"{first['implied_rate']:.1f}%" if first["implied_rate"] is not None else "---"
        logger.info(
            "%-9s | %7.2f | %-11s | %4d | %-8s | %s",
            sym,
            first["spot"],
            first["future_ticker"] or "---",
            int(first["days"]),
            tna_txt,
            first["signal"],
        )

    logger.info("============================================================")
    logger.info(
        "Tasa referencia: %.1f%% TNA | Umbral: %dbps",
        cfg.REFERENCE_RATE_TNA,
        cfg.MIN_SPREAD_BPS,
    )
    logger.info("============================================================")


if __name__ == "__main__":
    main()
