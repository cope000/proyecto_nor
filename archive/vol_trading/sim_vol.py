"""Offline simulation of DLR options volatility trading strategy."""

from __future__ import annotations

import argparse
import math

import numpy as np

from utils import setup_logger
from vol_config import VolTradingConfig
from strategies.greeks import GreeksCalculator
from strategies.vol_surface import VolSurface
from strategies.vol_trading import VolTrader

logger = setup_logger("sim_vol")


def _synthetic_options_chain(
    F: float,
    iv: float,
    expiry_days: int,
    r: float,
    greeks: GreeksCalculator,
) -> list[dict]:
    """Creates synthetic bid/ask for ATM-centered option strikes."""
    center = round(F / 10.0) * 10.0
    strikes = [center + 10.0 * i for i in range(-5, 6)]
    chain: list[dict] = []
    T = max(expiry_days / 365.0, 1e-6)

    for K in strikes:
        call = greeks.call_price(F, K, T, r, iv)
        put = greeks.put_price(F, K, T, r, iv)
        for opt_type, px in (("C", call), ("P", put)):
            px = max(px, 0.05)
            spread = max(px * 0.08, 0.02)
            bid = max(px - 0.5 * spread, 0.01)
            ask = px + 0.5 * spread
            chain.append({
                "ticker": f"DLR/SYN {int(K)} {opt_type}",
                "strike": K,
                "option_type": opt_type,
                "bid": bid,
                "ask": ask,
                "last": px,
                "days_to_expiry": expiry_days,
                "expiry_label": f"SYN{expiry_days}d",
                "future_price": F,
            })
    return chain


