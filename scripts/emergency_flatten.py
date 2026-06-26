"""
Flatten de emergencia: cancela todas las órdenes abiertas de DLR/MAY26 y DLR/JUN26,
calcula posición neta desde fills del día, y envía órdenes agresivas para flatear.

Uso:
    cd c:\\Users\\54344\\Desktop\\A3\\proyecto_nor
    python scripts/emergency_flatten.py
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Asegurar que el project root esté en sys.path ───────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import pyRofex
from core.connect import connect
from core.market_data import get_snapshot
from core.order_manager import get_all_orders, send_limit_order

# ── Configuración ────────────────────────────────────────────────────────────
NEAR = "DLR/MAY26"
FAR  = "DLR/JUN26"
TICKERS = {NEAR, FAR}
TICK        = 0.5   # tick mínimo del instrumento
AGGR_TICKS  = 2     # ticks de agresión sobre bid/ask
FILLS_DIR   = _ROOT / "logs" / "fills"
SESSION_DATE = datetime.now().strftime("%Y%m%d")


def _csv_path(ticker: str) -> Path:
    safe = ticker.replace("/", "-")
    return FILLS_DIR / f"{safe}_{SESSION_DATE}.csv"


def _net_position_from_csv(ticker: str) -> int:
    """Calcula posición neta: sum(BUY qty) - sum(SELL qty) desde el CSV de fills del día."""
    path = _csv_path(ticker)
    if not path.exists():
        print(f"  [WARN] No se encontró fills CSV: {path}")
        return 0
    net = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            qty  = int(float(row.get("qty", 0)))
            side = str(row.get("side", "")).upper()
            if side == "BUY":
                net += qty
            elif side == "SELL":
                net -= qty
    return net


def _cancel_all_open(tickers: set[str]) -> int:
    """Cancela todas las órdenes NEW/PARTIALLY_FILLED de los tickers indicados.
    
    Llama pyRofex.cancel_order directamente para evitar el get_order_status()
    pre-check de cancel_order() — con 4000+ órdenes históricas la versión
    con pre-check tarda minutos.
    """
    orders = get_all_orders()
    # Filtrar primero en memoria; solo iterar candidatos reales
    candidates = [
        o for o in orders
        if str(o.get("instrumentId", {}).get("symbol") or "") in tickers
        and str(o.get("status") or "").upper() in {"NEW", "PARTIALLY_FILLED"}
    ]
    print(f"  Órdenes abiertas encontradas: {len(candidates)} (de {len(orders)} totales)")
    cancelled = 0
    for order in candidates:
        sym  = str(order.get("instrumentId", {}).get("symbol") or "")
        oid  = str(order.get("clOrdId") or order.get("clientId") or "")
        prop = order.get("proprietary")
        status = str(order.get("status") or "").upper()
        if not oid:
            continue
        try:
            # Bypass pre-check: ya conocemos el status desde get_all_orders()
            if prop is not None:
                try:
                    resp = pyRofex.cancel_order(oid, proprietary=prop)
                except TypeError:
                    resp = pyRofex.cancel_order(oid, prop)
            else:
                resp = pyRofex.cancel_order(oid)
            if resp is not None:
                cancelled += 1
                print(f"  Cancelada: {sym} oid={oid} status={status}")
        except Exception as exc:
            print(f"  [WARN] Error cancelando {sym} oid={oid}: {exc}")
    return cancelled


def _flatten_leg(ticker: str, net_pos: int) -> bool:
    """Envía orden agresiva para flatear una pata. Retorna True si se envió."""
    if net_pos == 0:
        print(f"  {ticker}: posición neta = 0 — no se requiere flatten")
        return False

    snap = get_snapshot(ticker)
    if snap is None:
        print(f"  [ERROR] Sin snapshot para {ticker} — no se puede flatear")
        return False

    bid = snap.get("bid_price")
    ask = snap.get("ask_price")

    if net_pos > 0:
        # LONG → SELL agresivo al bid - aggr_ticks
        if not bid:
            print(f"  [ERROR] Sin bid para {ticker}")
            return False
        side  = "SELL"
        price = round(float(bid) - AGGR_TICKS * TICK, 2)
    else:
        # SHORT → BUY agresivo al ask + aggr_ticks
        if not ask:
            print(f"  [ERROR] Sin ask para {ticker}")
            return False
        side  = "BUY"
        price = round(float(ask) + AGGR_TICKS * TICK, 2)

    qty   = abs(net_pos)
    price = max(price, TICK)

    print(f"  Enviando flatten: {ticker} | side={side} | price={price} | qty={qty}")
    resp = send_limit_order(ticker=ticker, side=side, price=price, size=qty)

    if resp is not None:
        print(f"  Flatten order sent | side={side} | price={price} | qty={qty}")
        return True
    else:
        print(f"  [ERROR] Fallo al enviar orden flatten para {ticker}")
        return False


def main() -> None:
    print("=" * 60)
    print("EMERGENCY FLATTEN — DLR Calendar Spread")
    print(f"Sesión: {SESSION_DATE}  |  {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 60)

    # 1. Conectar
    print("\n[1] Conectando a reMarkets...")
    if not connect():
        print("  [FATAL] No se pudo conectar. Abortando.")
        sys.exit(1)
    print("  Conexión OK")

    # 2. Calcular posiciones ANTES de cancelar (para no perder estado si se corta)
    print(f"\n[2] Calculando posición neta desde fills CSV ({SESSION_DATE})...")
    pos_near = _net_position_from_csv(NEAR)
    pos_far  = _net_position_from_csv(FAR)
    print(f"  {NEAR}: posición neta = {pos_near}")
    print(f"  {FAR} : posición neta = {pos_far}")

    # 3. Cancelar todas las órdenes abiertas
    print(f"\n[3] Cancelando órdenes abiertas para {NEAR} y {FAR}...")
    n_cancelled = _cancel_all_open(TICKERS)
    print(f"  Órdenes canceladas: {n_cancelled}")

    # 4. Enviar flatten para cada pata independientemente
    print("\n[4] Enviando órdenes de flatten...")
    sent_near = _flatten_leg(NEAR, pos_near)
    sent_far  = _flatten_leg(FAR,  pos_far)

    # 5. Esperar 10 segundos y verificar estado
    if sent_near or sent_far:
        print("\n[5] Esperando 10s para confirmación de ejecución...")
        time.sleep(10)

        # Re-leer fills post-ejecución
        pos_near_post = _net_position_from_csv(NEAR)
        pos_far_post  = _net_position_from_csv(FAR)
    else:
        pos_near_post = pos_near
        pos_far_post  = pos_far

    # 6. Resumen final
    print("\n" + "=" * 60)
    print("RESUMEN FINAL")
    print("=" * 60)
    print(f"  Posición antes : {NEAR}={pos_near} | {FAR}={pos_far}")
    print(f"  Posición post  : {NEAR}={pos_near_post} | {FAR}={pos_far_post}")
    print(f"  Órdenes canceladas: {n_cancelled}")
    print(f"  Flatten enviado: near={'SI' if sent_near else 'NO'} | far={'SI' if sent_far else 'NO'}")

    near_ok = pos_near == 0 or pos_near_post == 0
    far_ok  = pos_far  == 0 or pos_far_post  == 0
    estado  = "OK" if (near_ok and far_ok) else "PENDIENTE"

    summary = (
        f"Posicion antes: near={pos_near} far={pos_far} | "
        f"Ordenes canceladas: {n_cancelled} | "
        f"Flatten enviado: near={'SI' if sent_near else 'NO'} far={'SI' if sent_far else 'NO'} | "
        f"Estado: {estado}"
    )
    print(f"\n>>> {summary}")
    print("=" * 60)


if __name__ == "__main__":
    main()
