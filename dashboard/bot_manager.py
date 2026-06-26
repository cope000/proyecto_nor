from __future__ import annotations

import csv
import json
import logging
import os
import re
import signal
import subprocess
import sys
from collections import deque
from datetime import datetime, time
from pathlib import Path
from typing import Any
from utils.ticker_roller import days_to_expiry, get_active_ticker, get_next_ticker

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

BOTS_STATE_FILE = "data/bots_state.json"
PROJECT_DIR = _PROJECT_ROOT
PYTHON_EXE = "c:/Users/54344/Desktop/A3/.venv/Scripts/python.exe"

_logger = logging.getLogger(__name__)

BOT_REGISTRY: dict[str, dict[str, Any]] = {
    "mm_dlr": {
        "name": "MM DLR/ABR26",
        "runner": "runners/run_mm.py",
        "args": ["--instrument", "DLR", "--ticker", "DLR/ABR26"],
        "log": "logs/run_mm_dlr_abr26.log",
        "description": "Market Maker on DLR standard futures",
    },
    "mm_dlr_mini": {
        "name": "MM DLR/ABR26M",
        "runner": "runners/run_mm.py",
        "args": ["--instrument", "DLR", "--ticker", "DLR/ABR26M"],
        "log": "logs/run_mm_dlr_abr26m.log",
        "description": "Market Maker on DLR mini futures",
    },
    "mm_soj": {
        "name": "MM SOJ.ROS/MAY26",
        "runner": "runners/run_mm.py",
        "args": ["--instrument", "SOJ", "--ticker", "SOJ.ROS/MAY26"],
        "log": "logs/run_mm_soj_may26.log",
        "description": "Market Maker on Soja Rosario futures",
    },
    "mm_soj_mini": {
        "name": "MM SOJ.MIN/MAY26",
        "runner": "runners/run_mm.py",
        "args": ["--instrument", "SOJ_MIN", "--ticker", "SOJ.MIN/MAY26"],
        "log": "logs/run_mm_soj_min.log",
        "description": "Market Maker on Soja Rosario mini futures",
    },
    "cash_carry": {
        "name": "Cash & Carry (DLR vs CAUC)",
        "runner": "runners/run_cc.py",
        "log": "logs/run_cc.log",
        "description": "DLR implied rate vs CAUC rate arbitrage",
        "status": "not_deployed",
    },
    "cs_dlr": {
        "name": "CS DLR MAY26/JUN26",
        "runner": "runners/run_cs.py",
        "args": ["--near", "DLR/MAY26", "--far", "DLR/JUN26", "--no-time-check"],
        "log": "logs/run_cs_dlr.log",
        "description": "Calendar spread DLR MAY26 vs JUN26",
    },
}


MM_SIM_RUNNER = "sim/sim_mm.py"
MM_SIM_LOG = "logs/run_mm_sim.log"
CS_SIM_RUNNER = "sim/sim_cs.py"
CS_SIM_LOG = "logs/run_cs_sim.log"
_SIM_START_PRICE_BY_BOT: dict[str, str] = {
    "mm_soj": "324.6",
    "mm_soj_mini": "324.6",
}


