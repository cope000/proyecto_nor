"""
NOR Trading System Dashboard v3 - Redesigned Layout
Streamlit app with improved visual hierarchy and UX
"""

import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from datetime import date, datetime, timedelta
import json
from typing import Any

from dashboard.bot_manager import BotManager, BOT_REGISTRY
from utils.ticker_roller import days_to_expiry, get_active_ticker

_BOTS_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "bots_state.json"

# ============================================================================
# CONFIG & STYLING
# ============================================================================

st.set_page_config(
    page_title="NOR Dashboard v3",
    page_icon="N",
    layout="wide",
    initial_sidebar_state="expanded",
)

DARK_CSS = """
.stApp { background-color: #0d1117; }
section[data-testid="stSidebar"] {
    background-color: #0f1b2d;
    border-right: 1px solid #21262d;
}
div[data-testid="metric-container"] {
    background-color: #161b22;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 12px 16px;
}
h1, h2, h3 { color: #58a6ff !important; }
p, span, div { color: #e6edf3; }
"""

LIGHT_CSS = """
.stApp { background-color: #f6f8fa; }
section[data-testid="stSidebar"] {
    background-color: #ffffff;
    border-right: 1px solid #d0d7de;
}
div[data-testid="metric-container"] {
    background-color: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    padding: 12px 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
h1, h2, h3 { color: #0969da !important; }
p, span, div { color: #24292f; }
.stButton > button {
    background: #ffffff;
    border: 1px solid #d0d7de;
    color: #24292f;
    border-radius: 6px;
}
.stButton > button:hover {
    background: #f3f4f6;
    border-color: #0969da;
}
div[data-testid="stDataFrame"] {
    border: 1px solid #d0d7de;
    border-radius: 8px;
}
"""

# ============================================================================
# SINGLETON BOT MANAGER
# ============================================================================

@st.cache_resource
def get_bot_manager():
    return BotManager(project_dir=Path(__file__).resolve().parent.parent)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def status_emoji(status: str) -> str:
    emojis = {
        "running": "RUNNING",
        "stopped": "STOPPED",
        "crashed": "CRASHED",
        "not_deployed": "NOT_DEPLOYED",
    }
    return emojis.get(status, "UNKNOWN")

def format_pnl(pnl: float | None) -> str:
    if pnl is None:
        return "N/A"
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{pnl:.2f}"

def get_mm_mode() -> str:
    mode = str(st.session_state.get("mm_data_mode", "LIVE")).strip().upper()
    return "sim" if mode == "SIM" else "live"

def start_bot_compat(mgr: BotManager, bot_id: str, mode: str = "live") -> tuple[bool, str]:
    """Starts bots with backward compatibility."""
    if not bot_id.startswith("mm_"):
        return mgr.start_bot(bot_id)
    try:
        return mgr.start_bot(bot_id, data_mode=mode)
    except TypeError:
        try:
            return mgr.start_bot(bot_id, mode)
        except TypeError:
            return mgr.start_bot(bot_id)

def get_mm_mode_compat(mgr: BotManager, bot_id: str = "mm_dlr") -> str:
    try:
        return str(mgr.get_data_mode(bot_id)).upper()
    except Exception:
        return get_mm_mode().upper()


def _status_badge_html(status: str, theme: str) -> str:
    s = str(status).lower()
    th = str(theme).lower()
    if th == "light":
        if s == "running":
            return '<span style="background:#dafbe1;color:#1a7f37;border:1px solid #1f883d;border-radius:12px;padding:2px 10px;font-size:11px">RUNNING</span>'
        if s == "crashed":
            return '<span style="background:#ffebe9;color:#cf222e;border:1px solid #cf222e;border-radius:12px;padding:2px 10px;font-size:11px">CRASHED</span>'
        if s == "stopped":
            return '<span style="background:#f6f8fa;color:#57606a;border:1px solid #d0d7de;border-radius:12px;padding:2px 10px;font-size:11px">STOPPED</span>'
        return '<span style="background:#f6f8fa;color:#57606a;border:1px solid #d0d7de;border-radius:12px;padding:2px 10px;font-size:11px">UNKNOWN</span>'

    if s == "running":
        return '<span style="background:#1a4731;color:#3fb950;border:1px solid #2ea043;border-radius:12px;padding:2px 10px;font-size:11px">RUNNING</span>'
    if s == "crashed":
        return '<span style="background:#4d1919;color:#f85149;border:1px solid #b91c1c;border-radius:12px;padding:2px 10px;font-size:11px">CRASHED</span>'
    if s == "stopped":
        return '<span style="background:#21262d;color:#8b949e;border:1px solid #6e7681;border-radius:12px;padding:2px 10px;font-size:11px">STOPPED</span>'
    return '<span style="background:#161b22;color:#8b949e;border:1px solid #30363d;border-radius:12px;padding:2px 10px;font-size:11px">UNKNOWN</span>'


def _stale_badge_html(stale_sec: float, theme: str) -> str:
    """Red badge shown when heartbeat is stale. stale_sec must be >= 60."""
    age = int(stale_sec)
    label = f"STALE — última actividad hace {age}s"
    th = str(theme).lower()
    if th == "light":
        return (
            f'<div style="background:#fff3cd;color:#856404;border:2px solid #ffc107;'
            f'border-radius:8px;padding:6px 12px;font-size:13px;font-weight:bold;'
            f'margin:4px 0">{label}</div>'
        )
    return (
        f'<div style="background:#4d3800;color:#f0a30a;border:2px solid #ffc107;'
        f'border-radius:8px;padding:6px 12px;font-size:13px;font-weight:bold;'
        f'margin:4px 0">{label}</div>'
    )


