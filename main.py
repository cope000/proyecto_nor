"""Script principal: conecta, consulta instrumentos, market data y gestiona ordenes."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time
from core.connect import connect, test_connection
from core.instruments import get_futures_dollar, print_instruments
from core.market_data import get_snapshot, subscribe_realtime
from core.order_manager import send_limit_order, cancel_order, get_order_status
from core.utils import setup_logger

logger = setup_logger("main")


def main() -> None:
    # 1. Conectar a reMarkets
    logger.info("=== PASO 1: Conectando a reMarkets ===")
    if not connect():
        logger.error("No se pudo conectar. Abortando.")
        return
    segments = test_connection()
    if segments is None:
        logger.error("Conexión no verificada. Abortando.")
        return

    # 2. Listar futuros de dólar
    logger.info("=== PASO 2: Listando futuros de dólar ===")
    futures = get_futures_dollar()
    if not futures:
        logger.warning("No se encontraron futuros de dólar.")
        return
    print_instruments(futures)

    # Tomar el primer futuro de dólar
    first_ticker: str = futures[0].get("instrumentId", {}).get("symbol", "")
    if not first_ticker:
        logger.error("No se pudo obtener el ticker del primer futuro.")
        return
    logger.info("Ticker seleccionado: %s", first_ticker)

    # 3. Snapshot REST antes de suscribirse
    logger.info("=== PASO 3: Snapshot REST de %s ===", first_ticker)
    snapshot = get_snapshot(first_ticker)

    # 4. Suscribirse por WebSocket y recibir market data 30 segundos
    logger.info("=== PASO 4: Market data en tiempo real (30s) ===")
    subscribe_realtime([first_ticker], duration_seconds=30)

    # 5. Obtener last price actualizado para calcular precio de orden
    logger.info("=== PASO 5: Enviando orden límite de compra ===")
    snapshot = get_snapshot(first_ticker)
    last_price: float | None = snapshot.get("last") if snapshot else None
    if last_price is None or last_price == 0:
        logger.warning("No hay last price disponible. Usando bid o valor por defecto.")
        last_price = (snapshot or {}).get("bid_price") or 1000.0

    order_price = round(last_price * 0.99, 2)  # 1% debajo del last
    logger.info("Last: %.2f -> Precio orden: %.2f", last_price, order_price)

    order_response = send_limit_order(
        ticker=first_ticker,
        side="BUY",
        price=order_price,
        size=1,
    )

    order_id: str | None = None
    order_proprietary: str | None = None
    if order_response:
        order_id = order_response.get("order", {}).get("clientId")
        order_proprietary = (
            order_response.get("proprietary")
            or order_response.get("order", {}).get("proprietary")
        )
        logger.info("Response completo de send_limit_order: %s", order_response)
        logger.info("Orden enviada con clientId: %s", order_id)
        logger.info("Orden enviada con proprietary: %s", order_proprietary)

    # 6. Esperar 5 segundos
    logger.info("=== PASO 6: Esperando 5 segundos ===")
    time.sleep(5)

    # Consultar estado
    if order_id:
        get_order_status(order_id, proprietary=order_proprietary)

    # 7. Cancelar la orden
    logger.info("=== PASO 7: Cancelando orden ===")
    if order_id:
        cancel_response = cancel_order(order_id, proprietary=order_proprietary)
        logger.info("Response cancel_order: %s", cancel_response)
        time.sleep(1)
        get_order_status(order_id, proprietary=order_proprietary)
    else:
        logger.warning("No hay orden para cancelar.")

    # 8. Resumen
    logger.info("=== PASO 8: Resumen ===")
    print("\n" + "=" * 60)
    print("RESUMEN DE EJECUCIÓN")
    print("=" * 60)
    print(f"  Segmentos conectados : {segments}")
    print(f"  Futuros DLR listados : {len(futures)}")
    print(f"  Ticker operado       : {first_ticker}")
    print(f"  Last price           : {last_price}")
    print(f"  Precio orden compra  : {order_price} (1% debajo)")
    print(f"  Order ID             : {order_id}")
    print(f"  Proprietary          : {order_proprietary}")
    print(f"  Orden cancelada      : {'Si' if order_id else 'No'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