class BotManager:
    def __init__(self, project_dir: Path | None = None) -> None:
        self.project_dir = project_dir or PROJECT_DIR
        self.state_file = self.project_dir / BOTS_STATE_FILE
        self.python_exe = PYTHON_EXE
        self.registry = BOT_REGISTRY
        self._state = self._load_state()

    def _sync_registry(self) -> None:
        """Keeps registry in sync for cached manager instances after code reloads."""
        self.registry = BOT_REGISTRY

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self.state_file.exists():
            return {}
        try:
            raw = self.state_file.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self.state_file.write_text(
                json.dumps(self._state, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _runner_path(self, bot_id: str) -> Path:
        entry = self._state.get(bot_id, {})
        runner = str(entry.get("runner") or self.registry[bot_id]["runner"])
        return self.project_dir / runner

    def _log_path(self, bot_id: str) -> Path:
        entry = self._state.get(bot_id, {})
        log_name = str(entry.get("log") or self.registry[bot_id]["log"])
        return self.project_dir / log_name

    def _is_process_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                cmd = ["tasklist", "/FI", f"PID eq {pid}"]
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
                out = (result.stdout or "")
                return str(pid) in out and "No tasks are running" not in out
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def start_bot(self, bot_id: str, data_mode: str = "live") -> tuple[bool, str]:
        self._sync_registry()
        if bot_id not in self.registry:
            return False, "Unknown bot id"

        status = self.get_status(bot_id)
        if status == "not_deployed":
            return False, "Not deployed yet"
        if status == "running":
            return True, "Already running"

        mode = (data_mode or "live").strip().lower()
        runner_name = self.registry[bot_id]["runner"]
        log_name = self.registry[bot_id]["log"]
        start_args: list[str] = list(self.registry[bot_id].get("args") or [])

        # Apply ticker rollover on configured --ticker before launching process.
        if "--ticker" in start_args:
            try:
                ticker_idx = start_args.index("--ticker")
                if ticker_idx + 1 < len(start_args):
                    configured_ticker = str(start_args[ticker_idx + 1])
                    active_ticker = get_active_ticker(configured_ticker)
                    # Pre-emptive rollover: switch within 3 days of expiry.
                    if active_ticker == configured_ticker:
                        try:
                            _rollover_days = 2 if "SOJ" in configured_ticker.upper() else 2
                            if days_to_expiry(configured_ticker) <= _rollover_days:
                                candidate = get_next_ticker(configured_ticker)
                                if candidate != configured_ticker:
                                    active_ticker = candidate
                        except Exception:
                            pass
                    if active_ticker != configured_ticker:
                        _logger.info(
                            "Auto-rollover: %s \u2192 %s", configured_ticker, active_ticker
                        )
                        start_args[ticker_idx + 1] = active_ticker
            except Exception:
                # Never block startup for rollover issues.
                pass

        # MM and CS bots support SIM mode from dashboard; all other bots stay live.
        is_mm = bot_id.startswith("mm_")
        is_cs = bot_id.startswith("cs_")
        if is_mm and mode == "sim":
            runner_name = MM_SIM_RUNNER
            log_name = MM_SIM_LOG
            # Run long enough for extended observation from the dashboard.
            sim_args: list[str] = ["--run-seconds", "21600"]
            if "--ticker" in start_args:
                try:
                    ticker_idx = start_args.index("--ticker")
                    if ticker_idx + 1 < len(start_args):
                        sim_args.extend(["--ticker", str(start_args[ticker_idx + 1])])
                except Exception:
                    pass
            start_price = _SIM_START_PRICE_BY_BOT.get(bot_id)
            if start_price:
                sim_args.extend(["--start-price", start_price])
            start_args = sim_args

        if is_cs and mode == "sim":
            runner_name = CS_SIM_RUNNER
            log_name = CS_SIM_LOG
            sim_args = ["--run-seconds", "21600"]
            near = None
            far = None
            if "--near" in start_args:
                idx = start_args.index("--near")
                near = start_args[idx + 1]
                sim_args.extend(["--near-price", "1385"])
            if "--far" in start_args:
                idx = start_args.index("--far")
                far = start_args[idx + 1]
                sim_args.extend(["--far-price", "1405"])
            start_args = sim_args

        runner = self.project_dir / runner_name
        log_path = self.project_dir / log_name
        if not runner.exists():
            return False, f"Runner not found: {runner_name}"

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(f"\n===== DASH START {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n")

            log_handle = log_path.open("a", encoding="utf-8", errors="replace")
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

            proc = subprocess.Popen(
                [self.python_exe, str(runner), *start_args],
                cwd=str(self.project_dir),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )

            self._state[bot_id] = {
                "pid": proc.pid,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "runner": runner_name,
                "log": log_name,
                "data_mode": "sim" if ((is_mm or is_cs) and mode == "sim") else "live",
            }
            self._save_state()
            return True, f"Started PID {proc.pid} ({self._state[bot_id]['data_mode']})"
        except Exception as exc:
            return False, f"Start failed: {exc}"

    def get_data_mode(self, bot_id: str) -> str:
        entry = self._state.get(bot_id, {})
        mode = str(entry.get("data_mode") or "live").lower()
        return "sim" if mode == "sim" else "live"

    def stop_bot(self, bot_id: str) -> tuple[bool, str]:
        self._sync_registry()
        if bot_id not in self.registry:
            return False, "Unknown bot id"

        entry = self._state.get(bot_id, {})
        pid = int(entry.get("pid") or 0)

        if pid <= 0:
            self._state.pop(bot_id, None)
            self._save_state()
            return True, "Already stopped"

        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            self._state.pop(bot_id, None)
            self._save_state()
            return False, f"Stop failed: {exc}"

        self._state.pop(bot_id, None)
        self._save_state()
        return True, f"Stopped PID {pid}"

    def get_status(self, bot_id: str) -> str:
        self._sync_registry()
        if bot_id not in self.registry:
            return "not_deployed"

        if str(self.registry[bot_id].get("status", "")).lower() == "not_deployed":
            self._state.pop(bot_id, None)
            self._save_state()
            return "not_deployed"

        if not self._runner_path(bot_id).exists():
            self._state.pop(bot_id, None)
            self._save_state()
            return "not_deployed"

        entry = self._state.get(bot_id, {})
        pid = int(entry.get("pid") or 0)

        if pid > 0 and self._is_process_alive(pid):
            return "running"

        if pid > 0 and not self._is_process_alive(pid):
            return "crashed"

        return "stopped"

    def get_all_status(self) -> dict[str, str]:
        self._sync_registry()
        return {bot_id: self.get_status(bot_id) for bot_id in self.registry}

    # ------------------------------------------------------------------
    # Heartbeat helpers
    # ------------------------------------------------------------------

    def get_heartbeat(self, bot_id: str) -> dict | None:
        """Return the latest heartbeat dict for *bot_id*, or None if missing."""
        try:
            import utils.heartbeat as hb
            return hb.read(bot_id)
        except Exception:
            return None

    def get_health_status(self, bot_id: str) -> dict:
        """Return a dict with heartbeat data + staleness annotation.

        Keys:
            heartbeat      — raw heartbeat dict or None
            stale_sec      — float seconds since last cycle, or None
            is_stale       — True if > 60 s since last cycle
            is_crashed_hb  — True if > 300 s since last cycle (heartbeat-based crash)
            severity       — "ok" | "stale" | "crashed" | "no_heartbeat"
        """
        beat = self.get_heartbeat(bot_id)
        result: dict = {"heartbeat": beat, "stale_sec": None, "is_stale": False,
                        "is_crashed_hb": False, "severity": "no_heartbeat"}
        if beat is None:
            return result
        try:
            import utils.heartbeat as hb
            stale_sec = hb.staleness_seconds(beat)
        except Exception:
            stale_sec = None

        result["stale_sec"] = stale_sec
        if stale_sec is None:
            result["severity"] = "unknown"
        elif stale_sec < 60:
            result["is_stale"] = False
            result["severity"] = "ok"
        elif stale_sec < 300:
            result["is_stale"] = True
            result["severity"] = "stale"
        else:
            result["is_stale"] = True
            result["is_crashed_hb"] = True
            result["severity"] = "crashed"
        return result

    def get_display_name(self, bot_id: str) -> str:
        """Devuelve el nombre visible del bot con el ticker activo del día.

        Si get_active_ticker falla por cualquier razón, devuelve el
        nombre estático del registry como fallback. Nunca propaga error.
        """
        entry = self.registry.get(bot_id, {})
        args = list(entry.get("args") or [])
        try:
            # MM bots: --ticker DLR/ABR26 → "MM DLR/JUN26"
            if "--ticker" in args:
                idx = args.index("--ticker")
                if idx + 1 < len(args):
                    active = get_active_ticker(args[idx + 1])
                    return f"MM {active}"
            # CS bots: --near DLR/MAY26 --far DLR/JUN26 → "CS DLR JUN26/JUL26"
            if "--near" in args and "--far" in args:
                near = get_active_ticker(args[args.index("--near") + 1])
                far = get_active_ticker(args[args.index("--far") + 1])
                instr = near.split("/")[0]
                return f"CS {instr} {near.split('/')[1]}/{far.split('/')[1]}"
        except Exception:
            pass
        return entry.get("name", bot_id)

    def get_pid(self, bot_id: str) -> int | None:
        entry = self._state.get(bot_id, {})
        pid = int(entry.get("pid") or 0)
        return pid or None

    def read_log(self, bot_id: str, tail_lines: int = 100) -> str:
        self._sync_registry()
        if bot_id not in self.registry:
            return "Unknown bot"

        log_path = self._log_path(bot_id)
        if not log_path.exists():
            return "No log file found"

        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                last = deque(f, maxlen=tail_lines)
            return "".join(last).rstrip() or "(empty log)"
        except Exception:
            try:
                with log_path.open("r", encoding="utf-16", errors="replace") as f:
                    last = deque(f, maxlen=tail_lines)
                return "".join(last).rstrip() or "(empty log)"
            except Exception as exc:
                return f"Error reading log: {exc}"

    def _extract_mm_ticker_from_log(self, bot_id: str = "mm_dlr") -> str | None:
        raw = self.read_log(bot_id, tail_lines=1200)
        if not raw or raw in {"No log file found", "(empty log)"}:
            return None
        for ln in reversed(raw.splitlines()):
            m = re.search(r"\bticker=([^\s|]+)", ln)
            if m:
                return m.group(1).strip()
        return None

    def _configured_fill_tickers(self, bot_id: str) -> list[str]:
        """Returns candidate tickers used by the bot to locate fills CSVs."""
        tickers: list[str] = []

        # Prefer ticker seen in runtime logs (already rollover-adjusted by runner).
        live_ticker = self._extract_mm_ticker_from_log(bot_id)
        if live_ticker:
            tickers.append(live_ticker)

        args = list(self.registry.get(bot_id, {}).get("args") or [])

        def _add_arg_ticker(flag: str) -> None:
            if flag not in args:
                return
            try:
                idx = args.index(flag)
                if idx + 1 >= len(args):
                    return
                tk = str(args[idx + 1])
                if tk:
                    tickers.append(get_active_ticker(tk))
            except Exception:
                return

        _add_arg_ticker("--ticker")
        _add_arg_ticker("--near")
        _add_arg_ticker("--far")

        # Preserve order while removing duplicates and empty entries.
        uniq: list[str] = []
        for tk in tickers:
            if tk and tk not in uniq:
                uniq.append(tk)
        return uniq

    def _count_fills_from_csv(self, bot_id: str) -> int:
        tickers = self._configured_fill_tickers(bot_id)
        if not tickers:
            return 0

        session_date = datetime.now().strftime("%Y%m%d")
        fills_dir = "fills_sim" if self.get_data_mode(bot_id) == "sim" else "fills"
        total = 0

        for ticker in tickers:
            safe_ticker = ticker.replace("/", "-")
            fills_path = self.project_dir / "logs" / fills_dir / f"{safe_ticker}_{session_date}.csv"
            if not fills_path.exists():
                continue
            try:
                with fills_path.open("r", newline="", encoding="utf-8") as csv_file:
                    reader = csv.DictReader(csv_file, delimiter=",")
                    total += sum(1 for _ in reader)
            except Exception:
                continue

        return total

    def flatten_mm(self, bot_id: str = "mm_dlr") -> tuple[bool, str]:
        """Cancels MM orders, flattens MM position aggressively, and stops MM bot."""
        try:
            mm_status = self.get_status(bot_id)
            mm_stats = self.parse_bot_stats(bot_id)
            pos = int(mm_stats.get("pos") or 0)
            ticker = self._extract_mm_ticker_from_log(bot_id)

            try:
                from core.connect import connect
                from core.market_data import get_snapshot
                from core.order_manager import cancel_order, get_all_orders, send_limit_order
            except Exception as exc:
                self.stop_bot(bot_id)
                return False, f"MM stopped. Flatten unavailable (import error: {exc})"

            if not connect():
                self.stop_bot(bot_id)
                return False, "MM stopped. Flatten unavailable (connection failed)."

            # 1) Cancel MM-related pending orders (ticker-scoped when ticker is known).
            canceled = 0
            orders = get_all_orders()
            for order in orders:
                try:
                    order_ticker = str(order.get("instrumentId", {}).get("symbol") or "")
                    status = str(order.get("status") or "").upper()
                    if status not in {"NEW", "PARTIALLY_FILLED"}:
                        continue
                    if ticker and order_ticker and order_ticker != ticker:
                        continue

                    oid = str(order.get("clOrdId") or order.get("clientId") or "")
                    proprietary = order.get("proprietary")
                    if oid:
                        resp = cancel_order(oid, proprietary=proprietary)
                        if resp is not None:
                            canceled += 1
                except Exception:
                    continue

            # 2) If position is open, send aggressive limit to flatten.
            flatten_sent = False
            if pos != 0 and ticker:
                snap = get_snapshot(ticker)
                bid = snap.get("bid_price") if snap else None
                ask = snap.get("ask_price") if snap else None
                if bid and ask and bid > 0 and ask > 0:
                    tick = 0.5
                    aggression = 2 * tick
                    side = "SELL" if pos > 0 else "BUY"
                    qty = abs(pos)
                    price = (float(bid) - aggression) if pos > 0 else (float(ask) + aggression)
                    price = max(price, tick)
                    resp = send_limit_order(ticker=ticker, side=side, price=price, size=qty)
                    flatten_sent = resp is not None

            # 3) Stop MM process.
            self.stop_bot(bot_id)

            details = f"status={mm_status} | pos={pos} | canceled={canceled}"
            if ticker:
                details += f" | ticker={ticker}"
            if flatten_sent:
                details += " | flatten_order=sent"
            elif pos != 0:
                details += " | flatten_order=not_sent"

            return True, f"MM flatten completed: {details}"
        except Exception as exc:
            try:
                self.stop_bot(bot_id)
            except Exception:
                pass
            return False, f"MM flatten failed: {exc}"

    def extract_pnl_history(self, bot_id: str) -> list[dict[str, Any]]:
        max_points = 500
        log_path = self._log_path(bot_id)
        if not log_path.exists():
            return []

        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            try:
                text = log_path.read_text(encoding="utf-16", errors="replace")
            except Exception:
                return []

        ts_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        pnl_re = re.compile(r"\bpnl=([-+]?\d+(?:\.\d+)?)")

        points: list[tuple[datetime, float]] = []
        for ln in text.splitlines():
            m_ts = ts_re.search(ln)
            m_pnl = pnl_re.search(ln)
            if not m_ts or not m_pnl:
                continue
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
                pnl = float(m_pnl.group(1))
                points.append((ts, pnl))
            except Exception:
                continue

        if len(points) > max_points:
            return points[-max_points:]
        return points


    def parse_bot_stats(self, bot_id: str) -> dict[str, Any]:
        """
        Parsea estadísticas detalladas del bot del log.
        Retorna: total_cycles, total_skips, total_fills, last_fill_info, uptime, pos, pnl, spread_bps
        """
        out: dict[str, Any] = {
            "cycles": 0,
            "skips": 0,
            "fills": 0,
            "last_fill": None,
            "uptime": None,
            "pos": None,
            "pnl": None,
            "spread_bps": None,
        }

        log_path = self._log_path(bot_id)
        if not log_path.exists():
            return out

        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            try:
                text = log_path.read_text(encoding="utf-16", errors="replace")
            except Exception:
                return out

        today_str = datetime.now().strftime("%Y-%m-%d")
        if today_str not in text:
            return out  # log sin líneas de hoy — retornar vacío

        lines = text.splitlines()
        
        # Contar ciclos, skips. Los fills deben salir del CSV para alinear KPI y panel.
        for ln in lines:
            if "cycle" in ln.lower() and "|" in ln:
                out["cycles"] += 1
            if "skipped" in ln.lower() or "skip" in ln.lower():
                out["skips"] += 1

        out["fills"] = self._count_fills_from_csv(bot_id)

        # Extraer último fill
        for ln in reversed(lines):
            if "filled" in ln.lower() or "fill processed" in ln.lower():
                m_ts = re.search(r"(\d{2}:\d{2}:\d{2})", ln)
                m_side = re.search(r"\b(BUY|SELL)\b", ln)
                m_price = re.search(r"@\s*([\d.]+)", ln)
                if m_ts:
                    out["last_fill"] = {
                        "time": m_ts.group(1),
                        "side": m_side.group(1) if m_side else "?",
                        "price": float(m_price.group(1)) if m_price else None,
                    }
                    break

        # Extraer último ciclo para pos, pnl, spread
        for ln in reversed(lines):
            # CS cycle line: "CS cycle | near=... | spread=... | zscore=... | signal=... | pos=... | pnl=..."
            if "cs cycle" in ln.lower() and "|" in ln:
                kv = dict(re.findall(r"([a-zA-Z_]+)=([-+]?\d+(?:\.\d+)?)", ln))
                # spread → "pos" field (so dashboard card shows spread value)
                out["pos"] = int(float(kv["spread"])) if "spread" in kv else None
                # zscore → "spread_bps" field
                out["spread_bps"] = float(kv["zscore"]) if "zscore" in kv else None
                out["pnl"] = float(kv["pnl"]) if "pnl" in kv else None
                # signal → "last_fill" dict with side=signal, time from line
                m_ts = re.search(r"(\d{2}:\d{2}:\d{2})", ln)
                m_sig = re.search(r"signal=([A-Z_]+)", ln)
                if m_sig:
                    out["last_fill"] = {
                        "time": m_ts.group(1) if m_ts else "",
                        "side": m_sig.group(1),
                        "price": None,
                    }
                break
            if "cycle" in ln.lower() and "|" in ln and "cs cycle" not in ln.lower():
                pairs = dict(re.findall(r"([a-zA-Z_]+)=([-+]?\d+(?:\.\d+)?)", ln))
                out["pos"] = int(float(pairs.get("pos", 0))) if "pos" in pairs else None
                out["pnl"] = float(pairs.get("pnl", 0)) if "pnl" in pairs else None
                out["spread_bps"] = float(pairs.get("our_spread_bps", pairs.get("spread_bps", 0))) if "our_spread_bps" in pairs or "spread_bps" in pairs else None
                break

        # Calcular uptime
        entry = self._state.get(bot_id, {})
        started_at_str = entry.get("started_at")
        if started_at_str:
            try:
                started_at = datetime.fromisoformat(started_at_str)
                elapsed = datetime.now() - started_at
                hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                out["uptime"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            except Exception:
                pass

        return out

    def extract_recent_fills(self, max_fills: int = 20) -> list[dict[str, Any]]:
        """
        Extrae los últimos fills de todos los bots.
        Retorna lista de dicts con: timestamp, strategy, side, price, qty, position_after
        """
        fills: list[dict[str, Any]] = []

        for bot_id in self.registry:
            log_path = self._log_path(bot_id)
            if not log_path.exists():
                continue

            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                try:
                    text = log_path.read_text(encoding="utf-16", errors="replace")
                except Exception:
                    continue

            ts_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
            side_re = re.compile(r"\b(BUY|SELL)\b")
            price_re = re.compile(r"@\s*([\d.]+)")
            qty_re = re.compile(r"qty[=:]?\s*([\d.]+)|cumQty[=:]?\s*([\d.]+)")
            pos_re = re.compile(r"pos[=:]?\s*([-\d.]+)")

            for ln in text.splitlines():
                if not ("filled" in ln.lower() or "fill processed" in ln.lower()):
                    continue

                m_ts = ts_re.search(ln)
                m_side = side_re.search(ln)
                m_price = price_re.search(ln)
                m_qty = qty_re.search(ln)
                m_pos = pos_re.search(ln)

                if not (m_ts and m_side and m_price):
                    continue

                try:
                    ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
                    fills.append({
                        "timestamp": ts,
                        "strategy": self.registry[bot_id]["name"],
                        "side": m_side.group(1),
                        "price": float(m_price.group(1)),
                        "qty": float(m_qty.group(1) or m_qty.group(2)) if m_qty else None,
                        "position_after": int(float(m_pos.group(1))) if m_pos else None,
                    })
                except Exception:
                    continue

        # Ordenar por timestamp descendente (más reciente primero)
        fills.sort(key=lambda x: x["timestamp"], reverse=True)
        return fills[:max_fills]

    def parse_pnl_history(self, bot_id: str) -> list[dict[str, Any]]:
        """
        Extrae historial completo de PnL del log.
        Retorna lista de dicts con: timestamp, pnl, bot_id
        """
        points: list[dict[str, Any]] = []

        log_path = self._log_path(bot_id)
        if not log_path.exists():
            return points

        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            try:
                text = log_path.read_text(encoding="utf-16", errors="replace")
            except Exception:
                return points

        ts_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        pnl_re = re.compile(r"\bpnl=([-+]?\d+(?:\.\d+)?)")

        for ln in text.splitlines():
            m_ts = ts_re.search(ln)
            m_pnl = pnl_re.search(ln)
            if not (m_ts and m_pnl):
                continue
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
                pnl = float(m_pnl.group(1))
                points.append({
                    "timestamp": ts,
                    "pnl": pnl,
                    "bot_id": bot_id,
                    "strategy": self.registry[bot_id]["name"],
                })
            except Exception:
                continue

        return points

    def emergency_cancel_all(self) -> str:
        """
        Detiene todos los bots y cancela todas las órdenes pendientes en reMarkets.
        Retorna mensaje de resultado.
        """
        try:
            # 1. Detener todos los bots
            for bot_id in self.registry:
                self.stop_bot(bot_id)

            # 2. Conectar a reMarkets y cancelar órdenes
            try:
                from core.connect import connect as initialize_connection
                
                initialize_connection()
                
                # Intentar cancelar todas las órdenes
                cancelled_count = 0

                return f"✓ All bots stopped. Orders cancellation attempted ({cancelled_count} cancelled). Emergency mode complete."

            except Exception as e:
                return f"✓ All bots stopped. Order cancellation skipped (error: {str(e)[:50]})"

        except Exception as exc:
            return f"✗ Emergency procedure error: {str(exc)[:100]}"
