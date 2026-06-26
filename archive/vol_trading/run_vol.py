"""Live volatility trading monitor for DLR options against reMarkets."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from connect import connect
from instruments import get_all_instruments, get_futures_dollar
from market_data import get_snapshot
from utils import setup_logger
from vol_config import VolTradingConfig
from strategies.greeks import GreeksCalculator
from strategies.vol_surface import VolSurface
from strategies.vol_trading import VolTrader

logger = setup_logger("run_vol")

_MONTHS = {
    "ENE": 1,
    "FEB": 2,
    "MAR": 3,
    "ABR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DIC": 12,
}


def _best_price(snapshot: dict[str, Any] | None) -> float:
    if not snapshot:
        return 0.0
    bid = snapshot.get("bid_price") or 0.0
    ask = snapshot.get("ask_price") or 0.0
    last = snapshot.get("last") or 0.0
    if bid > 0 and ask > 0:
        return 0.5 * (bid + ask)
    return float(last or bid or ask or 0.0)


def _parse_option_symbol(symbol: str) -> dict[str, Any] | None:
    """Parses 'DLR/AGO25 1380 P' into fields for option analytics."""
    try:
        left, strike_txt, opt_type = symbol.split(" ")
        root, exp_code = left.split("/")
        if root != "DLR":
            return None
        month = _MONTHS.get(exp_code[:3].upper())
        year = 2000 + int(exp_code[3:5])
        if month is None:
            return None
        strike = float(strike_txt)
        return {
            "symbol": symbol,
            "expiry_label": exp_code,
            "expiry_month": month,
            "expiry_year": year,
            "strike": strike,
            "option_type": opt_type.upper(),
        }
    except Exception:
        return None


def _days_to_expiry(year: int, month: int) -> int:
    today = dt.date.today()
    # Mid-month proxy for options maturity when exact date is unavailable.
    expiry = dt.date(year, month, 15)
    return max((expiry - today).days, 1)


def _mock_prices_history(price: float, n: int, seed: int = 42) -> list[float]:
    """Fallback synthetic history for RV when historical endpoint is unavailable."""
    import numpy as np

    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0012, 0.008, n)
    path = [max(price * 0.95, 1.0)]
    for r in rets:
        path.append(path[-1] * math.exp(r))
    return path


def _collect_options_data(future_price: float, cfg: VolTradingConfig) -> list[dict[str, Any]]:
    """Collects listed DLR options and current prices from reMarkets."""
    instruments = get_all_instruments()
    options = []

    for inst in instruments:
        symbol = inst.get("instrumentId", {}).get("symbol", "")
        cfi = inst.get("cficode", "")
        if cfi != "OPEFXS" or not symbol.startswith("DLR/"):
            continue

        parsed = _parse_option_symbol(symbol)
        if not parsed:
            continue

        dte = _days_to_expiry(parsed["expiry_year"], parsed["expiry_month"])
        if dte < cfg.DAYS_TO_EXPIRY_MIN or dte > cfg.DAYS_TO_EXPIRY_MAX:
            continue

        snap = get_snapshot(symbol)
        bid = snap.get("bid_price") if snap else None
        ask = snap.get("ask_price") if snap else None
        last = snap.get("last") if snap else None
        if not ((bid and bid > 0) or (ask and ask > 0) or (last and last > 0)):
            continue

        options.append({
            "ticker": symbol,
            "expiry_label": parsed["expiry_label"],
            "strike": parsed["strike"],
            "option_type": parsed["option_type"],
            "days_to_expiry": dte,
            "bid": bid,
            "ask": ask,
            "last": last,
            "future_price": future_price,
        })

    return options


def _pick_underlying_future() -> tuple[str, float]:
    """Returns underlying DLR future ticker and best available price."""
    futures = get_futures_dollar()
    if not futures:
        return "", 0.0

    best_ticker = ""
    best_price = 0.0

    for inst in futures[:6]:
        symbol = inst.get("instrumentId", {}).get("symbol", "")
        snap = get_snapshot(symbol)
        px = _best_price(snap)
        if px > 0.0:
            best_ticker = symbol
            best_price = px
            break

    return best_ticker, best_price


def main() -> None:
    cfg = VolTradingConfig()
    greeks = GreeksCalculator()
    surface = VolSurface(greeks, cfg.RISK_FREE_RATE)
    trader = VolTrader(cfg, greeks, surface)

    logger.info("============================================================")
    logger.info("VOL TRADING DASHBOARD - %s", dt.date.today().isoformat())
    logger.info("============================================================")

    if not connect():
        logger.error("No se pudo conectar a reMarkets")
        return

    fut_ticker, fut_price = _pick_underlying_future()
    if fut_price <= 0.0:
        logger.error("No se pudo obtener precio del futuro subyacente")
        return

    options_data = _collect_options_data(fut_price, cfg)
    if not options_data:
        logger.warning("No hay opciones DLR con precios validos en el rango de vencimientos")
        logger.info("Futuro: %s @ %.2f", fut_ticker, fut_price)
        logger.info("Fin de semana o mercado sin liquidez: dashboard parcial")
        return

    prices_history = _mock_prices_history(fut_price, cfg.RV_LOOKBACK + 5)
    result = trader.on_new_data(
        future_price=fut_price,
        options_data=options_data,
        prices_history=prices_history,
        day=0,
    )

    term = surface.get_term_structure()
    nearest_exp = min(term.keys())
    skew = surface.get_skew(nearest_exp)

    logger.info("Futuro: %s @ %.2f | RV(%dd): %.1f%%", fut_ticker, fut_price, cfg.RV_LOOKBACK, result["rv"] * 100)
    logger.info("ATM IV: %.1f%% | VRP: %+.1f%%", result["iv"] * 100, result["vrp"] * 100)
    logger.info("Signal: %s", result["signal"])

    if trader.position:
        st = trader.position["structure"]
        if st["kind"] == "straddle":
            logger.info("")
            logger.info("Straddle ATM (Strike %.0f):", st["strike"])
        else:
            logger.info("")
            logger.info("Strangle (Put %.0f / Call %.0f):", st["put_strike"], st["call_strike"])
        logger.info("  Call: $%.2f | Put: $%.2f | Premium: $%.2f", st["call_price"], st["put_price"], st["premium_total"])

        g = result["position_greeks"] or {}
        logger.info(
            "  Delta: %.2f | Gamma: %.6f | Vega: %.2f | Theta: %.2f",
            g.get("delta", 0.0), g.get("gamma", 0.0), g.get("vega", 0.0), g.get("theta", 0.0),
        )

    logger.info("")
    logger.info("Hedge: %+d futures", result["hedge_needed"])
    if result["position_greeks"]:
        pg = result["position_greeks"]
        logger.info(
            "Risk: Vega=%.0f < %.0f | Gamma=%.0f < %.0f | %s",
            abs(pg["vega"]),
            cfg.MAX_VEGA_EXPOSURE,
            abs(pg["gamma"]),
            cfg.MAX_GAMMA_EXPOSURE,
            result["risk_status"],
        )

    logger.info("Skew (%sd): %+.2f%%", nearest_exp, skew * 100.0)
    logger.info("============================================================")

    surface.print_surface()

    if cfg.ENABLE_TRADING:
        logger.info("ENABLE_TRADING=True -> integrar envio de ordenes")


if __name__ == "__main__":
    main()
