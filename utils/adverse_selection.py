"""Adverse selection detector for market making."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


class AdverseSelectionDetector:
    """
    Detecta adverse selection basándose en imbalance de fills recientes
    y movimiento del mid price post-fill.
    
    Adverse selection ocurre cuando fills en un lado se acumulan y el precio
    se mueve en contra inmediatamente después. Señal de que estamos siendo
    "picked off" por traders informados.
    
    Parámetros:
        window: int — cantidad de fills recientes a analizar (default 10)
        imbalance_threshold: float — umbral de imbalance para activar (default 0.6)
        spread_multiplier: float — factor de spread cuando se activa (default 1.5)
        cooldown_fills: int — fills sin señal antes de desactivar (default 5)
    """

    def __init__(
        self,
        window: int = 10,
        imbalance_threshold: float = 0.6,
        spread_multiplier: float = 1.5,
        cooldown_fills: int = 5,
    ) -> None:
        self.window = max(int(window), 2)
        self.imbalance_threshold = float(imbalance_threshold)
        self.spread_multiplier = float(spread_multiplier)
        self.cooldown_fills = max(int(cooldown_fills), 1)

        # Deque of recent fills: {side: 'BUY'|'SELL', mid_at_fill: float}
        self._fills: deque[dict[str, Any]] = deque(maxlen=self.window)

        # Current mid price for post-fill movement detection
        self._current_mid: float = 0.0

        # Adverse selection state
        self._adverse_active: bool = False
        self._cooldown_counter: int = 0

        # Thread-safe access
        self._lock = threading.Lock()

    def on_fill(self, side: str, price: float, mid_at_fill: float) -> None:
        """
        Registra un fill.
        
        Args:
            side: 'BUY' o 'SELL'
            price: Precio de ejecución del fill
            mid_at_fill: Mid price en el momento del fill
        """
        side_upper = str(side).upper().strip()
        if side_upper not in ("BUY", "SELL"):
            return

        with self._lock:
            self._fills.append({
                "side": side_upper,
                "mid_at_fill": float(mid_at_fill),
            })

            # Decrement cooldown if adverse is active
            if self._adverse_active and self._cooldown_counter > 0:
                self._cooldown_counter -= 1
                if self._cooldown_counter <= 0:
                    self._adverse_active = False
                    return

            # Recalculate adverse selection signal
            self._update_adverse_state()

    def on_mid_update(self, mid: float) -> None:
        """
        Actualiza el mid actual para calcular movimiento post-fill.
        
        Esta método solo almacena el mid, no dispara lógica de detección.
        """
        with self._lock:
            self._current_mid = float(mid)

    def is_adverse(self) -> bool:
        """Retorna True si se detecta adverse selection activa."""
        with self._lock:
            return self._adverse_active

    def get_spread_multiplier(self) -> float:
        """
        Retorna el multiplicador de spread actual.
        
        Returns:
            1.0 si no hay adverse selection
            spread_multiplier si hay adverse selection activa
        """
        with self._lock:
            return self.spread_multiplier if self._adverse_active else 1.0

    def get_state(self) -> dict[str, Any]:
        """
        Retorna estado para observabilidad.
        
        Returns:
            {
                'adverse_active': bool,
                'imbalance': float,  # -1.0 a 1.0
                'spread_multiplier': float,
                'fills_in_window': int,
                'cooldown_remaining': int,
            }
        """
        with self._lock:
            imbalance = self._calculate_imbalance()
            return {
                "adverse_active": self._adverse_active,
                "imbalance": imbalance,
                "spread_multiplier": self.spread_multiplier if self._adverse_active else 1.0,
                "fills_in_window": len(self._fills),
                "cooldown_remaining": self._cooldown_counter,
            }

    # ====================================================================
    # INTERNAL METHODS
    # ====================================================================

    def _calculate_imbalance(self) -> float:
        """
        Calcula imbalance = (buys - sells) / (buys + sells).
        
        Rango: -1.0 (todo sells) a +1.0 (todo buys)
        
        Returns:
            Imbalance en rango [-1.0, 1.0], o 0.0 si no hay fills
        """
        if len(self._fills) == 0:
            return 0.0

        buy_count = sum(1 for f in self._fills if f["side"] == "BUY")
        sell_count = sum(1 for f in self._fills if f["side"] == "SELL")

        total = buy_count + sell_count
        if total == 0:
            return 0.0

        return float(buy_count - sell_count) / float(total)

    def _check_adverse_signal(self) -> bool:
        """
        Verifica si hay señal de adverse selection.
        
        Condiciones:
        1. abs(imbalance) > threshold
        2. Tenemos al menos window//2 fills (mínimo data)
        3. Movimiento del mid en contra de la posición dominante
        
        Returns:
            True si se detecta adverse selection
        """
        if len(self._fills) < max(self.window // 2, 2):
            return False

        imbalance = self._calculate_imbalance()

        if abs(imbalance) <= self.imbalance_threshold:
            return False

        # Check post-fill movement: did price move against dominant side?
        # Si mostly BUY (imbalance > 0), precio debería subir (mid > mid_at_fill)
        # Si mostly SELL (imbalance < 0), precio debería bajar (mid < mid_at_fill)
        
        if len(self._fills) == 0:
            return False

        # Last fill's mid
        last_mid_at_fill = self._fills[-1]["mid_at_fill"]
        
        # Movement since last fill
        movement = self._current_mid - last_mid_at_fill

        # If mostly buy but price went down, or mostly sell but price went up
        # → adverse selection signal
        if imbalance > self.imbalance_threshold and movement < 0:
            # Mostly BUY but price DOWN → picked off on buys
            return True
        elif imbalance < -self.imbalance_threshold and movement > 0:
            # Mostly SELL but price UP → picked off on sells
            return True

        return False

    def _update_adverse_state(self) -> None:
        """
        Actualiza el estado de adverse selection interno.
        
        Esta es una llamada interna (dentro del lock).
        """
        signal = self._check_adverse_signal()

        if signal and not self._adverse_active:
            # Activate adverse mode
            self._adverse_active = True
            self._cooldown_counter = self.cooldown_fills
        elif not signal and self._adverse_active:
            # Signal cleared but still in cooldown
            if self._cooldown_counter > 0:
                self._cooldown_counter -= 1
            if self._cooldown_counter <= 0:
                self._adverse_active = False