def _crashed_hb_banner_html(stale_sec: float, theme: str) -> str:
    """Unmissable red banner when heartbeat is > 5 min stale."""
    mins = int(stale_sec // 60)
    label = f"⚠ BOT NO RESPONDE — sin actividad hace {mins}min — posible CRASH"
    th = str(theme).lower()
    if th == "light":
        return (
            f'<div style="background:#ffebe9;color:#cf222e;border:3px solid #cf222e;'
            f'border-radius:8px;padding:8px 14px;font-size:14px;font-weight:bold;'
            f'margin:4px 0;text-align:center">{label}</div>'
        )
    return (
        f'<div style="background:#4d1919;color:#f85149;border:3px solid #b91c1c;'
        f'border-radius:8px;padding:8px 14px;font-size:14px;font-weight:bold;'
        f'margin:4px 0;text-align:center">{label}</div>'
    )


def _adverse_badge_html(active: bool, theme: str) -> str:
    th = str(theme).lower()
    if th == "light":
        if active:
            return '<span style="background:#ffebe9;color:#cf222e;border:1px solid #cf222e;border-radius:12px;padding:2px 10px;font-size:11px">ADVERSE ACTIVE</span>'
        return '<span style="background:#f6f8fa;color:#57606a;border:1px solid #d0d7de;border-radius:12px;padding:2px 10px;font-size:11px">Normal</span>'

    if active:
        return '<span style="background:#4d1919;color:#f85149;border:1px solid #b91c1c;border-radius:12px;padding:2px 10px;font-size:11px">ADVERSE ACTIVE</span>'
    return '<span style="background:#21262d;color:#8b949e;border:1px solid #6e7681;border-radius:12px;padding:2px 10px;font-size:11px">Normal</span>'


def _rollover_badge_html(theme: str) -> str:
    """Orange badge indicating contract rollover is needed or imminent."""
    th = str(theme).lower()
    if th == "light":
        return '<span style="background:#fff8c5;color:#9a6700;border:1px solid #d4a72c;border-radius:12px;padding:2px 10px;font-size:11px">⚠ ROLLOVER</span>'
    return '<span style="background:#3d2c00;color:#f0c040;border:1px solid #d4a72c;border-radius:12px;padding:2px 10px;font-size:11px">⚠ ROLLOVER</span>'


def _apply_rollover(bot_id: str, new_ticker: str, mgr: BotManager) -> None:
    """Actualiza el ticker en el registry y reinicia el bot si estaba corriendo."""
    if bot_id not in BOT_REGISTRY:
        return
    args = list(BOT_REGISTRY[bot_id].get("args") or [])
    if "--ticker" in args:
        idx = args.index("--ticker")
        if idx + 1 < len(args):
            args[idx + 1] = new_ticker
            BOT_REGISTRY[bot_id]["args"] = args
    status = mgr.get_status(bot_id)
    if status == "running":
        mgr.stop_bot(bot_id)
        mgr.start_bot(bot_id)


def render_bot_card(
    bot_id: str,
    info: dict[str, Any],
    status: str,
    stats: dict[str, Any],
    mgr: BotManager,
    theme: str,
) -> None:
    """Render one bot card with a consistent 4-row structure."""
    with st.container(border=True):
        # Row 1: Header
        hcol1, hcol2 = st.columns([3, 1])
        with hcol1:
            st.markdown(f"**{mgr.get_display_name(bot_id)}**")
        with hcol2:
            st.markdown(_status_badge_html(status, theme), unsafe_allow_html=True)

        # Heartbeat staleness badge (injected after status badge)
        if status == "running":
            try:
                hs = mgr.get_health_status(bot_id)
                stale_sec = hs.get("stale_sec")
                if stale_sec is not None and stale_sec >= 60:
                    if stale_sec >= 300:
                        st.markdown(_crashed_hb_banner_html(stale_sec, theme), unsafe_allow_html=True)
                    else:
                        st.markdown(_stale_badge_html(stale_sec, theme), unsafe_allow_html=True)
            except Exception:
                pass

        # Rollover badge if ticker is near expiry or already rolled
        _bot_args = list(info.get("args") or [])
        if "--ticker" in _bot_args:
            try:
                _ti = _bot_args.index("--ticker")
                if _ti + 1 < len(_bot_args):
                    _bot_ticker = str(_bot_args[_ti + 1])
                    _needs_roll = (
                        get_active_ticker(_bot_ticker) != _bot_ticker
                        or days_to_expiry(_bot_ticker) <= (1 if "SOJ" in _bot_ticker.upper() else 3)
                    )
                    if _needs_roll:
                        st.markdown(_rollover_badge_html(theme), unsafe_allow_html=True)
            except Exception:
                pass

        # Row 2: Metrics
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("Position", f"{stats.get('pos', 'N/A') if stats.get('pos') is not None else 'N/A'}")
        with m2:
            st.metric("PnL", format_pnl(stats.get("pnl")))
        with m3:
            st.metric("Fills", f"{stats.get('fills', 0)}")
        with m4:
            spread_val = stats.get("spread_bps")
            st.metric("Spread", f"{float(spread_val):.2f}bps" if spread_val is not None else "N/A")

        # Row 2b: Adverse detection badge from recent logs
        log_tail = mgr.read_log(bot_id, tail_lines=100)
        adverse_active = (
            "Adverse selection active" in (log_tail or "")
            or "adverse_active" in (log_tail or "")
        )
        st.markdown(_adverse_badge_html(adverse_active, theme), unsafe_allow_html=True)

        # Row 3: Error (only if crashed)
        if status == "crashed":
            error_line = ""
            bot_logs = mgr.get_recent_logs(bot_id, n=5) if hasattr(mgr, "get_recent_logs") else []
            if bot_logs:
                error_line = bot_logs[-1] if isinstance(bot_logs[-1], str) else str(bot_logs[-1])
            else:
                error_line = stats.get("last_error", "") or stats.get("error", "") or ""
            if error_line:
                st.markdown(
                    f"<span style='color:#f85149;font-size:11px;'>Error: {str(error_line)[:80]}</span>",
                    unsafe_allow_html=True,
                )

        # Row 4: Timestamp + Actions
        r1, r2, r3 = st.columns([2, 1, 1])
        with r1:
            upd = str(stats.get("uptime") or datetime.now().strftime("%H:%M:%S"))
            st.markdown(f"<span style='color:#8b949e;font-size:11px;'>Upd: {upd}</span>", unsafe_allow_html=True)

        with r2:
            if status == "crashed":
                if st.button("Restart", key=f"restart_{bot_id}", type="primary", use_container_width=True):
                    mgr.stop_bot(bot_id)
                    start_bot_compat(mgr, bot_id, mode="live")
                    st.toast(f"✓ Bot {bot_id} restarting...")
            elif status in ("stopped", "unknown", "not_deployed"):
                if st.button("Start", key=f"start_{bot_id}", type="primary", use_container_width=True):
                    start_bot_compat(mgr, bot_id, mode="live")
                    st.toast(f"✓ Bot {bot_id} starting...")
            else:
                if st.button("Stop", key=f"stop_{bot_id}", use_container_width=True):
                    mgr.stop_bot(bot_id)
                    st.toast(f"✓ Bot {bot_id} stopping...")

        with r3:
            if status in ("stopped", "crashed", "unknown"):
                if st.button("SIM", key=f"sim_{bot_id}", help="Iniciar en modo simulacion", use_container_width=True):
                    try:
                        result = mgr.start_bot(bot_id, mode="sim")
                    except TypeError:
                        result = mgr.start_bot(bot_id, data_mode="sim")
                    st.toast(f"✓ Bot {bot_id} starting in SIM mode...")

def _load_runtime_bot_state(bot_id: str) -> dict[str, Any]:
    """Loads shared runtime state merged by runners."""
    try:
        if not _BOTS_STATE_FILE.exists():
            return {}
        raw = _BOTS_STATE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        entry = data.get(bot_id, {})
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}

