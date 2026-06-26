"""Global position risk tracker shared across all bots in the same process.

Thread-safe singleton. Each bot registers its ticker limits on startup and
updates positions on every confirmed fill. The CS runner checks the near-leg
limit before submitting spread orders to avoid exceeding the combined position
cap with the MM bot.
"""
from __future__ import annotations

import threading
from collections import defaultdict


class GlobalRiskManager:
    """
    Trackea posición neta global por ticker entre todos los bots.
    Thread-safe. Singleton.
    """

    _instance: "GlobalRiskManager | None" = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "GlobalRiskManager":
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._positions: dict[str, int] = defaultdict(int)
                inst._limits: dict[str, int] = {}
                inst._rlock: threading.RLock = threading.RLock()
                cls._instance = inst
        return cls._instance

    def set_limit(self, ticker: str, max_position: int) -> None:
        """Configura el límite absoluto de posición neta para un ticker."""
        with self._rlock:
            self._limits[ticker] = abs(max_position)

    def update(self, ticker: str, delta: int) -> None:
        """Actualiza la posición neta del ticker en delta contratos (+BUY / -SELL)."""
        with self._rlock:
            self._positions[ticker] += delta

    def get_position(self, ticker: str) -> int:
        """Retorna la posición neta actual del ticker."""
        with self._rlock:
            return self._positions.get(ticker, 0)

    def check(self, ticker: str, side: str, qty: int) -> bool:
        """
        Retorna True si la operación está dentro del límite global.

        Parameters
        ----------
        ticker : str
        side   : 'BUY' o 'SELL'
        qty    : contratos a operar (positivo)
        """
        with self._rlock:
            limit = self._limits.get(ticker)
            if limit is None:
                return True  # sin límite configurado → permitir
            pos = self._positions.get(ticker, 0)
            new_pos = pos + qty if side.upper() == "BUY" else pos - qty
            return abs(new_pos) <= limit

    def get_all(self) -> dict[str, int]:
        """Retorna copia del dict completo {ticker: net_position}."""
        with self._rlock:
            return dict(self._positions)

    def reset(self, ticker: str | None = None) -> None:
        """Resetea posición de un ticker (o todos si ticker=None). Solo para tests."""
        with self._rlock:
            if ticker is None:
                self._positions.clear()
            else:
                self._positions.pop(ticker, None)


# Singleton global exportado
global_risk = GlobalRiskManager()
