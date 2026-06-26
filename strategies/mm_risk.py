"""Intraday risk management for the Market Maker strategy."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass

from core.utils import setup_logger

logger = setup_logger("mm_risk")


@dataclass
class MMRiskConfig:
    """Configuration for MM-specific risk limits."""

    # Layer 1: Volatility Circuit Breaker
    VOL_WINDOW_SECONDS: int = 300
    VOL_CIRCUIT_BREAKER_BPS: float = 50.0
    VOL_RESUME_BPS: float = 30.0

    # Layer 2: Max Daily Loss
    MAX_DAILY_LOSS_ARS: float = 500_000.0

    # Layer 3: Position Time Limit
    MAX_POSITION_HOLD_SECONDS: int = 600

    # Layer 4: Spread Widening by Volatility
    VOL_WIDEN_THRESHOLD_BPS: float = 20.0
    VOL_SPREAD_MULTIPLIER_EXTRA: float = 2.0

    # Layer 5: Emergency Flatten
    FLATTEN_USE_MARKET_ORDER: bool = False
    FLATTEN_AGGRESSION_TICKS: int = 2


class MMRiskManager:
    """Intraday risk manager specific to the Market Maker strategy."""

    def __init__(self, config: MMRiskConfig | None = None) -> None:
        self.config = config or MMRiskConfig()

        self._price_history: deque[tuple[float, float]] = deque()
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_since: float = 0.0

        self._daily_loss_limit_hit: bool = False
        self._trading_date: str = ""

        self._position_opened_at: float = 0.0
        self._last_known_position: int = 0

        self._current_vol_bps: float = 0.0
        self._spread_multiplier: float = 1.0

        self._flatten_attempted: bool = False
        self._flatten_attempt_time: float = 0.0
        self._flatten_max_retries: int = 3
        self._flatten_retry_count: int = 0
        self._flatten_cooldown_seconds: float = 30.0
        self._flatten_manual_alerted: bool = False

    def _reset_flatten_state(self) -> None:
        self._flatten_attempted = False
        self._flatten_attempt_time = 0.0
        self._flatten_retry_count = 0
        self._flatten_manual_alerted = False

    def reset_daily(self) -> None:
        """Reset daily counters. Call at start of each trading day."""
        self._daily_loss_limit_hit = False
        self._circuit_breaker_active = False
        self._price_history.clear()
        self._position_opened_at = 0.0
        self._last_known_position = 0
        self._reset_flatten_state()
        logger.info("MM risk manager daily reset")

    def on_price_update(self, price: float) -> None:
        """Feed a new price for volatility calculation."""
        if price <= 0:
            return
        now = time.time()
        self._price_history.append((now, float(price)))

        cutoff = now - self.config.VOL_WINDOW_SECONDS
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()

    def _compute_realized_vol_bps(self) -> float:
        """Compute realized volatility in bps over the rolling window."""
        if len(self._price_history) < 10:
            self._current_vol_bps = 0.0
            return 0.0

        prices = [p for _, p in self._price_history]
        if not prices or prices[0] <= 0:
            self._current_vol_bps = 0.0
            return 0.0

        log_returns: list[float] = []
        for i in range(1, len(prices)):
            if prices[i] > 0 and prices[i - 1] > 0:
                log_returns.append(math.log(prices[i] / prices[i - 1]))

        if len(log_returns) < 5:
            self._current_vol_bps = 0.0
            return 0.0

        mean_ret = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_ret) ** 2 for r in log_returns) / len(log_returns)
        std_ret = math.sqrt(variance)

        range_bps = ((max(prices) - min(prices)) / prices[0]) * 10000.0
        std_bps = std_ret * 10000.0
        vol_bps = max(std_bps, range_bps / 4.0)

        self._current_vol_bps = vol_bps
        return vol_bps

    def check_circuit_breaker(self) -> bool:
        """Returns True if trading is allowed, False if CB is active."""
        vol_bps = self._compute_realized_vol_bps()

        if self._circuit_breaker_active:
            if vol_bps < self.config.VOL_RESUME_BPS:
                self._circuit_breaker_active = False
                duration = time.time() - self._circuit_breaker_since
                logger.info(
                    "CIRCUIT BREAKER OFF | vol=%.2f bps < resume=%.2f bps | was active %.0f seconds",
                    vol_bps,
                    self.config.VOL_RESUME_BPS,
                    duration,
                )
                return True
            return False

        if vol_bps > self.config.VOL_CIRCUIT_BREAKER_BPS:
            self._circuit_breaker_active = True
            self._circuit_breaker_since = time.time()
            logger.warning(
                "CIRCUIT BREAKER ON | vol=%.2f bps > threshold=%.2f bps | STOPPING QUOTES",
                vol_bps,
                self.config.VOL_CIRCUIT_BREAKER_BPS,
            )
            return False

        return True

    def check_daily_loss(self, current_pnl: float) -> bool:
        """Returns True if trading is allowed, False if daily loss limit hit."""
        if self._daily_loss_limit_hit:
            return False

        if current_pnl < -self.config.MAX_DAILY_LOSS_ARS:
            self._daily_loss_limit_hit = True
            logger.error(
                "DAILY LOSS LIMIT HIT | pnl=%.2f < limit=-%.2f | MM SHUTDOWN FOR TODAY",
                current_pnl,
                self.config.MAX_DAILY_LOSS_ARS,
            )
            return False

        return True

    def check_position_time(self, current_position: int) -> bool:
        """Returns True if position hold time is OK, False if flatten required."""
        now = time.time()

        if current_position == 0:
            self._position_opened_at = 0.0
            self._last_known_position = 0
            return True

        if self._last_known_position == 0 and current_position != 0:
            self._position_opened_at = now
            self._last_known_position = current_position
            logger.info(
                "Position opened | pos=%d | time limit=%d seconds",
                current_position,
                self.config.MAX_POSITION_HOLD_SECONDS,
            )
            return True

        self._last_known_position = current_position

        if self._position_opened_at > 0:
            elapsed = now - self._position_opened_at
            if elapsed > self.config.MAX_POSITION_HOLD_SECONDS:
                logger.warning(
                    "POSITION TIME LIMIT | pos=%d | held for %.0f seconds > limit=%d | FLATTEN REQUIRED",
                    current_position,
                    elapsed,
                    self.config.MAX_POSITION_HOLD_SECONDS,
                )
                return False

        return True

    def get_spread_multiplier(self) -> float:
        """Returns spread multiplier based on current volatility."""
        vol_bps = self._current_vol_bps
        if vol_bps > self.config.VOL_WIDEN_THRESHOLD_BPS:
            ratio = min(vol_bps / self.config.VOL_CIRCUIT_BREAKER_BPS, 1.0)
            multiplier = 1.0 + (self.config.VOL_SPREAD_MULTIPLIER_EXTRA - 1.0) * ratio
            if abs(multiplier - self._spread_multiplier) > 0.1:
                logger.info(
                    "Spread multiplier adjusted | vol=%.2f bps | multiplier=%.2fx",
                    vol_bps,
                    multiplier,
                )
            self._spread_multiplier = multiplier
            return multiplier

        self._spread_multiplier = 1.0
        return 1.0

    def should_flatten(self, current_position: int, current_pnl: float) -> bool:
        """Returns True if emergency flatten should be executed now."""
        _ = current_pnl
        if current_position == 0:
            self.on_flatten_success()
            return False

        self._last_known_position = current_position

        if self._flatten_retry_count >= self._flatten_max_retries:
            if not self._flatten_manual_alerted:
                logger.error(
                    "FLATTEN FAILED after %d retries - MANUAL INTERVENTION REQUIRED - pos=%d",
                    self._flatten_max_retries,
                    current_position,
                )
                self._flatten_manual_alerted = True
            return False

        if self._flatten_attempted:
            elapsed = time.time() - self._flatten_attempt_time
            if elapsed < self._flatten_cooldown_seconds:
                return False

        if self._daily_loss_limit_hit:
            logger.warning("FLATTEN TRIGGER: daily loss limit with open position pos=%d", current_position)
            return True

        if self._circuit_breaker_active:
            logger.warning("FLATTEN TRIGGER: circuit breaker active with open position pos=%d", current_position)
            return True

        if not self.check_position_time(current_position):
            return True

        return False

    def on_flatten_attempt(self, success: bool) -> None:
        """Tracks the result of an emergency flatten attempt."""
        self._flatten_attempted = True
        self._flatten_attempt_time = time.time()
        self._flatten_retry_count += 1

        if success:
            logger.info(
                "Flatten order sent successfully (attempt %d/%d)",
                self._flatten_retry_count,
                self._flatten_max_retries,
            )
            return

        logger.warning(
            "Flatten order FAILED (attempt %d/%d) - next retry in %.0f seconds",
            self._flatten_retry_count,
            self._flatten_max_retries,
            self._flatten_cooldown_seconds,
        )

        if self._flatten_retry_count >= self._flatten_max_retries and not self._flatten_manual_alerted:
            logger.error(
                "FLATTEN FAILED after %d retries - MANUAL INTERVENTION REQUIRED - pos=%d",
                self._flatten_max_retries,
                self._last_known_position,
            )
            self._flatten_manual_alerted = True

    def on_flatten_success(self) -> None:
        """Resets flatten retry state once the position returns to flat."""
        if self._flatten_attempted or self._flatten_retry_count > 0:
            logger.info("Flatten state reset after position returned to zero")
        self._reset_flatten_state()

    def get_flatten_price(self, position: int, market_bid: float, market_ask: float, tick_size: float = 0.5) -> float:
        """Returns aggressive limit price for flattening."""
        aggression = self.config.FLATTEN_AGGRESSION_TICKS * tick_size

        if position > 0:
            price = market_bid - aggression
            logger.info("Flatten SELL price=%.2f (bid=%.2f - aggression=%.2f)", price, market_bid, aggression)
            return price

        price = market_ask + aggression
        logger.info("Flatten BUY price=%.2f (ask=%.2f + aggression=%.2f)", price, market_ask, aggression)
        return price

    def get_status_summary(self) -> dict[str, float | bool]:
        """Returns current MM risk status for dashboard display."""
        held_seconds = 0.0
        if self._position_opened_at > 0:
            held_seconds = round(time.time() - self._position_opened_at, 0)

        return {
            "circuit_breaker": self._circuit_breaker_active,
            "daily_loss_limit_hit": self._daily_loss_limit_hit,
            "current_vol_bps": round(self._current_vol_bps, 2),
            "spread_multiplier": round(self._spread_multiplier, 2),
            "position_held_seconds": held_seconds,
        }