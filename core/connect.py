"""Inicialización de conexión a reMarkets vía pyRofex."""

import _ws_patch  # Parche isAlive para websocket-client >=1.0.0 y Python 3.12+
import time
import pyRofex
from core import credentials as config
from core.utils import setup_logger

logger = setup_logger("connect")


def _resolve_env() -> pyRofex.Environment:
    """Resuelve el entorno desde config.ENV con fallback seguro a REMARKET."""
    env_name = (config.ENV or "REMARKET").upper()
    if env_name == "LIVE":
        return pyRofex.Environment.LIVE
    return pyRofex.Environment.REMARKET


def connect(max_retries: int = 3, retry_sleep_seconds: float = 1.5) -> bool:
    """Inicializa la conexión a reMarkets con reintentos ante fallos transitorios."""
    env = _resolve_env()
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            pyRofex.initialize(
                user=config.USER,
                password=config.PASSWORD,
                account=config.ACCOUNT,
                environment=env,
            )
            logger.info(
                "Conexion inicializada exitosamente con usuario %s (intento %d/%d)",
                config.USER,
                attempt,
                max_retries,
            )
            return True
        except Exception as e:
            last_error = e
            logger.warning(
                "Fallo de conexion (intento %d/%d): %s",
                attempt,
                max_retries,
                e,
            )
            if attempt < max_retries:
                time.sleep(retry_sleep_seconds)

    logger.error("Error al conectar: %s", last_error)
    return False


def test_connection() -> list[str] | None:
    """Verifica la conexión obteniendo los segmentos disponibles."""
    try:
        segments = pyRofex.get_segments()
        seg_list: list[str] = segments.get("segments", [])
        logger.info("Conexion activa - Segmentos disponibles: %s", seg_list)
        return seg_list
    except Exception as e:
        logger.error("Error al obtener segmentos: %s", e)
        return None


if __name__ == "__main__":
    if connect():
        test_connection()