def _extract_bot_ticker(bot_id: str, bot_info: dict[str, Any], bot_state: dict[str, Any]) -> str:
    """Best-effort ticker extraction."""
    for key in ("ticker", "symbol", "instrument", "instrument_symbol"):
        value = bot_state.get(key)
        if value:
            return str(value)
    args = bot_info.get("args") or []
    if "--ticker" in args:
        idx = args.index("--ticker")
        if idx + 1 < len(args):
            return str(args[idx + 1])
    return ""

def _compute_session_pnl_from_df(df: pd.DataFrame, current_mid: float) -> float:
    """Computes mark-to-mid PnL from fills DataFrame."""
    if df.empty:
        return 0.0
    side = df["side"].astype(str).str.upper()
    qty = pd.to_numeric(df["qty"], errors="coerce").fillna(0.0)
    px = pd.to_numeric(df["price"], errors="coerce").fillna(0.0)
    buy_qty = qty.where(side == "BUY", 0.0)
    sell_qty = qty.where(side == "SELL", 0.0)
    buy_cash = (px * buy_qty).sum()
    sell_cash = (px * sell_qty).sum()
    position = float(buy_qty.sum() - sell_qty.sum())
    cash = float(sell_cash - buy_cash)
    return cash + position * float(current_mid)

def check_market_open() -> tuple[bool, str]:
    """Verifica si ROFEX esta abierto (10:00-15:00 ART Lunes-Viernes)."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False, "MARKET CLOSED (Weekend)"
    market_open = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    market_close = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    if now < market_open:
        delta = market_open - now
        total = int(delta.total_seconds())
        return False, f"MARKET OPENS at 10:00 ART (en {total // 3600}h {(total % 3600) // 60}m)"
    if now >= market_close:
        return False, f"MARKET CLOSED (opens 10:00 ART tomorrow)"
    return True, "Market Open"

# ============================================================================
# FRAGMENTS (auto-refresh components)
# ============================================================================

@st.fragment(run_every=timedelta(seconds=5))
def bot_cards_fragment():
    """Renders all bot cards with state and actions."""
    mgr = get_bot_manager()
    theme = str(st.session_state.get("theme", "dark")).lower()
    all_status = mgr.get_all_status()
    all_stats = {}
    
    for bot_id, status in all_status.items():
        if status in ("running", "stopped", "crashed"):
            stats = mgr.parse_bot_stats(bot_id)
            runtime_state = _load_runtime_bot_state(bot_id)
            if runtime_state:
                stats.update(runtime_state)
            all_stats[bot_id] = stats
    
    bot_ids = list(BOT_REGISTRY.keys())
    for i in range(0, len(bot_ids), 2):
        row_ids = bot_ids[i:i + 2]
        cards_cols = st.columns(2)
        for col, bot_id in zip(cards_cols, row_ids):
            try:
                info = BOT_REGISTRY[bot_id]
                status = all_status.get(bot_id, "unknown")
                stats = all_stats.get(bot_id, {})
                with col:
                    render_bot_card(bot_id, info, status, stats, mgr, theme)
            except Exception as e:
                st.warning(f"Error rendering card for {bot_id}: {str(e)[:50]}")

@st.fragment(run_every=timedelta(seconds=5))
def kpi_row_fragment():
    """Renders 5-metric KPI row."""
    mgr = get_bot_manager()
    all_status = mgr.get_all_status()
    all_stats = {}
    
    active_count = sum(1 for s in all_status.values() if s == "running")
    total_count = len([s for s in all_status.values() if s != "not_deployed"])
    
    total_pnl = 0.0
    total_pos = 0
    total_fills = 0
    
    for bot_id, status in all_status.items():
        if status in ("running", "stopped"):
            stats = mgr.parse_bot_stats(bot_id)
            runtime_state = _load_runtime_bot_state(bot_id)
            if runtime_state:
                stats.update(runtime_state)
            all_stats[bot_id] = stats
            if stats["pnl"] is not None:
                total_pnl += stats["pnl"]
            if stats["pos"] is not None:
                total_pos += stats["pos"]
            if stats["fills"] is not None:
                total_fills += stats["fills"]
    
    # Calculate drawdown
    fund_size = float(st.session_state.get("fund_size", 14_000_000))
    drawdown_pct = abs(total_pnl) / fund_size * 100 if total_pnl < 0 else 0
    pnl_border = "#3fb950" if total_pnl >= 0 else "#f85149"
    st.markdown(
        f"<style>div[data-testid='stHorizontalBlock'] > div:nth-child(2) div[data-testid='stMetric'] {{ border-bottom: 3px solid {pnl_border}; }}</style>",
        unsafe_allow_html=True,
    )
    
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.metric("Active", f"{active_count}/{total_count}")
    with col2:
        st.metric("Fund PnL", format_pnl(total_pnl), delta=format_pnl(total_pnl))
    with col3:
        st.metric("Net Pos", f"{total_pos}")
    with col4:
        st.metric("Fills Hoy", f"{total_fills}")
    with col5:
        st.metric("Drawdown", f"{drawdown_pct:.2f}%")

@st.fragment(run_every=timedelta(seconds=5))
def fills_and_skew_fragment(selected_bot_id: str):
    """Renders fills table and skew metrics for selected bot."""
    mgr = get_bot_manager()
    
    try:
        bot_info = BOT_REGISTRY.get(selected_bot_id, {})
        stats = mgr.parse_bot_stats(selected_bot_id)
        runtime_state = _load_runtime_bot_state(selected_bot_id)
        if runtime_state:
            stats.update(runtime_state)
        
        ticker = _extract_bot_ticker(selected_bot_id, bot_info, stats)
        if not ticker:
            st.info("Ticker no disponible")
            return
        
        data_mode = get_mm_mode_compat(mgr, selected_bot_id).lower()
        safe_ticker = ticker.replace("/", "-")
        session_date = date.today().strftime("%Y%m%d")
        fills_dir = "fills_sim" if data_mode == "sim" else "fills"
        fills_path = Path(__file__).resolve().parent.parent / "logs" / fills_dir / f"{safe_ticker}_{session_date}.csv"
        st.caption(f"Fuente fills: {data_mode.upper()} | Archivo: logs/{fills_dir}/{safe_ticker}_{session_date}.csv")
        
        df = pd.DataFrame(columns=["timestamp", "ticker", "side", "price", "qty", "order_id", "session_date"])
        
        try:
            if fills_path.exists():
                df = pd.read_csv(fills_path, encoding="utf-8")
        except Exception:
            st.warning("No fills data available")
            return
        
        if not df.empty:
            df["timestamp_dt"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.sort_values(by="timestamp_dt", ascending=False).reset_index(drop=True)
        
        if df.empty:
            # Fallback: intentar desde log
            log_fills = mgr.extract_recent_fills(max_fills=50)
            bot_name = BOT_REGISTRY.get(selected_bot_id, {}).get("name", "")
            log_fills_filtered = [
                f for f in log_fills
                if f.get("strategy") == bot_name
            ]
            if log_fills_filtered:
                df = pd.DataFrame(log_fills_filtered)
                df = df.rename(columns={
                    "timestamp": "Hora",
                    "side": "Lado",
                    "price": "Precio",
                    "qty": "Cantidad",
                    "position_after": "Pos After",
                })
                df["Hora"] = pd.to_datetime(df["Hora"], errors="coerce").dt.strftime("%H:%M:%S")
                st.caption("Fuente: log (CSV pendiente de fills)")
                st.dataframe(
                    df[["Hora", "Lado", "Precio", "Cantidad", "Pos After"]],
                    use_container_width=True,
                    hide_index=True,
                )
                return
            else:
                st.info("Sin fills en esta sesion")
                return
        
        # Show skew metrics if active
        if bool(stats.get("skew_active", False)):
            skew_cols = st.columns(4)
            with skew_cols[0]:
                st.markdown("<div class='skew-pill'>Skew: ON</div>", unsafe_allow_html=True)
            with skew_cols[1]:
                rp = stats.get("reservation_price")
                rp_txt = f"{float(rp):.2f}" if rp is not None else "-"
                st.markdown(
                    f"<div class='skew-res-box'><div style='font-size:11px;'>Reservation</div><div style='font-size:16px;font-weight:700'>{rp_txt}</div></div>",
                    unsafe_allow_html=True,
                )
            with skew_cols[2]:
                sg = stats.get("sigma")
                sg_txt = f"{float(sg):.4f}" if sg is not None else "-"
                st.markdown(
                    f"<div class='skew-sigma-box'><div style='font-size:11px;'>Sigma</div><div style='font-size:16px;font-weight:700'>{sg_txt}</div></div>",
                    unsafe_allow_html=True,
                )
            with skew_cols[3]:
                np_val = stats.get("net_position")
                np_txt = f"{int(np_val)}" if np_val is not None else "-"
                st.metric("Net Pos", np_txt)
            st.markdown("---")
        
        with st.expander("Ver resumen completo"):
            if not df.empty:
                side_upper = df["side"].astype(str).str.upper()
                total_buys = int((side_upper == "BUY").sum())
                total_sells = int((side_upper == "SELL").sum())
                buy_prices = pd.to_numeric(df.loc[side_upper == "BUY", "price"], errors="coerce")
                sell_prices = pd.to_numeric(df.loc[side_upper == "SELL", "price"], errors="coerce")
                avg_buy = float(buy_prices.mean()) if total_buys > 0 else 0.0
                avg_sell = float(sell_prices.mean()) if total_sells > 0 else 0.0

                cols = st.columns(4)
                cols[0].metric("Total BUY", total_buys)
                cols[1].metric("Total SELL", total_sells)
                cols[2].metric("Precio prom BUY", f"{avg_buy:.2f}" if total_buys > 0 else "-")
                cols[3].metric("Precio prom SELL", f"{avg_sell:.2f}" if total_sells > 0 else "-")

        show_df = df.copy()
        show_df["Hora"] = show_df["timestamp_dt"].dt.strftime("%H:%M:%S").fillna(show_df["timestamp"].astype(str))
        show_df["Lado"] = show_df["side"].astype(str).str.upper()
        show_df["Precio"] = pd.to_numeric(show_df["price"], errors="coerce")
        show_df["Cantidad"] = pd.to_numeric(show_df["qty"], errors="coerce")
        show_df["Order ID"] = show_df["order_id"].astype(str)

        cash_flow = []
        running = 0.0
        for _, row in show_df.iterrows():
            lado = str(row.get("Lado", "")).upper()
            px = float(row.get("Precio", 0) or 0)
            qty = float(row.get("Cantidad", 0) or 0)
            if lado == "SELL":
                running += px * qty
            elif lado == "BUY":
                running -= px * qty
            cash_flow.append(round(running, 2))
        show_df["Cash Flow"] = cash_flow

        total_buy_rows = int((show_df["Lado"] == "BUY").sum())
        total_sell_rows = int((show_df["Lado"] == "SELL").sum())
        st.caption(f"Mostrando {len(show_df)} fills - {total_buy_rows} compras / {total_sell_rows} ventas")

        show_df = show_df[["Hora", "Lado", "Precio", "Cantidad", "Cash Flow", "Order ID"]]
        
        def _style_side_row(row: pd.Series) -> list[str]:
            base_bg = "background-color: #1a1f27" if (row.name % 2 == 0) else "background-color: #161b22"
            side_val = str(row.get("Lado", "")).upper()
            styles = [base_bg] * len(row)
            if side_val == "BUY":
                styles[1] = base_bg + "; color: #3fb950; font-weight: 700"
            elif side_val == "SELL":
                styles[1] = base_bg + "; color: #f85149; font-weight: 700"
            return styles
        
        styled = show_df.style.apply(_style_side_row, axis=1).set_table_styles([
            {"selector": "th", "props": [("color", "#8b949e"), ("background-color", "#0d1117"), ("border-color", "#21262d")]},
            {"selector": "td", "props": [("border-color", "#21262d")]},
        ])
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            height=min(400, max(150, len(show_df) * 35 + 38)),
        )

        if not df.empty:
            csv_bytes = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="Descargar fills CSV",
                data=csv_bytes,
                file_name=f"fills_{safe_ticker}_{session_date}.csv",
                mime="text/csv",
                key=f"download_{selected_bot_id}_{fills_dir}_{safe_ticker}_{session_date}",
            )
        
    except Exception as exc:
        st.error(f"Error loading fills: {exc}")

@st.fragment(run_every=timedelta(seconds=8))
def risk_monitor_fragment(bot_id: str = "mm_dlr"):
    """Renders risk monitoring panel for the selected bot."""
    mgr = get_bot_manager()

    try:
        stats = mgr.parse_bot_stats(bot_id)
        bot_pnl = float(stats.get("pnl") or 0.0)
        bot_pos = stats.get("pos")
        bot_name = mgr.get_display_name(bot_id)
        status = mgr.get_status(bot_id)

        # Calculate drawdown for this bot
        fund_size = float(st.session_state.get("fund_size", 14_000_000))
        drawdown_pct = abs(bot_pnl) / fund_size * 100 if bot_pnl < 0 else 0

        # Risk level
        if drawdown_pct < 5:
            risk_color = "#00ff00"
            risk_level = "LOW"
        elif drawdown_pct <= 15:
            risk_color = "#ffaa00"
            risk_level = "WARN"
        else:
            risk_color = "#ff4444"
            risk_level = "CRITICAL"

        st.caption(f"{bot_name} — {status.upper()}")
        st.markdown(f"**Risk Level: <span style='color:{risk_color}'>{risk_level}</span>**", unsafe_allow_html=True)
        drawdown_norm = min(drawdown_pct / 50.0, 1.0) * 100.0
        st.markdown(
            f"""
            <div style='width:100%;height:10px;background:#21262d;border-radius:999px;overflow:hidden;'>
                <div style='width:{drawdown_norm:.1f}%;height:10px;background:{risk_color};'></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        pnl_sign = "+" if bot_pnl >= 0 else ""
        st.caption(f"PnL: {pnl_sign}{bot_pnl:.2f} ARS | Drawdown: {drawdown_pct:.1f}% | Pos: {bot_pos if bot_pos is not None else 'N/A'}")

        # Market status
        market_open, market_status = check_market_open()
        market_color = "#00ff00" if market_open else "#ff4444"
        st.markdown(f"**Market: <span style='color:{market_color}'>{market_status}</span>**", unsafe_allow_html=True)

        all_status = mgr.get_all_status()
        running_bots = sum(1 for s in all_status.values() if s == "running")
        if market_open and running_bots > 0:
            feed_html = "<span style='color:#3fb950'>Feed: OK</span>"
        elif running_bots > 0:
            feed_html = "<span style='color:#d29922;background:rgba(210,153,34,0.14);padding:2px 6px;border-radius:4px;'>Feed: WARN</span>"
        else:
            feed_html = "<span style='color:#f85149;background:rgba(248,81,73,0.14);padding:2px 6px;border-radius:4px;'>Feed: DOWN</span>"
        st.markdown(feed_html, unsafe_allow_html=True)

    except Exception as e:
        st.error(f"Risk monitor error: {e}")

