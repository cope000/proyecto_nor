"""CSV-based per-session fill persistence for MM runners."""

from __future__ import annotations

import csv
import logging
import os as _os
import threading
from datetime import datetime
from pathlib import Path


logger = logging.getLogger("fill_logger")
_THIS_DIR = _os.path.dirname(_os.path.abspath(__file__))
_PROJECT_ROOT = _os.path.dirname(_THIS_DIR)


class FillLogger:
    """Persists fills to a UTF-8 CSV file and provides session aggregates."""

    FIELDNAMES = [
        "timestamp",
        "ticker",
        "side",
        "price",
        "qty",
        "order_id",
        "session_date",
    ]

    def __init__(self, ticker: str, session_date: str, output_dir: str = "logs/fills") -> None:
        self.ticker = str(ticker)
        self.session_date = str(session_date)
        self.lock = threading.Lock()

        safe_ticker = self.ticker.replace("/", "-")
        if not _os.path.isabs(output_dir):
            resolved_dir = Path(_PROJECT_ROOT) / output_dir
        else:
            resolved_dir = Path(output_dir)

        _os.makedirs(resolved_dir, exist_ok=True)
        self.file_path = resolved_dir / f"{safe_ticker}_{self.session_date}.csv"
        logger.info("FillLogger path: %s", str(self.file_path.resolve()))
        logger.info("FillLogger initialized | path=%s", str(self.file_path))

        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=self.FIELDNAMES, delimiter=",")
                writer.writeheader()

    def log_fill(
        self,
        side: str,
        price: float,
        qty: int,
        order_id: str,
        timestamp: str | None = None,
    ) -> None:
        """Appends a fill row to the session CSV. Never raises on IO failures."""
        row = {
            "timestamp": timestamp or datetime.now().isoformat(timespec="milliseconds"),
            "ticker": self.ticker,
            "side": str(side).upper(),
            "price": float(price),
            "qty": int(qty),
            "order_id": str(order_id),
            "session_date": self.session_date,
        }

        try:
            with self.lock:
                with self.file_path.open("a", newline="", encoding="utf-8") as csv_file:
                    writer = csv.DictWriter(csv_file, fieldnames=self.FIELDNAMES, delimiter=",")
                    writer.writerow(row)
        except Exception as exc:
            import traceback

            logger.error(
                "FillLogger WRITE FAILED | path=%s | error=%s | trace=%s",
                str(self.file_path),
                exc,
                traceback.format_exc(),
            )

    def get_session_fills(self) -> list[dict[str, str]]:
        """Returns all fills from current session CSV as list of dict rows."""
        if not self.file_path.exists():
            return []

        rows: list[dict[str, str]] = []
        with self.lock:
            with self.file_path.open("r", newline="", encoding="utf-8") as csv_file:
                reader = csv.DictReader(csv_file, delimiter=",")
                for row in reader:
                    rows.append(dict(row))
        return rows

    def get_net_position(self) -> int:
        """Returns session net position: buys minus sells."""
        net = 0
        for row in self.get_session_fills():
            side = str(row.get("side", "")).upper()
            qty = int(float(row.get("qty", 0) or 0))
            if side == "BUY":
                net += qty
            elif side == "SELL":
                net -= qty
        return net

    def get_session_pnl(self, current_mid: float) -> float:
        """Returns mark-to-mid session PnL from persisted fills."""
        cash = 0.0
        position = 0

        for row in self.get_session_fills():
            side = str(row.get("side", "")).upper()
            qty = int(float(row.get("qty", 0) or 0))
            price = float(row.get("price", 0) or 0)

            if side == "BUY":
                position += qty
                cash -= price * qty
            elif side == "SELL":
                position -= qty
                cash += price * qty

        return cash + (position * float(current_mid))
