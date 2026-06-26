"""Ticker rollover helpers for DLR futures."""

from __future__ import annotations

import calendar
from datetime import date


_MONTH_MAP = {
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

_MONTH_REV = {
    1: "ENE",
    2: "FEB",
    3: "MAR",
    4: "ABR",
    5: "MAY",
    6: "JUN",
    7: "JUL",
    8: "AGO",
    9: "SEP",
    10: "OCT",
    11: "NOV",
    12: "DIC",
}


def _parse_ticker(ticker: str) -> tuple[str, int, int, bool]:
    """Parses DLR/MMMYY or DLR/MMMYYM and returns (root, year, month, is_mini)."""
    raw = str(ticker or "").strip().upper()
    if "/" not in raw:
        raise ValueError(f"Invalid ticker format: {ticker}")

    root, token = raw.split("/", 1)
    if len(token) < 5:
        raise ValueError(f"Invalid ticker token: {ticker}")

    is_mini = token.endswith("M")
    base = token[:-1] if is_mini else token
    if len(base) != 5:
        raise ValueError(f"Invalid ticker maturity token: {ticker}")

    month_txt = base[:3]
    year_txt = base[3:5]

    month = _MONTH_MAP.get(month_txt)
    if month is None or not year_txt.isdigit():
        raise ValueError(f"Invalid month/year in ticker: {ticker}")

    year = 2000 + int(year_txt)
    return root, year, month, is_mini


def _last_friday(year: int, month: int) -> date:
    """Returns the last Friday for given month/year."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    while d.weekday() != 4:  # Monday=0 ... Friday=4
        d = date.fromordinal(d.toordinal() - 1)
    return d


def _third_friday(year: int, month: int) -> date:
    """Returns the third Friday of the given month/year (DLR ROFEX settlement date)."""
    d = date(year, month, 1)
    # Days until first Friday (0 if the 1st is already Friday)
    days_until_first_fri = (4 - d.weekday()) % 7
    first_friday_day = 1 + days_until_first_fri
    # Third Friday = first Friday + 14 days
    return date(year, month, first_friday_day + 14)


def _build_ticker(root: str, year: int, month: int, is_mini: bool) -> str:
    token = f"{_MONTH_REV[month]}{str(year)[2:]}"
    if is_mini:
        token += "M"
    return f"{root}/{token}"


# Fechas de vencimiento confirmadas para DLR (último día hábil bursátil del mes).
# Fuente: reglamento A3 Mercados (ex Matba Rofex). Máxima prioridad en get_expiry_date.
_OVERRIDE_EXPIRY: dict[str, date] = {
    "DLR/MAY26":  date(2026, 5, 29),
    "DLR/MAY26M": date(2026, 5, 29),
    "DLR/JUN26":  date(2026, 6, 30),
    "DLR/JUN26M": date(2026, 6, 30),
    "DLR/JUL26":  date(2026, 7, 31),
    "DLR/JUL26M": date(2026, 7, 31),
    "DLR/AGO26":  date(2026, 8, 31),
    "DLR/AGO26M": date(2026, 8, 31),
    "DLR/SEP26":  date(2026, 9, 30),
    "DLR/SEP26M": date(2026, 9, 30),
    "DLR/OCT26":  date(2026, 10, 30),
    "DLR/OCT26M": date(2026, 10, 30),
    "DLR/NOV26":  date(2026, 11, 30),
    "DLR/NOV26M": date(2026, 11, 30),
    "DLR/DIC26":  date(2026, 12, 31),
    "DLR/DIC26M": date(2026, 12, 31),
}

# Fixed expiry overrides for contracts with non-standard settlement (e.g. SOJ).
# DLR uses the third-Friday rule; SOJ.ROS and SOJ.MIN use end-of-harvest-month.
_FIXED_EXPIRY: dict[str, date] = {
    "SOJ.ROS/MAY26": date(2026, 4, 30),
    "SOJ.ROS/JUL26": date(2026, 7, 31),
    "SOJ.MIN/MAY26": date(2026, 4, 30),
    "SOJ.MIN/JUL26": date(2026, 7, 31),
}

# Explicit next-contract map for sequences that skip months (SOJ: MAY26 -> JUL26).
_NEXT_TICKER_MAP: dict[str, str] = {
    "SOJ.ROS/MAY26": "SOJ.ROS/JUL26",
    "SOJ.MIN/MAY26": "SOJ.MIN/JUL26",
}


def get_expiry_date(ticker: str) -> date:
    """
    Retorna fecha de vencimiento de un ticker.

    Prioridad:
    1. _OVERRIDE_EXPIRY  — fechas confirmadas manualmente (DLR, etc.)
    2. _FIXED_EXPIRY     — fechas fijas para SOJ y similares
    3. Fallback          — _last_friday() calculado
    """
    normalized = str(ticker or "").strip().upper()
    if normalized in _OVERRIDE_EXPIRY:
        return _OVERRIDE_EXPIRY[normalized]
    if normalized in _FIXED_EXPIRY:
        return _FIXED_EXPIRY[normalized]
    _root, year, month, _is_mini = _parse_ticker(ticker)
    return _last_friday(year, month)


def is_expired(ticker: str, reference_date: date | None = None) -> bool:
    """
    Retorna True si el ticker ya vencio respecto a reference_date.

    Regla de rollover: si hoy >= fecha de vencimiento, se considera vencido.
    """
    ref = reference_date or date.today()
    return ref >= get_expiry_date(ticker)


def days_to_expiry(ticker: str, reference_date: date | None = None) -> int:
    """Retorna dias calendarios hasta el vencimiento. Negativo si ya venció."""
    ref = reference_date or date.today()
    return (get_expiry_date(ticker) - ref).days


def get_next_ticker(ticker: str, available_tickers: list[str] | None = None) -> str:
    """
    Dado un ticker vencido o proximo a vencer, retorna el siguiente activo.

    Consulta _NEXT_TICKER_MAP primero (para secuencias no mensuales como SOJ).
    Si available_tickers es None, construye secuencia mensual logica.
    Para mini conserva sufijo M.
    """
    normalized = str(ticker or "").strip().upper()
    if normalized in _NEXT_TICKER_MAP:
        return _NEXT_TICKER_MAP[normalized]
    root, year, month, is_mini = _parse_ticker(ticker)
    ref = date.today()
    current_expiry = get_expiry_date(ticker)

    if available_tickers:
        candidates: list[tuple[date, str]] = []
        for candidate in available_tickers:
            try:
                c_root, _c_year, _c_month, c_mini = _parse_ticker(candidate)
                c_exp = get_expiry_date(candidate)
                if c_root != root:
                    continue
                if c_mini != is_mini:
                    continue
                if c_exp < ref:
                    continue
                if c_exp <= current_expiry:
                    continue
                candidates.append((c_exp, candidate.upper()))
            except Exception:
                continue

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]

    # Logical monthly sequence from next month onward.
    for _ in range(120):
        month += 1
        if month > 12:
            month = 1
            year += 1
        candidate = _build_ticker(root, year, month, is_mini)
        if get_expiry_date(candidate) >= ref:
            return candidate

    # Fallback (should not happen in normal conditions).
    return _build_ticker(root, year, month, is_mini)


def get_active_ticker(ticker: str, reference_date: date | None = None) -> str:
    """
    Retorna ticker vigente: el actual si no vencio, o el siguiente si vencio.

    Si algo falla, devuelve el ticker original.
    """
    try:
        ref = reference_date or date.today()
        if is_expired(ticker, ref):
            return get_next_ticker(ticker)
        return str(ticker).upper()
    except Exception:
        return str(ticker)


def set_expiry_override(ticker: str, expiry: date) -> None:
    """
    Permite sobreescribir manualmente la fecha de vencimiento
    de un contrato especifico en caso de feriado o circular
    de A3 Mercados que mueva el vencimiento.

    Uso::

        from utils.ticker_roller import set_expiry_override
        from datetime import date
        set_expiry_override("DLR/MAY26", date(2026, 5, 28))
    """
    _OVERRIDE_EXPIRY[str(ticker).strip().upper()] = expiry


# Test rapido:
# get_expiry_date("DLR/ABR26") -> date(2026, 4, 17)  # tercer viernes abril 2026
# get_expiry_date("DLR/MAY26") -> date(2026, 5, 15)  # tercer viernes mayo 2026
# get_expiry_date("DLR/JUN26") -> date(2026, 6, 19)  # tercer viernes junio 2026
# get_expiry_date("SOJ.ROS/MAY26") -> date(2026, 4, 30)  # fijo
# get_expiry_date("SOJ.ROS/JUL26") -> date(2026, 7, 31)  # fijo
# is_expired("DLR/ABR26", date(2026, 4, 18)) -> True
# is_expired("DLR/MAY26", date(2026, 4, 30)) -> False
# get_next_ticker("DLR/ABR26") -> "DLR/MAY26"
# get_next_ticker("DLR/ABR26M") -> "DLR/MAY26M"
# get_next_ticker("SOJ.ROS/MAY26") -> "SOJ.ROS/JUL26"  # skip JUN
# get_active_ticker("DLR/ABR26", date(2026, 4, 18)) -> "DLR/MAY26"
# days_to_expiry("DLR/MAY26", date(2026, 5, 12)) -> 3
