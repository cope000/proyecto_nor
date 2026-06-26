"""Listado y filtrado de instrumentos disponibles en reMarkets."""

from typing import Any
import pyRofex
from core.utils import setup_logger, print_table

logger = setup_logger("instruments")

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


def _parse_expiry_key(symbol: str) -> tuple[int, int, str]:
    """Convierte DLR/MESYY[SFX] en clave ordenable (anio, mes, sufijo)."""
    if "/" not in symbol:
        return (9999, 99, symbol)
    token = symbol.split("/", 1)[1].strip().upper()
    if len(token) < 5:
        return (9999, 99, token)
    month = _MONTHS.get(token[:3], 99)
    year_str = token[3:5]
    if not year_str.isdigit():
        return (9999, month, token)
    year = 2000 + int(year_str)
    suffix = token[5:]
    return (year, month, suffix)


def get_all_instruments() -> list[dict[str, Any]]:
    """Obtiene todos los instrumentos disponibles."""
    try:
        response = pyRofex.get_all_instruments()
        instruments: list[dict[str, Any]] = response.get("instruments", [])
        logger.info("Total de instrumentos obtenidos: %d", len(instruments))
        if instruments:
            logger.info("Ejemplo instrumento raw: %s", instruments[0])
        return instruments
    except Exception as e:
        logger.error("Error al obtener instrumentos: %s", e)
        return []


def filter_instruments(keyword: str) -> list[dict[str, Any]]:
    """Filtra instrumentos cuyo ticker contenga el keyword (case-insensitive)."""
    all_inst = get_all_instruments()
    keyword_upper = keyword.upper()
    filtered = [
        i for i in all_inst
        if keyword_upper in i.get("instrumentId", {}).get("symbol", "").upper()
    ]
    logger.info("Instrumentos que coinciden con '%s': %d", keyword, len(filtered))
    return filtered


def get_futures_dollar() -> list[dict[str, Any]]:
    """Retorna solo futuros simples de dolar DLR (sin opciones ni spreads)."""
    filtered = filter_instruments("DLR")
    futures = [
        inst
        for inst in filtered
        if inst.get("cficode", "") == "FXXXSX"
        and inst.get("instrumentId", {}).get("symbol", "").startswith("DLR/")
        and " " not in inst.get("instrumentId", {}).get("symbol", "")
        and "/" in inst.get("instrumentId", {}).get("symbol", "")
    ]
    futures.sort(key=lambda x: _parse_expiry_key(x.get("instrumentId", {}).get("symbol", "")))
    logger.info("Futuros de dolar encontrados: %d", len(futures))
    return futures


def print_instruments(instruments: list[dict[str, Any]]) -> None:
    """Imprime instrumentos en formato tabla."""
    headers = ["Ticker", "Descripcion", "Moneda", "Segmento"]
    rows = []
    for inst in instruments:
        inst_id = inst.get("instrumentId", {})
        ticker = inst_id.get("symbol", "N/A")
        cfi = inst.get("cficode", "N/A")
        if cfi == "FXXXSX":
            desc = "Futuro"
        elif cfi == "OPEFXS":
            desc = "Opcion"
        elif cfi == "FXXXXX":
            desc = "Spread"
        else:
            desc = cfi
        currency = inst.get("currency", "N/A")
        segment = inst_id.get("marketId", "N/A")
        rows.append([ticker, desc[:20], currency, segment])
    print_table(headers, rows)


if __name__ == "__main__":
    from connect import connect
    if connect():
        futures = get_futures_dollar()
        print_instruments(futures)
