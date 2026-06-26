"""Obtención de market data por REST y WebSocket."""

from typing import Any
import time
import pyRofex
from core.utils import setup_logger, now_str, fmt_price

logger = setup_logger("market_data")

# Entries que solicitamos
MD_ENTRIES = [
    pyRofex.MarketDataEntry.BIDS,
    pyRofex.MarketDataEntry.OFFERS,
    pyRofex.MarketDataEntry.LAST,
]


# ───────────────────────── REST ─────────────────────────

def get_snapshot(ticker: str) -> dict[str, Any] | None:
    """Obtiene snapshot de market data vía REST para un ticker."""
    try:
        md = pyRofex.get_market_data(
            ticker=ticker,
            entries=[
                pyRofex.MarketDataEntry.BIDS,
                pyRofex.MarketDataEntry.OFFERS,
                pyRofex.MarketDataEntry.LAST,
                pyRofex.MarketDataEntry.OPENING_PRICE,
                pyRofex.MarketDataEntry.TRADE_VOLUME,
                pyRofex.MarketDataEntry.OPEN_INTEREST,
            ],
        )
        market_data = md.get("marketData", {})

        bid = market_data.get("BI", [{}])
        ask = market_data.get("OF", [{}])
        last_px = market_data.get("LA", {})
        oi = market_data.get("OI", None)
        vol = market_data.get("TV", None)

        snapshot = {
            "ticker": ticker,
            "bid_price": bid[0].get("price") if bid else None,
            "bid_size": bid[0].get("size") if bid else None,
            "ask_price": ask[0].get("price") if ask else None,
            "ask_size": ask[0].get("size") if ask else None,
            "last": last_px.get("price") if isinstance(last_px, dict) else last_px,
            "volume": vol,
            "open_interest": oi,
        }
        logger.info(
            "Snapshot %s | Bid: %s | Ask: %s | Last: %s",
            ticker,
            fmt_price(snapshot["bid_price"]),
            fmt_price(snapshot["ask_price"]),
            fmt_price(snapshot["last"]),
        )
        return snapshot
    except Exception as e:
        logger.error("Error obteniendo snapshot de %s: %s", ticker, e)
        return None


def get_book_depth(ticker: str, levels: int = 5) -> dict[str, Any] | None:
    """Obtiene profundidad del libro (N niveles bid/ask) via REST para un ticker."""
    try:
        md = pyRofex.get_market_data(
            ticker=ticker,
            entries=[
                pyRofex.MarketDataEntry.BIDS,
                pyRofex.MarketDataEntry.OFFERS,
                pyRofex.MarketDataEntry.LAST,
                pyRofex.MarketDataEntry.TRADE_VOLUME,
            ],
        )
        market_data = md.get("marketData", {})
        bids_raw = market_data.get("BI", [])
        asks_raw = market_data.get("OF", [])
        last_px = market_data.get("LA", {})
        vol = market_data.get("TV", None)

        bids = [
            {"price": float(b["price"]), "size": int(b.get("size") or 0)}
            for b in bids_raw[:levels]
            if b.get("price") is not None
        ]
        asks = [
            {"price": float(a["price"]), "size": int(a.get("size") or 0)}
            for a in asks_raw[:levels]
            if a.get("price") is not None
        ]

        return {
            "ticker": ticker,
            "bids": bids,
            "asks": asks,
            "last": last_px.get("price") if isinstance(last_px, dict) else last_px,
            "volume": vol,
        }
    except Exception as e:
        logger.error("Error obteniendo book depth de %s: %s", ticker, e)
        return None


# ───────────────────────── WebSocket ─────────────────────────

def _on_market_data(message: dict[str, Any]) -> None:
    """Callback para cada actualización de market data por WS."""
    md = message.get("marketData", {})
    ticker = message.get("instrumentId", {}).get("symbol", "???")

    bid = md.get("BI", [{}])
    ask = md.get("OF", [{}])
    last_px = md.get("LA", {})

    bid_price = bid[0].get("price") if bid else None
    ask_price = ask[0].get("price") if ask else None
    last_val = last_px.get("price") if isinstance(last_px, dict) else last_px

    print(
        f"[{now_str()}] {ticker}  |  "
        f"Bid: {fmt_price(bid_price)}  |  "
        f"Ask: {fmt_price(ask_price)}  |  "
        f"Last: {fmt_price(last_val)}"
    )


def _on_error(message: dict[str, Any]) -> None:
    """Callback de error en WS."""
    logger.error("WS error: %s", message)


def _on_order_report(message: dict[str, Any]) -> None:
    """Callback de order report en WS."""
    logger.info("WS order report: %s", message)


def subscribe_realtime(tickers: list[str], duration_seconds: int = 30) -> None:
    """Suscribe a market data en tiempo real por WebSocket durante N segundos."""
    try:
        pyRofex.init_websocket_connection(
            market_data_handler=_on_market_data,
            error_handler=_on_error,
            order_report_handler=_on_order_report,
        )
        logger.info("WebSocket conectado")

        pyRofex.market_data_subscription(
            tickers=tickers,
            entries=MD_ENTRIES,
        )
        logger.info("Suscrito a market data de: %s", tickers)

        print(f"\n--- Recibiendo market data por {duration_seconds}s ---\n")
        time.sleep(duration_seconds)

        pyRofex.close_websocket_connection()
        logger.info("WebSocket cerrado")
    except Exception as e:
        logger.error("Error en suscripción WebSocket: %s", e)


if __name__ == "__main__":
    from connect import connect
    if connect():
        # Ejemplo: snapshot REST
        get_snapshot("DLR/JUN26")
        # Ejemplo: WebSocket 15 segundos
        subscribe_realtime(["DLR/JUN26"], duration_seconds=15)
