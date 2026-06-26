"""Gestión de órdenes: envío, cancelación y consulta."""

from typing import Any
import pyRofex
from core import credentials as config
from core.utils import setup_logger

logger = setup_logger("order_manager")


def send_limit_order(
    ticker: str, side: str, price: float, size: int
) -> dict[str, Any] | None:
    """Envía una orden límite. side: 'BUY' o 'SELL'."""
    order_side = pyRofex.Side.BUY if side.upper() == "BUY" else pyRofex.Side.SELL
    try:
        response = pyRofex.send_order(
            ticker=ticker,
            side=order_side,
            size=size,
            price=price,
            order_type=pyRofex.OrderType.LIMIT,
            account=config.ACCOUNT,
        )
        proprietary = response.get("order", {}).get("proprietary") or response.get("proprietary")
        if proprietary is not None:
            response["proprietary"] = proprietary
        logger.info(
            "Orden LIMIT %s enviada: %s x%d @ %s - proprietary: %s - Response: %s",
            side, ticker, size, price, proprietary, response,
        )
        return response
    except Exception as e:
        logger.error("Error enviando orden límite: %s", e)
        return None


def send_market_order(
    ticker: str, side: str, size: int
) -> dict[str, Any] | None:
    """Envía una orden a mercado. side: 'BUY' o 'SELL'."""
    order_side = pyRofex.Side.BUY if side.upper() == "BUY" else pyRofex.Side.SELL
    try:
        response = pyRofex.send_order(
            ticker=ticker,
            side=order_side,
            size=size,
            order_type=pyRofex.OrderType.MARKET,
            account=config.ACCOUNT,
        )
        proprietary = response.get("order", {}).get("proprietary") or response.get("proprietary")
        if proprietary is not None:
            response["proprietary"] = proprietary
        logger.info(
            "Orden MARKET %s enviada: %s x%d - proprietary: %s - Response: %s",
            side, ticker, size, proprietary, response,
        )
        return response
    except Exception as e:
        logger.error("Error enviando orden a mercado: %s", e)
        return None


def cancel_order(order_id: str, proprietary: str | None = None) -> dict[str, Any] | None:
    """Cancels an order by clientId. Checks fill status first; returns ALREADY_FILLED if done."""
    try:
        status_resp = get_order_status(order_id, proprietary=proprietary)
        if status_resp:
            order = status_resp.get("order", {})
            if str(order.get("status", "")).upper() == "FILLED":
                fill_px = float(order.get("avgPx") or order.get("price") or 0)
                fill_qty = int(order.get("cumQty") or 0)
                logger.info(
                    "Order %s already FILLED at %.2f x %d - skipping cancel",
                    order_id, fill_px, fill_qty,
                )
                return {"status": "ALREADY_FILLED", "order": order}
    except Exception as e:
        logger.error("Pre-cancel status check failed for %s: %s", order_id, e)

    try:
        if proprietary is None:
            response = pyRofex.cancel_order(order_id)
        elif hasattr(pyRofex, "cancel_order_via_client_id"):
            response = pyRofex.cancel_order_via_client_id(order_id, proprietary)
        else:
            try:
                response = pyRofex.cancel_order(order_id, proprietary=proprietary)
            except TypeError:
                response = pyRofex.cancel_order(order_id, proprietary)
        logger.info(
            "Orden cancelada %s con proprietary=%s - Response: %s",
            order_id,
            proprietary,
            response,
        )
        return response
    except Exception as e:
        logger.error("Error cancelando orden %s (proprietary=%s): %s", order_id, proprietary, e)
        return None


def get_order_status(order_id: str, proprietary: str | None = None) -> dict[str, Any] | None:
    """Consulta estado por clientId. Si proprietary es None, usa default del environment."""
    try:
        if proprietary is None:
            response = pyRofex.get_order_status(order_id)
        else:
            try:
                response = pyRofex.get_order_status(order_id, proprietary=proprietary)
            except TypeError:
                response = pyRofex.get_order_status(order_id, proprietary)
        logger.info("Estado orden %s con proprietary=%s: %s", order_id, proprietary, response)
        return response
    except Exception as e:
        logger.error("Error consultando estado de orden %s (proprietary=%s): %s", order_id, proprietary, e)
        return None


def get_all_orders() -> list[dict[str, Any]]:
    """Lista todas las órdenes activas de la cuenta."""
    try:
        response = pyRofex.get_all_orders_status(account=config.ACCOUNT)
        orders: list[dict[str, Any]] = response.get("orders", [])
        logger.info("Órdenes activas obtenidas: %d", len(orders))
        return orders
    except Exception as e:
        logger.error("Error listando órdenes: %s", e)
        return []
