"""Script temporal — listar órdenes activas en el broker para detectar huérfanas."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyRofex
from core import credentials

pyRofex.initialize(
    user=credentials.USER,
    password=credentials.PASSWORD,
    account=credentials.ACCOUNT,
    environment=pyRofex.Environment.REMARKET,
)

response = pyRofex.get_all_orders_status(account=credentials.ACCOUNT)
orders = response.get("orders") or []

active = [
    o for o in orders
    if str(o.get("status", "")).upper() in ("NEW", "PARTIALLY_FILLED")
]

print(f"Total órdenes en respuesta: {len(orders)}")
print(f"Órdenes activas (NEW/PARTIALLY_FILLED): {len(active)}")
print()

if not active:
    print("✓ Sin órdenes activas. Book limpio.")
else:
    print("⚠ ÓRDENES ACTIVAS ENCONTRADAS:")
    for o in active:
        sym  = o.get("instrumentId", {}).get("symbol", "?")
        side = o.get("side", "?")
        qty  = o.get("orderQty", "?")
        px   = o.get("price", "?")
        st   = o.get("status", "?")
        oid  = o.get("orderId", "?")
        coid = o.get("clOrdId", "?")
        print(f"  {sym} | {side} {qty} @ {px} | status={st} | orderId={oid} | clOrdId={coid}")