def run_simulation(days: int = 60, seed: int = 42) -> None:
    cfg = VolTradingConfig()
    greeks = GreeksCalculator()
    surface = VolSurface(greeks, cfg.RISK_FREE_RATE)
    trader = VolTrader(cfg, greeks, surface)

    rng = np.random.default_rng(seed)

    F = 1400.0
    iv_mkt = 0.25
    expiry_days = 45

    prices_history: list[float] = [F]
    pnl_daily: list[float] = []
    pnl_total_path: list[float] = [0.0]

    sig_counts = {
        "SELL_VOL": 0,
        "BUY_VOL": 0,
        "NEUTRAL": 0,
        "CLOSE": 0,
        "NO_DATA": 0,
        "COOLDOWN": 0,
    }
    days_with_position: list[str] = []
    debug_position_days = 0

    logger.info("=== SIM Vol Trading | %d dias | seed=%d ===", days, seed)
    logger.info("DIA     | F        | IV     | RV     | VRP     | SIG       | POS | DELTA    | HEDGE | PNL_D      | PNL_T")

    for day in range(1, days + 1):
        # Future process with moderate realized vol and occasional stress shocks.
        daily_ret = rng.normal(0.0007, 0.008)
        if day % 15 == 0:
            daily_ret += rng.normal(-0.001, 0.008)
        F *= math.exp(daily_ret)

        # IV process correlated with downside moves (fear premium) + mean reversion.
        down_move = max(-daily_ret, 0.0)
        up_move = max(daily_ret, 0.0)
        iv_mkt += 0.30 * (0.23 - iv_mkt) + 0.8 * down_move - 0.9 * up_move + rng.normal(0.0, 0.0025)
        if day % 15 == 0:
            iv_mkt += abs(rng.normal(0.002, 0.003))
        iv_mkt = float(np.clip(iv_mkt, 0.17, 0.34))

        # Roll expiry to keep chain in configurable DTE range.
        expiry_days -= 1
        if expiry_days < cfg.DAYS_TO_EXPIRY_MIN:
            expiry_days = 45

        prices_history.append(F)
        chain = _synthetic_options_chain(F, iv_mkt, expiry_days, cfg.RISK_FREE_RATE, greeks)

        res = trader.on_new_data(
            future_price=F,
            options_data=chain,
            prices_history=prices_history,
            day=day,
            default_rv=0.20,
        )

        if res.get("rv_default_used"):
            logger.info(
                "Using default RV=20%% (insufficient history, day %d/%d)",
                day,
                cfg.RV_LOOKBACK,
            )

        sig = res["signal"]
        sig_counts[sig] = sig_counts.get(sig, 0) + 1

        daily_pnl = float(res["daily_pnl"])
        pnl_daily.append(daily_pnl)
        pnl_total_path.append(pnl_total_path[-1] + daily_pnl)

        pos_txt = "FLAT"
        delta_txt = 0.0
        if trader.position:
            st = trader.position["structure"]
            side = "-" if trader.position["side"] < 0 else "+"
            if st["kind"] == "straddle":
                pos_txt = f"{side}{trader.position['contracts']} straddle K={st['strike']:.0f}"
            else:
                pos_txt = (
                    f"{side}{trader.position['contracts']} strangle "
                    f"P{st['put_strike']:.0f}/C{st['call_strike']:.0f}"
                )
            if res["position_greeks"]:
                delta_txt = float(res["position_greeks"]["delta"])
            days_with_position.append(
                f"DAY {day:03d} | F={F:.2f} | IV={res['iv']*100:.1f}% | RV={res['rv']*100:.1f}% | "
                f"VRP={res['vrp']*100:+.1f}% | SIG={sig:<8} | POS={pos_txt}"
            )

            if debug_position_days < 5:
                if res.get("opened") and res.get("opened_structure"):
                    st = res["opened_structure"]
                    logger.info(
                        "STRADDLE OPENED: K=%.0f | Call=$%.2f Put=$%.2f | Premium=$%.2f | Vega=%.2f | Max_Vega=%.2f",
                        st.get("strike", 0.0),
                        st.get("call_price", 0.0),
                        st.get("put_price", 0.0),
                        st.get("premium_total", 0.0),
                        st.get("vega", 0.0) * cfg.MAX_CONTRACTS,
                        cfg.MAX_VEGA_EXPOSURE,
                    )
                if res.get("pnl_calc"):
                    pc = res["pnl_calc"]
                    logger.info(
                        "PNL CALC: prev_premium=$%.2f | curr_premium=$%.2f | theta_pnl=$%.2f | vega_pnl=$%.2f | hedge_pnl=$%.2f | daily=$%.2f",
                        pc.get("prev_premium", 0.0),
                        pc.get("curr_premium", 0.0),
                        pc.get("theta_pnl", 0.0),
                        pc.get("vega_pnl", 0.0),
                        pc.get("hedge_pnl", 0.0),
                        pc.get("daily", 0.0),
                    )
                debug_position_days += 1

        if res.get("close_reason"):
            logger.info("CLOSE REASON: %s", res["close_reason"])

        logger.info(
            "DAY %03d | %7.2f | %5.1f%% | %5.1f%% | %+.1f%% | %-9s | %-20s | %+8.3f | %+5d | %+10.2f | %+10.2f",
            day,
            F,
            res["iv"] * 100.0,
            res["rv"] * 100.0,
            res["vrp"] * 100.0,
            sig,
            pos_txt[:20],
            delta_txt,
            int(res["hedge_needed"]),
            daily_pnl,
            pnl_total_path[-1],
        )

    pnl_arr = np.asarray(pnl_total_path, dtype=float)
    peak = np.maximum.accumulate(pnl_arr)
    dd = pnl_arr - peak
    max_dd = float(np.min(dd))

    daily = np.asarray(pnl_daily, dtype=float)
    sharpe = 0.0
    if len(daily) > 2 and np.std(daily, ddof=1) > 1e-9:
        sharpe = float(np.mean(daily) / np.std(daily, ddof=1) * math.sqrt(252.0))

    opens = [t for t in trader.open_trades if t.get("action") == "OPEN"]
    closes = [t for t in trader.open_trades if t.get("action") == "CLOSE"]

    avg_vrp = 0.0
    if opens:
        avg_vrp = float(np.mean([t.get("vrp", 0.0) for t in opens]))

    logger.info("")
    logger.info("============================================================")
    logger.info("RESUMEN SIMULACION - Vol Trading DLR (%d dias)", days)
    logger.info("============================================================")
    logger.info("Dias con SELL_VOL          : %d", sig_counts.get("SELL_VOL", 0))
    logger.info("Dias con BUY_VOL           : %d", sig_counts.get("BUY_VOL", 0))
    logger.info(
        "Dias NEUTRAL/CLOSE/NO_DATA/COOLDOWN : %d",
        sig_counts.get("NEUTRAL", 0)
        + sig_counts.get("CLOSE", 0)
        + sig_counts.get("NO_DATA", 0)
        + sig_counts.get("COOLDOWN", 0),
    )
    logger.info("Trades (aperturas)         : %d", len(opens))
    logger.info("Trades (cierres)           : %d", len(closes))
    logger.info("Average VRP captured       : %+.2f%%", avg_vrp * 100.0)
    logger.info("PnL theta component ARS    : %s", f"{trader.theta_pnl:+,.2f}")
    logger.info("PnL vega component ARS     : %s", f"{trader.vega_pnl:+,.2f}")
    logger.info("PnL gamma component ARS    : %s", f"{trader.gamma_pnl:+,.2f}")
    logger.info("PnL hedge component ARS    : %s", f"{trader.hedge_pnl:+,.2f}")
    logger.info("PnL total ARS              : %s", f"{pnl_total_path[-1]:+,.2f}")
    logger.info("Max drawdown ARS           : %s", f"{max_dd:+,.2f}")
    logger.info("Sharpe ratio (daily PnL)   : %.2f", sharpe)
    logger.info("============================================================")

    if days_with_position:
        logger.info("\nPrimeros 5 dias con posicion:")
        for line in days_with_position[:5]:
            logger.info(line)

        logger.info("\nUltimos 10 dias de simulacion:")
        for day in range(max(1, days - 9), days + 1):
            idx = day - 1
            logger.info(
                "DAY %03d | PNL_D=%s | PNL_T=%s",
                day,
                f"{pnl_daily[idx]:+,.2f}",
                f"{pnl_total_path[idx + 1]:+,.2f}",
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulacion vol trading DLR")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_simulation(days=args.days, seed=args.seed)
