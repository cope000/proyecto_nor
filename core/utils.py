"""Helpers: logging configurado, formateo y timestamps."""

import logging
import sys
from datetime import datetime


def setup_logger(name: str = "proyecto_nor", level: int = logging.INFO) -> logging.Logger:
    """Configura y retorna un logger con salida a consola."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def now_str() -> str:
    """Timestamp actual como string legible."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def fmt_price(value: float | None) -> str:
    """Formatea un precio para mostrar, maneja None."""
    if value is None:
        return "---"
    return f"{value:,.2f}"


def print_table(headers: list[str], rows: list[list[str]], col_width: int = 20) -> None:
    """Imprime datos tabulares simple en consola."""
    header_line = " | ".join(h.ljust(col_width) for h in headers)
    sep = "-" * len(header_line)
    print(sep)
    print(header_line)
    print(sep)
    for row in rows:
        print(" | ".join(str(c).ljust(col_width) for c in row))
    print(sep)