@st.fragment(run_every=timedelta(seconds=10))
def logs_fragment():
    """Renders live log viewer with tabs per bot."""
    mgr = get_bot_manager()
    
    try:
        tab_list = list(BOT_REGISTRY.keys())
        tabs = st.tabs([mgr.get_display_name(bid) for bid in tab_list])
        
        for tab, bot_id in zip(tabs, tab_list):
            with tab:
                status = mgr.get_status(bot_id)
                log_text = mgr.read_log(bot_id, tail_lines=30)
                
                if status == "running":
                    status_badge = "<span style='color:#00ff00'>RUNNING</span>"
                elif status == "stopped":
                    status_badge = "<span style='color:#888888'>STOPPED</span>"
                else:
                    status_badge = f"<span style='color:#ff4444'>{status.upper()}</span>"
                
                st.markdown(f"**Status:** {status_badge}", unsafe_allow_html=True)
                
                if log_text not in {"No log file found", "(empty log)"}:
                    st.code(log_text, language="log")
                else:
                    st.info(log_text)
    except Exception as e:
        st.error(f"Logs error: {e}")


@st.fragment(run_every=timedelta(seconds=30))
def pnl_history_fragment():
    mgr = get_bot_manager()
    theme = str(st.session_state.get("theme", "dark")).lower()
    all_status = mgr.get_all_status()

    active_bot_ids = [
        bot_id
        for bot_id, status in all_status.items()
        if status in ("running", "stopped", "crashed")
    ]
    if not active_bot_ids:
        st.info("Sin datos de PnL hoy")
        return

    rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    today = date.today()
    now = datetime.now()

    # Rolling cache: acumula puntos (1 cada 30 s) para bots cuyo log no expone pnl=
    if "pnl_cache" not in st.session_state:
        st.session_state["pnl_cache"] = {}
    cache: dict = st.session_state["pnl_cache"]
    # Purgar entradas de días anteriores
    for bid in list(cache.keys()):
        cache[bid] = [
            p for p in cache[bid]
            if isinstance(p.get("timestamp"), datetime) and p["timestamp"].date() == today
        ]

    for bot_id in active_bot_ids:
        stats = mgr.parse_bot_stats(bot_id)
        current_pnl = float(stats.get("pnl")) if stats.get("pnl") is not None else None
        bot_name = mgr.get_display_name(bot_id)
        is_sim = mgr.get_data_mode(bot_id) == "sim"

        # Obtener historial basado en log solo para bots live
        today_points: list[dict] = []
        if not is_sim:
            all_points = mgr.parse_pnl_history(bot_id)
            today_points = [
                p for p in all_points
                if isinstance(p.get("timestamp"), datetime) and p["timestamp"].date() == today
            ]

        pnl_values = [float(p["pnl"]) for p in today_points if p.get("pnl") is not None]
        summary_rows.append({
            "Bot": bot_name,
            "PnL actual": current_pnl if current_pnl is not None else (pnl_values[-1] if pnl_values else 0.0),
            "Max PnL": max(pnl_values) if pnl_values else (current_pnl or 0.0),
            "Min PnL": min(pnl_values) if pnl_values else (current_pnl or 0.0),
            "Fills": int(stats.get("fills", 0) or 0),
        })

        # Bots SIM quedan fuera del gráfico
        if is_sim:
            continue

        if today_points:
            # Log tiene historial de hoy → usarlo directamente
            for p in today_points:
                rows.append({
                    "timestamp": p["timestamp"],
                    "pnl": p["pnl"],
                    "strategy": p.get("strategy", bot_name),
                })
        else:
            # Fallback: acumular en cache con parse_bot_stats (refresca cada 30 s)
            if current_pnl is not None:
                if bot_id not in cache:
                    cache[bot_id] = []
                cache[bot_id].append({"timestamp": now, "pnl": current_pnl, "strategy": bot_name})
            for p in cache.get(bot_id, []):
                rows.append(p)

    df = pd.DataFrame(rows)
    if df.empty:
        st.info("Sin datos de PnL hoy")
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
        df = df.dropna(subset=["timestamp", "pnl", "strategy"])
        df = df[df["timestamp"].dt.date == today]

        if df.empty:
            st.info("Sin datos de PnL hoy")
        else:
            fig = go.Figure()
            for strategy in sorted(df["strategy"].unique()):
                sub = df[df["strategy"] == strategy].sort_values("timestamp")
                fig.add_trace(
                    go.Scatter(
                        x=sub["timestamp"],
                        y=sub["pnl"],
                        mode="lines",
                        name=str(strategy),
                        line={"width": 2},
                    )
                )
            fig.update_layout(
                margin={"l": 20, "r": 20, "t": 20, "b": 20},
                height=250,
                template="plotly_dark" if theme == "dark" else "plotly_white",
                xaxis={"title": "Hora", "tickformat": "%H:%M"},
                yaxis={"title": "PnL (ARS)"},
                legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        st.dataframe(summary_df, use_container_width=True, hide_index=True)


@st.cache_resource
def _get_rofex_connection() -> bool:
    """Establece conexión REST a ROFEX para el proceso del dashboard (singleton)."""
    try:
        from core.connect import connect
        return connect()
    except Exception:
        return False


@st.fragment(run_every=timedelta(seconds=3))
def book_depth_fragment(ticker: str):
    """Renders live order book depth (multi-level) for selected ticker."""
    if not ticker:
        st.info("Seleccioná un bot para ver el libro")
        return

    try:
        connected = _get_rofex_connection()
        if not connected:
            st.warning("Sin conexión a ROFEX — el libro no está disponible")
            return

        from core.market_data import get_book_depth
        depth = get_book_depth(ticker, levels=5)

        if not depth:
            st.caption(f"Sin datos de libro para {ticker}")
            return

        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        last = depth.get("last")
        vol = depth.get("volume")

        if not bids and not asks:
            st.caption("Libro vacío")
            return

        # Max size across all levels for bar width normalisation
        max_size = max(
            [b["size"] for b in bids] + [a["size"] for a in asks] + [1]
        )

        # Header
        meta_parts = [f"`{ticker}`"]
        if last:
            meta_parts.append(f"Last: **{last:.2f}**")
        if vol is not None:
            meta_parts.append(f"Vol: {vol}")
        st.caption("  |  ".join(meta_parts))

        # Build HTML order book table
        rows_html = ""

        # ASK rows (reversed so best ask is closest to mid)
        for a in reversed(asks):
            pct = max(int(a["size"] / max_size * 100), 2)
            rows_html += (
                f"<tr>"
                f"<td style='text-align:right;color:#8b949e;padding:2px 6px'>{a['size']}</td>"
                f"<td style='text-align:right;padding:2px 6px'>"
                f"  <div style='position:relative;display:inline-block;width:100%;min-width:80px'>"
                f"    <div style='position:absolute;right:0;top:0;height:100%;"
                f"         width:{pct}%;background:rgba(248,81,73,0.18);'></div>"
                f"    <span style='position:relative;color:#f85149;font-weight:600'>{a['price']:.4g}</span>"
                f"  </div>"
                f"</td>"
                f"<td></td>"
                f"</tr>"
            )

        # Mid spread row
        if bids and asks:
            spread = asks[0]["price"] - bids[0]["price"]
            rows_html += (
                f"<tr><td colspan='3' style='text-align:center;color:#8b949e;"
                f"font-size:11px;padding:2px 0'>── spread {spread:.4g} ──</td></tr>"
            )

        # BID rows
        for b in bids:
            pct = max(int(b["size"] / max_size * 100), 2)
            rows_html += (
                f"<tr>"
                f"<td></td>"
                f"<td style='padding:2px 6px'>"
                f"  <div style='position:relative;display:inline-block;width:100%;min-width:80px'>"
                f"    <div style='position:absolute;left:0;top:0;height:100%;"
                f"         width:{pct}%;background:rgba(63,185,80,0.18);'></div>"
                f"    <span style='position:relative;color:#3fb950;font-weight:600'>{b['price']:.4g}</span>"
                f"  </div>"
                f"</td>"
                f"<td style='color:#8b949e;padding:2px 6px'>{b['size']}</td>"
                f"</tr>"
            )

        table_html = f"""
        <table style='width:100%;border-collapse:collapse;font-size:13px;font-family:monospace'>
          <thead>
            <tr>
              <th style='text-align:right;color:#8b949e;font-weight:400;padding:2px 6px'>Qty</th>
              <th style='text-align:center;color:#8b949e;font-weight:400;padding:2px 6px'>Precio</th>
              <th style='color:#8b949e;font-weight:400;padding:2px 6px'>Qty</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        """
        st.markdown(table_html, unsafe_allow_html=True)

    except Exception as exc:
        st.caption(f"Book depth error: {exc}")


@st.fragment(run_every=timedelta(seconds=3))
def order_book_fragment(bot_id: str = "mm_dlr"):
    mgr = get_bot_manager()
    log = mgr.read_log(bot_id, tail_lines=20)

    if not log or log in {"No log file found", "(empty log)"}:
        st.caption("Sin datos")
        return

    import re
    bid = ask = bid_depth = ask_depth = bid_size = ask_size = None
    for line in reversed(log.splitlines()):
        if "MM cycle" not in line:
            continue
        m_bid = re.search(r"market_bid=([\d.]+)", line)
        m_ask = re.search(r"market_ask=([\d.]+)", line)
        m_bd  = re.search(r"bid_depth=([\d.]+)", line)
        m_ad  = re.search(r"ask_depth=([\d.]+)", line)
        m_bs  = re.search(r"bid_size=([\d.]+)", line)
        m_as  = re.search(r"ask_size=([\d.]+)", line)
        if m_bid and m_ask:
            bid = float(m_bid.group(1))
            ask = float(m_ask.group(1))
            bid_depth = int(float(m_bd.group(1))) if m_bd else None
            ask_depth = int(float(m_ad.group(1))) if m_ad else None
            bid_size  = int(float(m_bs.group(1))) if m_bs else None
            ask_size  = int(float(m_as.group(1))) if m_as else None
            break

    if bid is None:
        st.caption("Sin datos de mercado")
        return

    spread = ask - bid if ask and bid else 0
    spread_bps = (spread / bid * 10000) if bid else 0

    col_b, col_s = st.columns(2)
    with col_b:
        st.markdown(
            f"<div style='text-align:center;color:#3fb950'>"
            f"<div style='font-size:11px;color:#8b949e'>BID</div>"
            f"<div style='font-size:22px;font-weight:700'>{bid:.2f}</div>"
            f"<div style='font-size:11px;color:#8b949e'>"
            f"size={bid_size or '-'} | depth={bid_depth or '-'}</div>"
            f"</div>",
            unsafe_allow_html=True
        )
    with col_s:
        st.markdown(
            f"<div style='text-align:center;color:#f85149'>"
            f"<div style='font-size:11px;color:#8b949e'>ASK</div>"
            f"<div style='font-size:22px;font-weight:700'>{ask:.2f}</div>"
            f"<div style='font-size:11px;color:#8b949e'>"
            f"size={ask_size or '-'} | depth={ask_depth or '-'}</div>"
            f"</div>",
            unsafe_allow_html=True
        )

    st.markdown(
        f"<div style='text-align:center;color:#8b949e;"
        f"font-size:11px;margin-top:4px'>"
        f"Spread: {spread:.2f} ({spread_bps:.1f}bps)</div>",
        unsafe_allow_html=True
    )


@st.fragment(run_every=timedelta(seconds=8))
def ofi_fragment(bot_id: str = "mm_dlr"):
    mgr = get_bot_manager()

    import re

    bot_name = mgr.get_display_name(bot_id)
    log = mgr.read_log(bot_id, tail_lines=50)
    ofi_val = None
    if log:
        for line in reversed(log.splitlines()):
            m = re.search(r"\bofi=([-+]?\d+(?:\.\d+)?)", line)
            if m:
                ofi_val = float(m.group(1))
                break

    st.markdown(f"**Order Flow Imbalance ({bot_name})**")

    if ofi_val is None:
        st.info("Sin datos OFI")
        return

    color = "#3fb950" if ofi_val > 0.2 else "#f85149" if ofi_val < -0.2 else "#8b949e"
    label = "BUY pressure" if ofi_val > 0.2 else "SELL pressure" if ofi_val < -0.2 else "Neutral"

    st.markdown(
        f"<div style='font-size:28px;font-weight:700;color:{color}'>{ofi_val:+.3f}</div>"
        f"<div style='color:{color};font-size:12px'>{label}</div>",
        unsafe_allow_html=True
    )

    pct = (ofi_val + 1) / 2 * 100
    st.markdown(
        f"""
        <div style='width:100%;height:8px;background:#21262d;
                    border-radius:999px;overflow:hidden;margin-top:8px'>
            <div style='width:{pct:.1f}%;height:8px;background:{color};'></div>
        </div>
        <div style='display:flex;justify-content:space-between;
                    font-size:10px;color:#8b949e;margin-top:2px'>
            <span>-1 SELL</span><span>0</span><span>+1 BUY</span>
        </div>
        """,
        unsafe_allow_html=True
    )

# ============================================================================
# SIDEBAR
# ============================================================================

def render_sidebar():
    """Renders enhanced sidebar with all controls."""
    mgr = get_bot_manager()
    
    with st.sidebar:
        st.sidebar.markdown(
            '<div style="padding:16px 0 8px 0"><span style="font-size:18px;font-weight:700;color:#58a6ff">NOR</span> <span style="font-size:18px;font-weight:300;color:#e6edf3">Trading</span><br><span style="font-size:11px;color:#8b949e">Market Making System</span></div>',
            unsafe_allow_html=True,
        )
        # Header
        st.markdown("# NOR SYSTEM v3")
        st.markdown(f"**Account:** REM21399  |  **Date:** {datetime.now().strftime('%Y-%m-%d')}")
        st.markdown("<div class='sidebar-divider'></div>", unsafe_allow_html=True)

        # Fund Info
        st.markdown("<div class='sidebar-section-title'>-- Fund Info --</div>", unsafe_allow_html=True)
        fund_size = st.number_input("Fund Size (ARS)", value=14_000_000, step=500_000, key="fund_size")
        
        market_open, market_status = check_market_open()
        market_color = "green" if market_open else "red"
        st.write(f"Market: {market_status}")
        
        all_status = mgr.get_all_status()
        running = sum(1 for s in all_status.values() if s == "running")
        total = len([s for s in all_status.values() if s != "not_deployed"])
        st.write(f"Bots: {running}/{total} running")
        
        mode = get_mm_mode_compat(mgr, "mm_dlr") if mgr.get_status("mm_dlr") == "running" else get_mm_mode().upper()
        st.write(f"Mode: {mode}")

        st.markdown("<div class='sidebar-divider'></div>", unsafe_allow_html=True)
        
        # Risk Settings
        st.markdown("<div class='sidebar-section-title'>-- Risk --</div>", unsafe_allow_html=True)
        gamma_display = st.number_input("Gamma (inventory skew)", value=0.05, step=0.01, format="%.4f")
        max_inv_display = st.number_input("Max Inventory", value=10, step=1)
        st.caption(f"Current settings: gamma={gamma_display}, max_inv={max_inv_display}")
        
        st.markdown("<div class='sidebar-divider'></div>", unsafe_allow_html=True)
        
        # Global Actions
        st.markdown("<div class='sidebar-section-title'>-- Actions --</div>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("START", use_container_width=True, key="btn_start_all", type="primary"):
                for bot_id in BOT_REGISTRY:
                    start_bot_compat(mgr, bot_id)
                st.toast("✓ Starting all bots...")
        with col2:
            if st.button("STOP", use_container_width=True, key="btn_stop_all"):
                for bot_id in BOT_REGISTRY:
                    mgr.stop_bot(bot_id)
                st.toast("✓ Stopping all bots...")
        with col3:
            if st.button("KILL", use_container_width=True, key="btn_kill_all", type="secondary"):
                result = mgr.emergency_cancel_all()
                st.toast(result)
        
        st.markdown("<div class='sidebar-divider'></div>", unsafe_allow_html=True)
        
        # Session Info
        st.markdown("<div class='sidebar-section-title'>-- Session --</div>", unsafe_allow_html=True)
        st.write(f"Started: {datetime.now().strftime('%H:%M:%S')}")
        st.caption("Version: NOR v3 | Refresh: 2s")

# ============================================================================
# MAIN APP
# ============================================================================

def main():
    mgr = get_bot_manager()

    st.markdown(f"<style>{DARK_CSS}</style>", unsafe_allow_html=True)
    
    # Render sidebar
    render_sidebar()
    
    # Main content
    st.markdown("# NOR Trading System - v3")
    
    # KPI Row
    kpi_row_fragment()
    st.markdown("---")

    # Selector del bot a monitorear — auto-selecciona el bot con posición abierta
    _all_status_top = mgr.get_all_status()
    _default_bot = list(BOT_REGISTRY.keys())[0]
    for _bid, _bstatus in _all_status_top.items():
        if _bstatus == "running":
            _bstats = mgr.parse_bot_stats(_bid)
            if _bstats.get("pos") and _bstats["pos"] != 0:
                _default_bot = _bid
                break
    _bot_keys = list(BOT_REGISTRY.keys())
    _default_idx = _bot_keys.index(_default_bot) if _default_bot in _bot_keys else 0

    monitor_bot = st.selectbox(
        "Monitor Bot",
        options=_bot_keys,
        format_func=lambda x: mgr.get_display_name(x),
        key="monitor_bot_selector",
        index=_default_idx,
    )

    # Top row: 3 columnas — Risk Monitor | Order Book | OFI
    top_a, top_b, top_c = st.columns(3)
    with top_a:
        st.subheader("Risk Monitor")
        risk_monitor_fragment(bot_id=monitor_bot)
    with top_b:
        st.subheader("Order Book")
        order_book_fragment(bot_id=monitor_bot)
    with top_c:
        st.subheader("OFI")
        ofi_fragment(bot_id=monitor_bot)
    st.markdown("---")

    st.subheader("PnL History")
    pnl_history_fragment()
    st.markdown("---")

    # Bot Cards Section
    st.subheader("Bot Control Cards")
    bot_cards_fragment()
    st.markdown("---")

    # Bottom Panel: 2 columns
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("Session Fills & Skew")
        selected_bot = st.selectbox(
            "Select Bot",
            options=list(BOT_REGISTRY.keys()),
            format_func=lambda x: mgr.get_display_name(x),
            key="fills_bot_selector"
        )
        fills_and_skew_fragment(selected_bot)

    with col_right:
        st.subheader("Live Logs")
        logs_fragment()

if __name__ == "__main__":
    main()

