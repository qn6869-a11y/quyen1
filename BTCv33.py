import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timezone, timedelta
import numpy as np
import time
import threading
import json
import calendar as _cal
from calendar import monthrange as _monthrange   # direct import — accessible in nested functions
from collections import deque
from typing import Any
import pathlib as _pathlib

# ── Auto-install streamlit-autorefresh nếu chưa có ───────────────────────────
try:
    import streamlit_autorefresh as _sar  # noqa: F401
except ImportError:
    import subprocess, sys as _sys
    subprocess.run(
        [_sys.executable, "-m", "pip", "install", "streamlit-autorefresh", "-q"],
        capture_output=True, timeout=60,
    )


# ═════════════════════════════════════════════════════════════════════════════
# STRATEGY PERSISTENCE — lưu cứng vào file JSON, bền vững qua restart/mạng
# ═════════════════════════════════════════════════════════════════════════════
_STRATEGY_CACHE_FILE  = _pathlib.Path(__file__).parent / "btc_strategy_cache.json"
_STRATEGY_CACHE_DIR   = _pathlib.Path(__file__).parent / "btc_ai_history"
_CACHE_TTL_DAYS       = 14   # Giữ dữ liệu AI tối đa 2 tuần


# ═════════════════════════════════════════════════════════════════════════════
# COUNTERFLOW NET LONG / NET SHORT — REST-based, bền vững qua restart
# Poll Binance REST mỗi 5 phút, tính ΔOI × ΔPrice, lưu file JSON local
# Không cần WebSocket, không cần Always On, không bị kill khi tắt máy
# ═════════════════════════════════════════════════════════════════════════════

_CF_CACHE_FILE   = _pathlib.Path(__file__).parent / "btc_cf_history.json"
_CF_MAX_ROWS     = 6048  # 21 ngày × 288 điểm/ngày (5 phút/lần)
_CF_POLL_SEC     = 300   # poll mỗi 5 phút
_CF_LOCK         = threading.Lock()
_CF_TABLE        = "btc_cf_history"   # tên table Supabase


# ─── Supabase helpers ────────────────────────────────────────────────────────

def _cf_get_supabase_cfg() -> tuple[str, str] | tuple[None, None]:
    """
    Đọc Supabase URL + anon key từ st.secrets hoặc env.
    Trả về (url, key) hoặc (None, None) nếu chưa config.
    """
    try:
        url = st.secrets["supabase"]["url"].strip()
        key = st.secrets["supabase"]["key"].strip()
        if url and key:
            return url, key
    except Exception:
        pass
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if url and key:
        return url, key
    return None, None


def _cf_supabase_available() -> bool:
    """Kiểm tra Supabase đã được config chưa."""
    u, k = _cf_get_supabase_cfg()
    return bool(u and k)


def _cf_load_supabase() -> list:
    """
    Đọc 21 ngày gần nhất từ Supabase table btc_cf_history.
    Dùng REST API trực tiếp — không cần cài supabase-py.
    """
    url, key = _cf_get_supabase_cfg()
    if not url or not key:
        return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
        endpoint = (
            f"{url}/rest/v1/{_CF_TABLE}"
            f"?ts=gte.{cutoff}"
            f"&order=ts.asc"
            f"&limit={_CF_MAX_ROWS}"
        )
        r = requests.get(
            endpoint,
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json()
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _cf_save_supabase(row: dict):
    """
    Upsert 1 snapshot mới vào Supabase (theo ts làm key).
    Ghi từng row — không ghi cả batch để tránh conflict.
    """
    url, key = _cf_get_supabase_cfg()
    if not url or not key:
        return
    try:
        requests.post(
            f"{url}/rest/v1/{_CF_TABLE}",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=ignore-duplicates",
            },
            json=row,
            timeout=8,
        )
    except Exception:
        pass


def _cf_purge_old_supabase():
    """Xóa các row cũ hơn 21 ngày khỏi Supabase."""
    url, key = _cf_get_supabase_cfg()
    if not url or not key:
        return
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
        requests.delete(
            f"{url}/rest/v1/{_CF_TABLE}?ts=lt.{cutoff}",
            headers={
                "apikey":        key,
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            },
            timeout=8,
        )
    except Exception:
        pass


# ─── Unified load / save (tự chọn Supabase hoặc file JSON) ──────────────────

def _cf_load() -> list:
    """
    Đọc lịch sử CounterFlow.
    Ưu tiên Supabase nếu đã config, fallback về file JSON local.
    """
    if _cf_supabase_available():
        rows = _cf_load_supabase()
        if rows:
            return rows
    # Fallback: file JSON local
    try:
        if _CF_CACHE_FILE.exists():
            data = json.loads(_CF_CACHE_FILE.read_text())
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _cf_save(rows: list):
    """
    Lưu lịch sử CounterFlow.
    Nếu Supabase đã config → upsert row mới nhất vào DB + purge cũ.
    Luôn lưu file JSON local làm backup.
    """
    if not rows:
        return
    latest = rows[-1]
    if _cf_supabase_available():
        _cf_save_supabase(latest)
        # Purge mỗi 100 lần ghi (không cần purge mỗi lần)
        if len(rows) % 100 == 0:
            _cf_purge_old_supabase()
    # Backup file local (giữ 24h gần nhất)
    try:
        rows_local = rows[-288:]
        _CF_CACHE_FILE.write_text(json.dumps(rows_local, ensure_ascii=False))
    except Exception:
        pass


def _cf_fetch_snapshot() -> dict | None:
    """
    Lấy 1 snapshot: giá BTC + OI từ Binance REST.
    Trả về {"ts": ISO, "price": float, "oi_btc": float, "oi_usd": float}
    hoặc None nếu lỗi.
    """
    try:
        _BINANCE_FUTURES = "https://fapi.binance.com"
        # Giá mark price
        r_price = requests.get(
            f"{_BINANCE_FUTURES}/fapi/v1/premiumIndex",
            params={"symbol": "BTCUSDT"}, timeout=8,
        )
        r_price.raise_for_status()
        price = float(r_price.json().get("markPrice", 0))

        # Open Interest
        r_oi = requests.get(
            f"{_BINANCE_FUTURES}/fapi/v1/openInterest",
            params={"symbol": "BTCUSDT"}, timeout=8,
        )
        r_oi.raise_for_status()
        oi_btc = float(r_oi.json().get("openInterest", 0))

        if price <= 0 or oi_btc <= 0:
            return None

        return {
            "ts":     datetime.now(timezone.utc).isoformat(),
            "price":  price,
            "oi_btc": oi_btc,
            "oi_usd": oi_btc * price,
        }
    except Exception:
        return None


def _cf_compute(rows: list) -> dict:
    """
    Tính Net Long %, Net Short %, Spread từ lịch sử rows.
    Logic: ΔPrice × ΔOI → phân loại long_flow / short_flow → cumsum → normalize.

    Trả về dict với arrays sẵn sàng để vẽ Plotly.
    """
    if len(rows) < 2:
        return {}

    prices   = [float(r["price"])  for r in rows]
    ois_btc  = [float(r["oi_btc"]) for r in rows]
    ois_usd  = [float(r["oi_usd"]) for r in rows]
    ts_list  = [r["ts"] for r in rows]

    avg_oi_usd = float(np.mean(ois_usd)) if ois_usd else 1.0
    if avg_oi_usd <= 0:
        avg_oi_usd = 1.0

    long_flow  = [0.0]
    short_flow = [0.0]

    for i in range(1, len(prices)):
        dP      = prices[i] - prices[i - 1]
        dOI_btc = ois_btc[i] - ois_btc[i - 1]
        usd_mag = abs(dOI_btc) * prices[i]

        if   dP > 0 and dOI_btc > 0:   # tiền mới mở LONG
            long_flow.append(+usd_mag);  short_flow.append(0.0)
        elif dP < 0 and dOI_btc < 0:   # LONG đóng/liq
            long_flow.append(-usd_mag);  short_flow.append(0.0)
        elif dP < 0 and dOI_btc > 0:   # tiền mới mở SHORT
            long_flow.append(0.0);       short_flow.append(+usd_mag)
        elif dP > 0 and dOI_btc < 0:   # SHORT bị squeeze
            long_flow.append(0.0);       short_flow.append(-usd_mag)
        else:
            long_flow.append(0.0);       short_flow.append(0.0)

    lf = np.array(long_flow)
    sf = np.array(short_flow)
    nl = np.cumsum(lf) / avg_oi_usd * 100.0
    ns = np.cumsum(sf) / avg_oi_usd * 100.0
    sp = nl - ns

    return {
        "ts":         ts_list,
        "prices":     prices,
        "net_long":   nl.tolist(),
        "net_short":  ns.tolist(),
        "spread":     sp.tolist(),
        "long_flow":  lf.tolist(),
        "short_flow": sf.tolist(),
        "avg_oi_usd": avg_oi_usd,
        "n_rows":     len(rows),
    }


def _cf_zone(nl_val: float, ns_val: float) -> str:
    if nl_val > 10:  return "LONG_EXTREME"
    if nl_val > 5:   return "LONG_FOMO"
    if ns_val > 10:  return "SHORT_EXTREME"
    if ns_val > 5:   return "SHORT_FOMO"
    return "NEUTRAL"


def _cf_signal_label(spread: float, nl: float, ns: float) -> tuple[str, str]:
    """Trả về (label, color)."""
    if abs(spread) > 10:
        return "⚠ ĐẢO CHIỀU — EXTREME FOMO", "#ffd700"
    if spread > 5:
        return "LONG FOMO >5%", "#f85149"
    if spread < -5:
        return "SHORT FOMO <-5%", "#4ade80"
    if spread > 0:
        return "Long Dominant", "#4ade80"
    if spread < 0:
        return "Short Dominant", "#f85149"
    return "Cân bằng", "#8b949e"


def _cf_background_poll():
    """
    Background thread: poll Binance mỗi _CF_POLL_SEC giây.
    Lưu snapshot vào file JSON. Chạy daemon — tắt khi app tắt.
    Guard bằng thread name để không chạy nhiều instance.
    """
    while True:
        try:
            snap = _cf_fetch_snapshot()
            if snap:
                with _CF_LOCK:
                    rows = _cf_load()
                    # Tránh duplicate trong vòng 3 phút
                    if rows:
                        last_ts = datetime.fromisoformat(rows[-1]["ts"])
                        now_utc = datetime.now(timezone.utc)
                        if last_ts.tzinfo is None:
                            last_ts = last_ts.replace(tzinfo=timezone.utc)
                        if (now_utc - last_ts).total_seconds() < 180:
                            time.sleep(_CF_POLL_SEC)
                            continue
                    rows.append(snap)
                    _cf_save(rows)
        except Exception:
            pass
        time.sleep(_CF_POLL_SEC)


def ensure_cf_poll_thread():
    """Khởi động background poll thread nếu chưa có."""
    _name = "CF_Poll_Thread"
    if not any(t.name == _name and t.is_alive() for t in threading.enumerate()):
        t = threading.Thread(target=_cf_background_poll, daemon=True, name=_name)
        t.start()


def make_cf_chart(cf: dict) -> "go.Figure | None":
    """
    Vẽ biểu đồ CounterFlow 2 panel:
      Panel 1 — Net Long % + Net Short % + Spread fill
      Panel 2 — Price (line)
    """
    if not cf or cf.get("n_rows", 0) < 3:
        return None

    ts       = pd.to_datetime(cf["ts"])
    nl       = np.array(cf["net_long"])
    ns       = np.array(cf["net_short"])
    sp       = np.array(cf["spread"])
    prices   = np.array(cf["prices"])

    latest_nl = float(nl[-1])
    latest_ns = float(ns[-1])
    latest_sp = float(sp[-1])
    label, dom_color = _cf_signal_label(latest_sp, latest_nl, latest_ns)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.60, 0.40],
        vertical_spacing=0.04,
        subplot_titles=[
            f"CounterFlow Net Long/Short %  ·  Spread {latest_sp:+.2f}%  ·  {label}",
            "BTC Price (mark)",
        ],
    )

    sp_max = max(float(np.max(np.abs(sp))) * 1.15, 12.0)

    # ── Zone hrects ──────────────────────────────────────────────────────────
    for y0, y1, fc in [
        ( 10,  sp_max, "rgba(248,81,73,0.07)"),
        (  5,  10,     "rgba(248,81,73,0.04)"),
        (-10, -5,      "rgba(63,185,80,0.04)"),
        (-sp_max, -10, "rgba(63,185,80,0.07)"),
    ]:
        fig.add_hrect(y0=y0, y1=y1, fillcolor=fc, line_width=0,
                      layer="below", row=1, col=1)   # type: ignore[call-arg]

    # ── Spread fill ──────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=ts, y=sp.tolist(),
        mode="lines",
        name=f"Spread {latest_sp:+.2f}%",
        line=dict(color=dom_color, width=1.5),
        fill="tozeroy",
        fillcolor="rgba(63,185,80,0.12)" if latest_sp > 0 else "rgba(248,81,73,0.12)",
        hovertemplate="Spread: %{y:.2f}%<extra></extra>",
    ), row=1, col=1)

    # ── Net Long % ───────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=ts, y=nl.tolist(),
        mode="lines",
        name=f"Net Long {latest_nl:+.2f}%",
        line=dict(color="#ffd700", width=2.2),
        hovertemplate="Net Long: %{y:.2f}%<extra></extra>",
    ), row=1, col=1)

    # ── Net Short % ──────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=ts, y=ns.tolist(),
        mode="lines",
        name=f"Net Short {latest_ns:+.2f}%",
        line=dict(color="#388bfd", width=2.2),
        hovertemplate="Net Short: %{y:.2f}%<extra></extra>",
    ), row=1, col=1)

    # ── Threshold lines ──────────────────────────────────────────────────────
    for yv, col_th, lbl_th in [
        ( 10, "#f85149", "+10%"), ( 5, "#f85149", "+5%"),
        (  0, "#484f58", "0"),
        ( -5, "#4ade80", "-5%"), (-10, "#4ade80", "-10%"),
    ]:
        fig.add_hline(y=yv, line_color=col_th, line_dash="dot", line_width=0.9,
                      annotation_text=lbl_th,
                      annotation_position="right",
                      annotation_font=dict(color=col_th, size=8),
                      row=1, col=1)

    # ── Price panel ──────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=ts, y=prices.tolist(),
        mode="lines",
        name="BTC Price",
        line=dict(color="#f7931a", width=1.8),
        hovertemplate="$%{y:,.0f}<extra></extra>",
    ), row=2, col=1)

    # Crossovers: đổi màu vùng nền khi spread đổi dấu
    for i in range(1, len(sp)):
        if sp[i - 1] * sp[i] < 0:
            fig.add_vline(
                x=ts.iloc[i],
                line_color="#ffffff", line_width=0.7, line_dash="dot",
                row=1, col=1,   # type: ignore[call-arg]
            )

    n   = cf["n_rows"]
    hrs = n * _CF_POLL_SEC / 3600

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0f1318",
        height=480,
        title=dict(
            text=(
                f"🌊 CounterFlow Net Long/Short  ·  "
                f"<span style='color:{dom_color}'>{label}</span>  ·  "
                f"<span style='color:#8b949e'>{n} điểm ({hrs:.1f}h · poll 5 phút)</span>"
            ),
            font=dict(color="#e6edf3", size=12, family="monospace"),
            x=0.01,
        ),
        xaxis=dict(gridcolor="#1a2028", tickfont=dict(size=9, color="#8b949e"),
                   showticklabels=False),
        xaxis2=dict(gridcolor="#1a2028", tickfont=dict(size=9, color="#8b949e"),
                    tickformat="%d/%m\n%H:%M"),
        yaxis=dict(gridcolor="#1a2028", ticksuffix="%",
                   tickfont=dict(size=9, color="#8b949e"),
                   title=dict(text="Net Flow %", font=dict(size=9, color="#484f58")),
                   range=[-sp_max, sp_max]),
        yaxis2=dict(gridcolor="#1a2028", tickprefix="$", tickformat=",.0f",
                    tickfont=dict(size=9, color="#8b949e")),
        legend=dict(bgcolor="rgba(13,17,23,0.90)", bordercolor="#30363d",
                    borderwidth=1, font=dict(size=9, color="#c9d1d9"),
                    x=0.0, y=-0.08, xanchor="left", yanchor="top",
                    orientation="h"),
        margin=dict(l=10, r=80, t=55, b=50),
        hovermode="x unified",
    )
    return fig


def render_counterflow_widget(current_price: float = 0.0):
    """
    Widget CounterFlow Net Long/Short — hiển thị ngay dưới MM Weekly Tactics.
    Poll REST Binance mỗi 5 phút, lưu file JSON, bền vững qua restart.
    """
    # Đảm bảo background thread đang chạy
    ensure_cf_poll_thread()

    with st.expander(
        "🌊 CounterFlow Net Long/Short  —  REST Poll 5 phút · Bền vững qua restart",
        expanded=True,
    ):
        # ── Header ────────────────────────────────────────────────────────────
        _hcard("""
        <div style="margin-bottom:12px;padding:10px 14px;background:#0d1117;
             border:1px solid #388bfd44;border-radius:10px;">
          <div style="color:#e6edf3;font-size:.98rem;font-weight:700;margin-bottom:3px;">
            🌊 CounterFlow Net Long / Net Short
          </div>
          <div style="color:#8b949e;font-size:.79rem;">
            Tính từ <b>ΔPrice × ΔOI</b> (Binance REST) · Poll mỗi 5 phút ·
            Lưu file JSON local → không mất khi tắt máy / restart app
          </div>
        </div>""")

        # ── Load data ─────────────────────────────────────────────────────────
        with _CF_LOCK:
            rows = _cf_load()

        cf = _cf_compute(rows)
        n  = len(rows)

        # ── Status badges ─────────────────────────────────────────────────────
        _thread_ok   = any(
            t.name == "CF_Poll_Thread" and t.is_alive()
            for t in threading.enumerate()
        )
        _supa_ok = _cf_supabase_available()

        _thread_badge = (
            '<span style="background:#0d1f12;color:#4ade80;font-size:.68rem;'
            'padding:2px 8px;border-radius:10px;border:1px solid #4ade8044;">'
            '● Poll đang chạy</span>'
            if _thread_ok else
            '<span style="background:#1f0d0d;color:#f87171;font-size:.68rem;'
            'padding:2px 8px;border-radius:10px;border:1px solid #f8717144;">'
            '○ Poll đã dừng</span>'
        )
        _supa_badge = (
            '<span style="background:#0d1a2e;color:#38bdf8;font-size:.68rem;'
            'padding:2px 8px;border-radius:10px;border:1px solid #38bdf844;">'
            '🗄 Supabase ✅ 21 ngày</span>'
            if _supa_ok else
            '<span style="background:#1a1400;color:#fbbf24;font-size:.68rem;'
            'padding:2px 8px;border-radius:10px;border:1px solid #fbbf2444;">'
            '📁 File local (chưa có Supabase)</span>'
        )

        _oldest = rows[0]["ts"][:16].replace("T", " ") if rows else "—"
        _newest = rows[-1]["ts"][:16].replace("T", " ") if rows else "—"
        _days   = n * _CF_POLL_SEC / 86400 if n > 0 else 0

        _hcard(
            f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;">'
            f'{_thread_badge}'
            f'{_supa_badge}'
            f'<span style="background:#0d1117;border:1px solid #30363d;border-radius:8px;'
            f'padding:4px 10px;font-size:.72rem;color:#c9d1d9;">'
            f'📦 {n} điểm · {_days:.1f} ngày dữ liệu</span>'
            f'<span style="background:#0d1117;border:1px solid #30363d;border-radius:8px;'
            f'padding:4px 10px;font-size:.72rem;color:#8b949e;">'
            f'Từ {_oldest} → {_newest}</span>'
            f'<span style="background:#0d1117;border:1px solid #f7931a44;border-radius:8px;'
            f'padding:4px 10px;font-size:.72rem;color:#f7931a;">'
            f'BTC ${current_price:,.0f}</span>'
            f'</div>'
        )

        # ── Chưa đủ data ──────────────────────────────────────────────────────
        if n < 3:
            _hcard("""
            <div style="background:#0d1117;border:1px solid #388bfd44;border-radius:10px;
                 padding:20px;text-align:center;color:#8b949e;font-size:.84rem;line-height:1.8;">
              <div style="font-size:1.5rem;margin-bottom:8px;">⏳</div>
              <b style="color:#c9d1d9;">Đang thu thập dữ liệu…</b><br>
              CounterFlow cần ít nhất <b style="color:#f7931a;">3 điểm</b>
              (15 phút) để tính được.<br>
              Background thread đang poll Binance mỗi 5 phút và lưu vào
              <code>btc_cf_history.json</code>.<br>
              <span style="color:#484f58;font-size:.76rem;">
              Dữ liệu tích lũy tự động — kể cả khi bạn không mở tab này.
              </span>
            </div>""")
            return

        # ── Metric cards ──────────────────────────────────────────────────────
        nl_now = float(cf["net_long"][-1])
        ns_now = float(cf["net_short"][-1])
        sp_now = float(cf["spread"][-1])
        label, dom_color = _cf_signal_label(sp_now, nl_now, ns_now)
        zone = _cf_zone(nl_now, ns_now)

        zone_desc = {
            "LONG_EXTREME":  ("⚠ LONG FOMO CỰC ĐỘ — nguy cơ dump", "#f85149"),
            "LONG_FOMO":     ("LONG đang FOMO — cẩn thận đảo chiều", "#fbbf24"),
            "SHORT_EXTREME": ("⚠ SHORT FOMO CỰC ĐỘ — nguy cơ pump", "#4ade80"),
            "SHORT_FOMO":    ("SHORT đang FOMO — cẩn thận squeeze", "#fbbf24"),
            "NEUTRAL":       ("Thị trường cân bằng", "#8b949e"),
        }.get(zone, ("—", "#8b949e"))

        mc1, mc2, mc3, mc4 = st.columns(4)
        for _col, _lbl, _val, _clr in [
            (mc1, "Net Long",    f"{nl_now:+.2f}%",  "#ffd700"),
            (mc2, "Net Short",   f"{ns_now:+.2f}%",  "#388bfd"),
            (mc3, "Spread",      f"{sp_now:+.2f}%",  dom_color),
            (mc4, "Zone",        zone_desc[0][:20],   zone_desc[1]),
        ]:
            with _col:
                _hcard(
                    f'<div style="background:#0d1117;border:1px solid #21262d;'
                    f'border-radius:8px;padding:10px;text-align:center;">'
                    f'<div style="color:#8b949e;font-size:.67rem;margin-bottom:2px;">{_lbl}</div>'
                    f'<div style="color:{_clr};font-size:.95rem;font-weight:700;">{_val}</div>'
                    f'</div>'
                )

        # ── Signal banner ─────────────────────────────────────────────────────
        _hcard(
            f'<div style="margin:8px 0;padding:8px 14px;background:#0d1117;'
            f'border-left:3px solid {dom_color};border-radius:0 6px 6px 0;'
            f'color:{dom_color};font-size:.82rem;font-weight:600;">'
            f'🎯 {label}  ·  '
            f'<span style="color:#8b949e;font-weight:400;">Spread = Net Long − Net Short</span>'
            f'</div>'
        )

        # ── Chart ─────────────────────────────────────────────────────────────
        fig = make_cf_chart(cf)
        if fig:
            st.plotly_chart(fig, use_container_width=True,
                            config={"displayModeBar": False})

        # ── Logic explanation ─────────────────────────────────────────────────
        with st.expander("📖 Logic tính CounterFlow", expanded=False):
            _hcard("""
            <div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;
                 padding:14px;font-size:.8rem;color:#c9d1d9;line-height:1.9;">
              <b style="color:#f7931a;">ΔPrice × ΔOI → phân loại dòng tiền:</b><br>
              🟡 <b>ΔP+, ΔOI+</b> → Giá tăng + OI tăng = tiền mới vào <b style="color:#ffd700;">MỞ LONG</b><br>
              🔴 <b>ΔP−, ΔOI−</b> → Giá giảm + OI giảm = LONG đóng / bị liq<br>
              🔵 <b>ΔP−, ΔOI+</b> → Giá giảm + OI tăng = tiền mới vào <b style="color:#388bfd;">MỞ SHORT</b><br>
              🟢 <b>ΔP+, ΔOI−</b> → Giá tăng + OI giảm = SHORT bị squeeze<br><br>
              <b style="color:#e6edf3;">Magnitude:</b>
              <code>usd_mag = |ΔΟΙBTC| × Price</code> — giá trị USD thực<br>
              <b style="color:#e6edf3;">Normalize:</b>
              <code>Net_Long% = cumsum(long_flow) / avg_OI_USD × 100</code><br>
              <b style="color:#e6edf3;">Spread:</b>
              <code>Net_Long% − Net_Short%</code><br><br>
              <b style="color:#fbbf24;">Ngưỡng cảnh báo:</b>
              Spread &gt;+10% = Long EXTREME → sắp dump |
              Spread &lt;-10% = Short EXTREME → sắp squeeze
            </div>""")

        # ── Manual refresh + clear ─────────────────────────────────────────────
        rb1, rb2 = st.columns([3, 1])
        with rb1:
            if st.button("🔄 Lấy snapshot mới ngay", key="cf_manual_refresh",
                         use_container_width=True):
                snap = _cf_fetch_snapshot()
                if snap:
                    with _CF_LOCK:
                        _rows = _cf_load()
                        _rows.append(snap)
                        _cf_save(_rows)
                    st.success(f"✅ Đã thêm snapshot: BTC ${snap['price']:,.0f} · OI {snap['oi_btc']:,.0f} BTC")
                    st.rerun()
                else:
                    st.error("❌ Không lấy được data từ Binance REST.")
        with rb2:
            if st.button("🗑 Xóa lịch sử", key="cf_clear_hist",
                         use_container_width=True):
                # Xóa file local
                try:
                    _CF_CACHE_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                # Xóa Supabase nếu đã config
                if _cf_supabase_available():
                    _url, _key = _cf_get_supabase_cfg()
                    try:
                        requests.delete(
                            f"{_url}/rest/v1/{_CF_TABLE}?ts=gte.2000-01-01",
                            headers={
                                "apikey": _key,
                                "Authorization": f"Bearer {_key}",
                            },
                            timeout=8,
                        )
                    except Exception:
                        pass
                st.success("✅ Đã xóa lịch sử CounterFlow (local + Supabase).")
                st.rerun()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _cache_dir() -> _pathlib.Path:
    """Trả về thư mục lịch sử AI, tạo nếu chưa có."""
    _STRATEGY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _STRATEGY_CACHE_DIR


def _cache_filename(ts: datetime) -> _pathlib.Path:
    """Tên file theo timestamp: YYYYMMDD_HHMMSS.json"""
    return _cache_dir() / ts.strftime("%Y%m%d_%H%M%S.json")


def _purge_old_cache():
    """Xóa các file cache cũ hơn _CACHE_TTL_DAYS ngày."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_CACHE_TTL_DAYS)
    try:
        for f in _cache_dir().glob("*.json"):
            try:
                data = json.loads(f.read_text())
                saved_raw = data.get("saved_at", "")
                saved_dt  = datetime.fromisoformat(saved_raw)
                if saved_dt.tzinfo is None:
                    saved_dt = saved_dt.replace(tzinfo=timezone.utc)
                if saved_dt < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _save_strategy_to_disk(analysis: dict, strategy: dict, source: str = "image"):
    """Lưu chiến thuật vào file JSON — bền vững qua restart/reconnect, giữ 2 tuần."""
    try:
        now = datetime.now(timezone.utc)
        payload = {
            "saved_at": now.isoformat(),
            "source":   source,
            "analysis": analysis,
            "strategy": strategy,
        }
        # Lưu vào thư mục lịch sử (theo timestamp)
        _cache_filename(now).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        # Cập nhật file "latest" để load nhanh khi restart
        _STRATEGY_CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        # Dọn cache cũ hơn 14 ngày
        _purge_old_cache()
    except Exception:
        pass


def _load_strategy_from_disk() -> dict:
    """Đọc chiến thuật mới nhất từ disk. Trả về {} nếu chưa có."""
    try:
        if _STRATEGY_CACHE_FILE.exists():
            return json.loads(_STRATEGY_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _load_all_history(days: int = 14) -> list:
    """
    Đọc toàn bộ lịch sử AI phân tích trong `days` ngày gần nhất.
    Trả về list[dict], sắp xếp mới nhất trước.
    """
    _purge_old_cache()
    results = []
    try:
        for f in sorted(_cache_dir().glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
                results.append(data)
            except Exception:
                pass
    except Exception:
        pass
    return results


def _clear_strategy_from_disk():
    """Xóa toàn bộ cache chiến thuật (file latest + thư mục lịch sử)."""
    try:
        if _STRATEGY_CACHE_FILE.exists():
            _STRATEGY_CACHE_FILE.unlink()
    except Exception:
        pass
    try:
        for f in _cache_dir().glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _merge_liq_analyses(analyses: list) -> dict:
    """
    Gộp kết quả phân tích từ nhiều ảnh (tối đa 10).
    Trả về 1 dict tổng hợp.
    """
    valid = [a for a in analyses if a and "error" not in a]
    if not valid:
        return analyses[0] if analyses else {"error": "Tất cả ảnh phân tích thất bại"}
    if len(valid) == 1:
        return valid[0]

    merged: dict = {}
    # current_price: lấy cái đầu tiên không null
    for a in valid:
        if a.get("current_price"):
            merged["current_price"] = a["current_price"]
            break
    merged.setdefault("current_price", None)

    # price_range: union
    lows  = [a["price_range"]["low"]  for a in valid if a.get("price_range")]
    highs = [a["price_range"]["high"] for a in valid if a.get("price_range")]
    merged["price_range"] = {"low": min(lows) if lows else 0, "high": max(highs) if highs else 0}

    def _merge_clusters(cluster_lists):
        all_c = []
        for cl in cluster_lists:
            all_c.extend(cl)
        all_c.sort(key=lambda x: x.get("price_level", 0))
        merged_c = []
        for c in all_c:
            if merged_c and abs(c.get("price_level", 0) - merged_c[-1]["price_level"]) < 500:
                if c.get("size_usd_millions", 0) > merged_c[-1].get("size_usd_millions", 0):
                    merged_c[-1]["size_usd_millions"] = c["size_usd_millions"]
                    merged_c[-1]["strength"] = c.get("strength", merged_c[-1]["strength"])
            else:
                merged_c.append(dict(c))
        return merged_c

    merged["long_liquidation_clusters"]  = _merge_clusters(
        [a.get("long_liquidation_clusters",  []) for a in valid])
    merged["short_liquidation_clusters"] = _merge_clusters(
        [a.get("short_liquidation_clusters", []) for a in valid])

    sides = [a.get("dominant_side", "balanced") for a in valid]
    merged["dominant_side"] = max(set(sides), key=sides.count)
    merged["total_long_liq_usd_millions"]  = sum(a.get("total_long_liq_usd_millions",  0) for a in valid) / len(valid)
    merged["total_short_liq_usd_millions"] = sum(a.get("total_short_liq_usd_millions", 0) for a in valid) / len(valid)
    merged["key_support_levels"]    = sorted(set(sum([a.get("key_support_levels",    []) for a in valid], [])))
    merged["key_resistance_levels"] = sorted(set(sum([a.get("key_resistance_levels", []) for a in valid], [])))
    for k in ("biggest_long_wall", "biggest_short_wall"):
        vals = [a.get(k) for a in valid if a.get(k)]
        merged[k] = vals[0] if vals else None
    conf_rank = {"low": 0, "medium": 1, "high": 2}
    best_conf = max(valid, key=lambda a: conf_rank.get(a.get("analysis_confidence", "low"), 0))
    merged["analysis_confidence"] = best_conf.get("analysis_confidence", "low")
    merged["chart_timeframe"]  = "; ".join(set(a.get("chart_timeframe", "?")  for a in valid if a.get("chart_timeframe",  "?")))
    merged["data_source_hint"] = "; ".join(set(a.get("data_source_hint", "?") for a in valid if a.get("data_source_hint", "?")))
    merged["raw_observations"] = f"Tổng hợp {len(valid)}/{len(analyses)} ảnh: " + " | ".join(
        a.get("raw_observations", "") for a in valid if a.get("raw_observations"))
    merged["_image_count"] = len(analyses)
    merged["_valid_count"] = len(valid)
    return merged


# ═════════════════════════════════════════════════════════════════════════════
# TEXT STRATEGY MODULE — Python logic (không gọi AI trực tiếp)
# Người dùng dán chiến thuật từ AI bên ngoài → Python parse + vẽ 2 kịch bản chart
# ═════════════════════════════════════════════════════════════════════════════

def _parse_strategy_text_py(
    text: str,
    current_price: float,
    call_wall: float,
    put_wall: float,
    weekly_max_pain: float,
) -> dict:
    """
    Phân tích chiến thuật bằng Python thuần (regex + heuristics).
    Trích xuất direction, entry, SL, targets từ text người dùng nhập.
    """
    import re

    text_lower = text.lower()

    # ── Direction detection ──
    long_score  = sum(text_lower.count(w) for w in
                      ["long", "mua", "buy", "tăng", "bull", "lên", "upside", "pump"])
    short_score = sum(text_lower.count(w) for w in
                      ["short", "bán", "sell", "giảm", "bear", "xuống", "downside", "dump"])
    direction = "long" if long_score > short_score else ("short" if short_score > long_score else "neutral")

    # ── Price extractor ──
    def _to_price(s: str):
        s = s.strip().replace(",", "").replace("$", "")
        try:
            if s.lower().endswith("k"):
                v = float(s[:-1]) * 1000
            else:
                v = float(s)
                if 30 < v < 300:
                    v *= 1000
            return v if 25000 <= v <= 300000 else None
        except Exception:
            return None

    raw_nums = re.findall(r'\$?(\d[\d,\.]*k?)', text, re.IGNORECASE)
    prices = sorted(set(v for v in (_to_price(n) for n in raw_nums) if v))

    # ── Keyword-based level extraction ──
    def _find_level(keywords):
        for kw in keywords:
            idx = text_lower.find(kw.lower())
            if idx < 0:
                continue
            snippet = text[max(0, idx - 3): min(len(text), idx + 90)]
            nums = re.findall(r'\$?(\d[\d,\.]*k?)', snippet, re.IGNORECASE)
            for n in nums:
                v = _to_price(n)
                if v and 25000 <= v <= 300000:
                    return v
        return None

    entry_price = _find_level(["entry", "vào lệnh", "vào từ", "long từ", "short từ",
                                "mua từ", "buy từ", "mua tại", "vùng vào", "enter"])
    stop_loss   = _find_level(["stop loss", "sl tại", "sl:", "stoploss",
                                "cắt lỗ", "stop tại", "cut loss", "invalidate"])
    target_1    = _find_level(["target 1", "tp1", "t1:", "mục tiêu 1",
                                "target1", "tp 1", "take profit 1"])
    target_2    = _find_level(["target 2", "tp2", "t2:", "mục tiêu 2",
                                "target2", "tp 2", "take profit 2"])
    target_3    = _find_level(["target 3", "tp3", "t3:", "mục tiêu 3",
                                "target3", "tp 3", "take profit 3"])

    # ── Fallback: infer from sorted price list ──
    above = sorted([p for p in prices if p > current_price * 0.999])
    below = sorted([p for p in prices if p < current_price * 1.001], reverse=True)

    if entry_price is None:
        entry_price = (min(prices, key=lambda x: abs(x - current_price))
                       if prices else current_price)

    if direction == "long":
        if stop_loss is None:
            stop_loss = below[0] if below else current_price * 0.970
        if target_1 is None:
            cands = [p for p in above if p > (entry_price or current_price)]
            target_1 = cands[0] if cands else (call_wall or (entry_price or current_price) * 1.05)
        if target_2 is None:
            cands2 = [p for p in above if p > (target_1 or current_price)]
            target_2 = cands2[0] if cands2 else (target_1 or current_price) * 1.04
    elif direction == "short":
        if stop_loss is None:
            stop_loss = above[0] if above else current_price * 1.030
        if target_1 is None:
            cands = [p for p in below if p < (entry_price or current_price)]
            target_1 = cands[0] if cands else (put_wall or (entry_price or current_price) * 0.95)
        if target_2 is None:
            cands2 = [p for p in below if p < (target_1 or current_price)]
            target_2 = cands2[0] if cands2 else (target_1 or current_price) * 0.96
    else:
        mp = weekly_max_pain or current_price
        if stop_loss is None:  stop_loss = current_price * 0.970
        if target_1  is None:  target_1  = call_wall or mp * 1.03
        if target_2  is None:  target_2  = (target_1 or mp) * 1.04

    if entry_price is None:
        entry_price = current_price

    risk   = abs(entry_price - (stop_loss or entry_price))
    reward = abs((target_1 or entry_price) - entry_price)
    rr     = round(reward / risk, 2) if risk > 50 else 2.0

    return {
        "direction":         direction,
        "entry_price":       round(entry_price) if entry_price else None,
        "stop_loss":         round(stop_loss)   if stop_loss   else None,
        "target_1":          round(target_1)    if target_1    else None,
        "target_2":          round(target_2)    if target_2    else None,
        "target_3":          round(target_3)    if target_3    else None,
        "risk_reward":       rr,
        "call_wall":         call_wall,
        "put_wall":          put_wall,
        "weekly_max_pain":   weekly_max_pain,
        "raw_prices_found":  prices,
    }


def _gen_price_path_smooth(waypoints: list, noise_pct: float = 0.008, seed: int = 42) -> list:
    """
    Tạo price path 14 ngày qua các waypoints.
    Mỗi ngày có low / mid / high với noise nhỏ — không dùng numpy.
    """
    import random
    rng = random.Random(seed)
    days_dict = dict(waypoints)
    days_sorted = sorted(days_dict.keys())

    def _lerp(d):
        lo = max((k for k in days_sorted if k <= d), default=days_sorted[0])
        hi = min((k for k in days_sorted if k >= d), default=days_sorted[-1])
        if lo == hi:
            return days_dict[lo]
        t = (d - lo) / (hi - lo)
        t = t * t * (3 - 2 * t)
        return days_dict[lo] + (days_dict[hi] - days_dict[lo]) * t

    path = []
    for d in range(14):
        base   = _lerp(d)
        noise  = base * noise_pct * rng.gauss(0, 1)
        spread = base * noise_pct * 1.6
        mid    = base + noise
        path.append({
            "day":        d,
            "price_low":  round(mid - spread, 0),
            "price_high": round(mid + spread, 0),
            "price_mid":  round(mid, 0),
        })
    return path


def _build_scenario_fig(
    path: list, dates: list, color: str, title: str,
    parsed: dict, current_price: float,
    call_wall: float, put_wall: float, max_pain: float,
    long_liq: list, short_liq: list,
    is_base: bool = True,
) -> "go.Figure":
    """Vẽ 1 biểu đồ kịch bản Plotly."""
    lows  = [p["price_low"]  for p in path]
    highs = [p["price_high"] for p in path]
    mids  = [p["price_mid"]  for p in path]

    fill_map = {"#4ade80": "46,160,67", "#f87171": "248,81,73", "#fbbf24": "251,191,36"}
    rgb = fill_map.get(color, "251,191,36")

    fig = go.Figure()

    # Shadow band
    fig.add_trace(go.Scatter(
        x=dates + dates[::-1], y=highs + lows[::-1],
        fill="toself", fillcolor=f"rgba({rgb},0.13)",
        line=dict(width=0), showlegend=False, hoverinfo="skip", name="Range",
    ))
    # Mid line
    fig.add_trace(go.Scatter(
        x=dates, y=mids, mode="lines",
        line=dict(color=color, width=2.5), name="Price Path",
        hovertemplate="%{x|%d/%m}<br>$%{y:,.0f}<extra></extra>",
    ))

    # Current price
    if current_price:
        fig.add_hline(y=current_price, line_color="#f7931a", line_dash="dot",
                      line_width=1.8, opacity=0.9,
                      annotation_text=f"  Hiện tại ${current_price:,.0f}",
                      annotation_font_color="#f7931a", annotation_font_size=9)

    # Key levels from parsed strategy
    for key, clr, lbl, ds in [
        ("entry_price", "#f7931a",  "Entry",    "dash"),
        ("stop_loss",   "#f87171",  "Stop Loss","dot"),
        ("target_1",    "#4ade80",  "TP1",      "dashdot"),
        ("target_2",    "#86efac",  "TP2",      "dashdot"),
        ("target_3",    "#bbf7d0",  "TP3",      "dashdot"),
    ]:
        val = parsed.get(key)
        if val:
            fig.add_hline(y=val, line_color=clr, line_dash=ds,
                          line_width=1.4, opacity=0.8,
                          annotation_text=f"  {lbl}: ${val:,.0f}",
                          annotation_font_color=clr, annotation_font_size=9)

    # Options walls
    if call_wall:
        fig.add_hline(y=call_wall, line_color="#818cf8", line_dash="longdash",
                      line_width=1.2, opacity=0.7,
                      annotation_text=f"  Call Wall ${call_wall:,.0f}",
                      annotation_font_color="#818cf8", annotation_font_size=8)
    if put_wall:
        fig.add_hline(y=put_wall, line_color="#fb923c", line_dash="longdash",
                      line_width=1.2, opacity=0.7,
                      annotation_text=f"  Put Wall ${put_wall:,.0f}",
                      annotation_font_color="#fb923c", annotation_font_size=8)
    if max_pain:
        fig.add_hline(y=max_pain, line_color="#a78bfa", line_dash="dash",
                      line_width=1.3, opacity=0.75,
                      annotation_text=f"  Max Pain ${max_pain:,.0f}",
                      annotation_font_color="#a78bfa", annotation_font_size=8)

    # Liq zones
    for lp in long_liq[:3]:
        fig.add_hrect(y0=lp * 0.999, y1=lp * 1.001,
                      fillcolor="rgba(248,113,113,0.09)", line_width=0,
                      annotation_text=f"  Long Liq ${lp:,.0f}",
                      annotation_font_color="#f87171", annotation_font_size=7)
    for sp in short_liq[:3]:
        fig.add_hrect(y0=sp * 0.999, y1=sp * 1.001,
                      fillcolor="rgba(74,222,128,0.09)", line_width=0,
                      annotation_text=f"  Short Liq ${sp:,.0f}",
                      annotation_font_color="#4ade80", annotation_font_size=7)

    rr = parsed.get("risk_reward", 0)
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#161b22",
        height=420,
        title=dict(text=f"{title}{'  ·  R/R '+str(rr)+'x' if rr else ''}",
                   font=dict(color="#e6edf3", size=12)),
        xaxis=dict(gridcolor="#21262d", title="", tickformat="%d/%m",
                   tickfont=dict(size=9)),
        yaxis=dict(gridcolor="#21262d", title="Giá BTC ($)",
                   tickprefix="$", tickformat=",.0f", tickfont=dict(size=9)),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=9)),
        margin=dict(l=12, r=12, t=48, b=10),
    )
    return fig


def _draw_two_scenario_charts(
    parsed: dict,
    current_price: float,
    liq_analysis: dict = None,
) -> tuple:
    """
    Vẽ 2 biểu đồ kịch bản 2 tuần:
      Biểu đồ 1 — Kịch bản Cơ sở (theo chiến thuật người dùng)
      Biểu đồ 2 — Kịch bản Thay thế (sweep / đảo chiều)
    Tích hợp Call/Put Wall, Max Pain và liq clusters từ ảnh AI nếu có.
    """
    direction = parsed.get("direction", "neutral")
    entry     = parsed.get("entry_price") or current_price
    sl        = parsed.get("stop_loss")
    t1        = parsed.get("target_1")
    t2        = parsed.get("target_2")
    call_wall = parsed.get("call_wall", 0.0) or 0.0
    put_wall  = parsed.get("put_wall",  0.0) or 0.0
    max_pain  = parsed.get("weekly_max_pain", 0.0) or 0.0

    today = datetime.now(timezone.utc).replace(tzinfo=None)
    dates = [today + timedelta(days=d) for d in range(14)]

    # Liq clusters from image AI (nếu có)
    long_liq, short_liq = [], []
    if liq_analysis and "error" not in liq_analysis:
        for c in liq_analysis.get("long_liquidation_clusters", []):
            p = c.get("price_level")
            if p: long_liq.append(p)
        for c in liq_analysis.get("short_liquidation_clusters", []):
            p = c.get("price_level")
            if p: short_liq.append(p)

    # ── Biểu đồ 1: Kịch bản Cơ sở ──
    if direction == "long":
        mid_1 = (entry + (t1 or entry * 1.05)) / 2
        wp1 = [(0, current_price), (2, entry * 0.9985), (6, mid_1),
               (10, t1 or entry * 1.05), (13, t2 or (t1 or entry) * 1.035)]
        col1   = "#4ade80"
        title1 = f"📈 Kịch Bản CƠ SỞ — LONG  Entry ${entry:,.0f} → TP1 ${(t1 or 0):,.0f}"
    elif direction == "short":
        mid_1 = (entry + (t1 or entry * 0.95)) / 2
        wp1 = [(0, current_price), (2, entry * 1.0015), (6, mid_1),
               (10, t1 or entry * 0.95), (13, t2 or (t1 or entry) * 0.965)]
        col1   = "#f87171"
        title1 = f"📉 Kịch Bản CƠ SỞ — SHORT  Entry ${entry:,.0f} → TP1 ${(t1 or 0):,.0f}"
    else:
        mp_lvl = max_pain or current_price
        wp1 = [(0, current_price), (4, (current_price + mp_lvl) / 2),
               (9, mp_lvl), (13, mp_lvl * 1.012)]
        col1   = "#fbbf24"
        title1 = f"⟷ Kịch Bản CƠ SỞ — NEUTRAL  Max Pain ${mp_lvl:,.0f}"

    path1 = _gen_price_path_smooth(wp1, noise_pct=0.008, seed=101)
    fig1  = _build_scenario_fig(
        path=path1, dates=dates, color=col1, title=title1,
        parsed=parsed, current_price=current_price,
        call_wall=call_wall, put_wall=put_wall, max_pain=max_pain,
        long_liq=long_liq, short_liq=short_liq, is_base=True,
    )

    # ── Biểu đồ 2: Kịch bản Thay thế (sweep SL / đảo chiều) ──
    if direction == "long":
        sl_lvl  = sl or entry * 0.970
        dip_lvl = (put_wall if put_wall and put_wall < sl_lvl else sl_lvl * 0.990)
        wp2 = [(0, current_price), (3, sl_lvl * 0.997), (6, dip_lvl),
               (10, (current_price + sl_lvl) / 2), (13, sl_lvl * 0.994)]
        col2   = "#f87171"
        title2 = f"📉 Kịch Bản THAY THẾ — Breakdown  SL ${sl_lvl:,.0f} bị phá"
    elif direction == "short":
        sl_lvl   = sl or entry * 1.030
        pump_lvl = (call_wall if call_wall and call_wall > sl_lvl else sl_lvl * 1.010)
        wp2 = [(0, current_price), (3, sl_lvl * 1.003), (6, pump_lvl),
               (10, (current_price + sl_lvl) / 2), (13, sl_lvl * 1.006)]
        col2   = "#4ade80"
        title2 = f"📈 Kịch Bản THAY THẾ — Breakout  SL ${sl_lvl:,.0f} bị phá"
    else:
        dip = put_wall if put_wall else current_price * 0.930
        wp2 = [(0, current_price), (4, current_price * 0.985), (8, dip),
               (13, (current_price + dip) / 2)]
        col2   = "#f87171"
        title2 = f"📉 Kịch Bản THAY THẾ — Bearish Dip  ${dip:,.0f}"

    path2 = _gen_price_path_smooth(wp2, noise_pct=0.009, seed=202)
    fig2  = _build_scenario_fig(
        path=path2, dates=dates, color=col2, title=title2,
        parsed=parsed, current_price=current_price,
        call_wall=call_wall, put_wall=put_wall, max_pain=max_pain,
        long_liq=long_liq, short_liq=short_liq, is_base=False,
    )

    return fig1, fig2


def render_text_strategy_widget(
    current_price: float = 0.0,
    daily_walls: dict = None,
    weekly_max_pain: float = 0.0,
    oi_total_usd: float = 0.0,
):
    """
    Widget nhập chiến thuật bằng chữ (từ AI bên ngoài như ChatGPT/Claude/v.v.).
    Python tự phân tích text + kết hợp Liquidation + Max Pain → vẽ 2 biểu đồ kịch bản 2 tuần.
    KHÔNG gọi AI trực tiếp.
    """
    for k in ("_text_strat_parsed", "_text_strat_figs"):
        if k not in st.session_state:
            st.session_state[k] = None

    # ── Call/Put Wall từ daily_walls ──
    call_wall, put_wall = 0.0, 0.0
    if daily_walls:
        for dk in sorted(daily_walls.keys()):
            w = daily_walls.get(dk, {})
            if w.get("wall_active") and w.get("call_wall"):
                call_wall = float(w.get("call_wall") or 0)
                put_wall  = float(w.get("put_wall")  or 0)
                break

    with st.expander(
        "✍️ Phân tích chiến thuật bằng chữ  —  Python vẽ 2 biểu đồ kịch bản 2 tuần",
        expanded=False,
    ):
        # ── Header ──
        _hcard("""
        <div style="margin-bottom:12px;padding:10px 14px;background:#0d1117;
             border:1px solid #30363d;border-radius:10px;">
          <div style="color:#e6edf3;font-size:.98rem;font-weight:700;margin-bottom:3px;">
            ✍️ Chiến Thuật Của Bạn → Biểu Đồ 2 Kịch Bản (2 Tuần)
          </div>
          <div style="color:#8b949e;font-size:.79rem;">
            Dán chiến thuật đã phân tích (từ AI bên ngoài) →
            Python kết hợp Liquidation + Max Pain → vẽ 2 kịch bản giá
          </div>
        </div>""")

        # ── Market context badge ──
        _hcard(f"""<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;">
          <div style="background:#0d1117;border:1px solid #f7931a44;border-radius:8px;
               padding:6px 12px;font-size:.78rem;">
            <span style="color:#8b949e;">BTC: </span>
            <span style="color:#f7931a;font-weight:700;">${current_price:,.0f}</span>
          </div>
          <div style="background:#0d1117;border:1px solid #4ade8044;border-radius:8px;
               padding:6px 12px;font-size:.78rem;">
            <span style="color:#8b949e;">Call Wall: </span>
            <span style="color:#4ade80;font-weight:700;">${call_wall:,.0f}</span>
          </div>
          <div style="background:#0d1117;border:1px solid #fb923c44;border-radius:8px;
               padding:6px 12px;font-size:.78rem;">
            <span style="color:#8b949e;">Put Wall: </span>
            <span style="color:#fb923c;font-weight:700;">${put_wall:,.0f}</span>
          </div>
          <div style="background:#0d1117;border:1px solid #a78bfa44;border-radius:8px;
               padding:6px 12px;font-size:.78rem;">
            <span style="color:#8b949e;">Max Pain: </span>
            <span style="color:#a78bfa;font-weight:700;">${weekly_max_pain:,.0f}</span>
          </div>
        </div>""")

        # ── Text area ──
        _hcard('<div style="color:#f7931a;font-weight:600;font-size:.8rem;margin-bottom:6px;">'
               '📝 Dán chiến thuật đã được AI phân tích (ChatGPT, Claude, Gemini, v.v.):</div>')
        user_text = st.text_area(
            "Chiến thuật",
            height=130,
            placeholder=(
                "Ví dụ:\n"
                "Long BTC từ vùng 103,000-104,000, target 1 là 108,000, target 2 là 112,000.\n"
                "Stop loss tại 101,500. Kỳ vọng giá sẽ retest vùng support rồi bật lên.\n"
                "Nếu giá phá 101,000 thì đảo chiều short target 97,000."
            ),
            key="ts_user_text",
            label_visibility="collapsed",
        )

        tb1, tb2 = st.columns(2)
        with tb1:
            do_analyze = st.button(
                "📊 Phân tích & Vẽ 2 biểu đồ kịch bản 2 tuần",
                type="primary", use_container_width=True, key="ts_btn_analyze",
            )
        with tb2:
            do_reset = st.button(
                "🗑 Reset", use_container_width=True, key="ts_btn_reset",
            )

        if do_reset:
            st.session_state._text_strat_parsed = None
            st.session_state._text_strat_figs   = None
            st.rerun()

        if do_analyze:
            if not user_text.strip():
                st.warning("Vui lòng nhập chiến thuật trước khi phân tích.")
            else:
                with st.spinner("📊 Đang phân tích chiến thuật và vẽ 2 biểu đồ kịch bản…"):
                    _liq_data = (st.session_state.get("_liq_ai_analysis")
                                 or st.session_state.get("_inline_liq_analysis"))
                    _parsed = _parse_strategy_text_py(
                        user_text, current_price,
                        call_wall, put_wall, weekly_max_pain,
                    )
                    _fig1, _fig2 = _draw_two_scenario_charts(
                        _parsed, current_price, liq_analysis=_liq_data,
                    )
                st.session_state._text_strat_parsed = _parsed
                st.session_state._text_strat_figs   = (_fig1, _fig2)
                st.rerun()

        # ── Results ──
        _parsed_res = st.session_state._text_strat_parsed
        _figs_res   = st.session_state._text_strat_figs

        if _parsed_res and _figs_res:
            st.markdown("---")
            direction = _parsed_res.get("direction", "neutral")
            dir_label = {"long": "↑ LONG", "short": "↓ SHORT", "neutral": "→ NEUTRAL"}.get(direction, "—")
            dir_color = {"long": "#4ade80", "short": "#f87171", "neutral": "#fbbf24"}.get(direction, "#8b949e")

            # ── Key levels summary ──
            sm_cols = st.columns(5)
            _metric_cfg = [
                (0, "Direction",  None,           dir_color),
                (1, "Entry",      "entry_price",  "#f7931a"),
                (2, "Stop Loss",  "stop_loss",    "#f87171"),
                (3, "TP1",        "target_1",     "#4ade80"),
                (4, "TP2",        "target_2",     "#86efac"),
            ]
            for col_idx, lbl, key, clr in _metric_cfg:
                with sm_cols[col_idx]:
                    if key is None:
                        val_str = dir_label
                    else:
                        v = _parsed_res.get(key)
                        val_str = f"${v:,.0f}" if v else "—"
                    _hcard(f"""<div style="background:#0d1117;border:1px solid #21262d;
                         border-radius:8px;padding:10px;text-align:center;">
                      <div style="color:#8b949e;font-size:.67rem;margin-bottom:2px;">{lbl}</div>
                      <div style="color:{clr};font-size:.9rem;font-weight:700;">{val_str}</div>
                    </div>""")

            rr = _parsed_res.get("risk_reward", 0)
            found_prices = _parsed_res.get("raw_prices_found", [])
            _hcard(
                f'<div style="color:#8b949e;font-size:.74rem;margin:6px 0 12px 0;">'
                f'R/R: <span style="color:#fbbf24;font-weight:600;">{rr:.1f}x</span>'
                + (f'  ·  Giá đọc được: ' + ", ".join(f"${p:,.0f}" for p in found_prices[:6]) if found_prices else "")
                + "</div>"
            )

            # ── Chart 1 ──
            _hcard('<div style="color:#4ade80;font-weight:600;font-size:.82rem;'
                   'margin:4px 0 4px 0;">📈 Kịch Bản 1 — Cơ Sở (theo chiến thuật của bạn)</div>')
            st.plotly_chart(_figs_res[0], width="stretch", config={"displayModeBar": False})

            # ── Chart 2 ──
            st.markdown("<br>", unsafe_allow_html=True)
            _hcard('<div style="color:#f87171;font-weight:600;font-size:.82rem;'
                   'margin:4px 0 4px 0;">📉 Kịch Bản 2 — Thay Thế (cú sweep SL / đảo chiều)</div>')
            st.plotly_chart(_figs_res[1], width="stretch", config={"displayModeBar": False})


# ═════════════════════════════════════════════════════════════════════════════
# LIQ IMAGE AI MODULE — Google Gemini FREE + Claude (inline)
# Config: .streamlit/secrets.toml
#   [gemini]
# Config: .streamlit/secrets.toml
#   [gemini]
#   api_key = "AIza..."          ← lấy miễn phí tại aistudio.google.com/apikey
#   [claude]
#   api_key = "sk-ant-..."       ← claude.ai → Settings → API Keys (trả phí)
#
# Cài thêm: pip install google-generativeai anthropic pillow
# ═════════════════════════════════════════════════════════════════════════════

import base64, re, os, io

# ── Lazy imports: chỉ import khi tab được dùng ─────────────────────────────
def _get_gemini_model(override_key: str = ""):
    """
    Lấy Gemini model với fallback tự động:
      - Nếu override_key được truyền vào (từ UI input) → dùng key đó trước tiên
      - Thử lần lượt từng API key (nếu key bị quota exceeded → sang key tiếp theo)
      - Với mỗi key, thử lần lượt từng model từ tốt nhất xuống thấp nhất
    Thêm key mới vào _HARDCODED_KEYS để mở rộng quota.
    """
    # ── THÊM KEY VÀO ĐÂY — khi hết quota key này sẽ tự dùng key tiếp theo ──
    _HARDCODED_KEYS = [
        "AIzaSyB6Ap1Tf1-_-Azoz8pIVH1YqKtoXTNDQDw",   # key 1
        "AIzaSyAc4TuQ5_K8jaih5tbYjMxNQL4i9FgNnoA",   # key 2
        # "AIzaSy...",  # thêm key 3 tại đây nếu cần
    ]

    _MODEL_PRIORITY = [
        "gemini-2.5-pro-preview-05-06",
        "gemini-2.5-flash-preview-05-20",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b",
    ]

    # Từ khóa nhận biết lỗi quota/rate-limit (không phải lỗi model không tồn tại)
    _QUOTA_KEYWORDS = ("quota", "rate", "limit", "exceeded", "429", "resource exhausted")

    try:
        import google.generativeai as _genai

        # Thu thập tất cả key: override (UI input) → hard-coded → secrets → env
        all_keys = []
        if override_key and override_key.strip():
            all_keys.append(override_key.strip())
        all_keys += [k.strip() for k in _HARDCODED_KEYS if k.strip() and k.strip() not in all_keys]
        try:
            env_key = st.secrets["gemini"]["api_key"].strip()
            if env_key and env_key not in all_keys:
                all_keys.append(env_key)
        except Exception:
            pass
        env_key2 = os.environ.get("GEMINI_API_KEY", "").strip()
        if env_key2 and env_key2 not in all_keys:
            all_keys.append(env_key2)

        if not all_keys:
            return None, "gemini_no_key"

        last_err = None
        # Vòng ngoài: thử từng API key
        for key_idx, key in enumerate(all_keys):
            _genai.configure(api_key=key)
            key_label = f"key{key_idx+1}({key[:8]}...)"

            # Vòng trong: thử từng model với key này
            for model_name in _MODEL_PRIORITY:
                try:
                    model = _genai.GenerativeModel(model_name)
                    model.generate_content("hi", generation_config={"max_output_tokens": 1})
                    return model, f"gemini:{model_name}:{key_label}"
                except Exception as e:
                    err_str = str(e).lower()
                    last_err = e
                    if any(kw in err_str for kw in _QUOTA_KEYWORDS):
                        # Quota lỗi → bỏ toàn bộ key này, thử key kế tiếp
                        break
                    # Lỗi khác (model không tồn tại, v.v.) → thử model kế tiếp
                    continue

        return None, f"gemini_all_keys_exhausted: {last_err}"

    except ImportError:
        return None, "gemini_not_installed"


def _get_claude_client(override_key: str = ""):
    try:
        import anthropic as _ant
        key = None
        if override_key and override_key.strip():
            key = override_key.strip()
        if not key:
            try:
                key = st.secrets["claude"]["api_key"]
            except Exception:
                key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return None, "claude_no_key"
        return _ant.Anthropic(api_key=key), "claude"
    except ImportError:
        return None, "claude_not_installed"


_ANALYSIS_PROMPT_LIQ = """
Bạn là chuyên gia phân tích liquidation heatmap BTC crypto.
Hình ảnh này là liquidation chart từ Coinglass, Hyblock, Velo, CryptoQuant, v.v.

NHIỆM VỤ: Đọc hình và trích xuất dữ liệu JSON.
Trả về DUY NHẤT JSON hợp lệ, KHÔNG có text thêm, KHÔNG có backtick markdown.

Schema:
{
  "current_price": <số hoặc null>,
  "price_range": {"low": <số>, "high": <số>},
  "long_liquidation_clusters": [
    {"price_level": <số>, "size_usd_millions": <số>, "strength": "low|medium|high|extreme"}
  ],
  "short_liquidation_clusters": [
    {"price_level": <số>, "size_usd_millions": <số>, "strength": "low|medium|high|extreme"}
  ],
  "dominant_side": "long|short|balanced",
  "biggest_long_wall": <price hoặc null>,
  "biggest_short_wall": <price hoặc null>,
  "total_long_liq_usd_millions": <số>,
  "total_short_liq_usd_millions": <số>,
  "key_support_levels": [<price>],
  "key_resistance_levels": [<price>],
  "chart_timeframe": "<string hoặc unknown>",
  "data_source_hint": "<tên web nếu nhận ra, hoặc unknown>",
  "analysis_confidence": "low|medium|high",
  "raw_observations": "<mô tả ngắn tiếng Việt>"
}

QUY TẮC:
- long_liq = vùng GIÁ DƯỚI hiện tại (long bị thanh lý khi giá giảm xuống).
- short_liq = vùng GIÁ TRÊN hiện tại (short bị thanh lý khi giá tăng lên).
- strength: low<50M | medium 50-200M | high 200-500M | extreme>500M.
- Chỉ JSON. Không gì khác.
"""

_STRATEGY_PROMPT_LIQ = """
Bạn là chiến lược gia giao dịch BTC futures chuyên nghiệp.

DỮ LIỆU:
- Giá hiện tại: ${current_price:,.0f}
- Tuần: {week_label}
- Phân tích từ ảnh liquidation:
{liq_analysis}

NHIỆM VỤ: Tạo 4 chiến thuật A/B/C/D cho tuần tới.
Trả về DUY NHẤT JSON hợp lệ, KHÔNG có text thêm, KHÔNG backtick.

Schema:
{{
  "week_label": "<tuần>",
  "market_bias": "bullish|bearish|neutral",
  "bias_reason": "<lý do ngắn tiếng Việt>",
  "key_levels": {{
    "critical_support": [<price>],
    "critical_resistance": [<price>]
  }},
  "strategies": {{
    "A": {{
      "label": "Kịch bản A — <tên>",
      "scenario": "<mô tả>",
      "probability": "low|medium|high",
      "direction": "long|short|neutral",
      "entry_zone": {{"from": <price>, "to": <price>}},
      "target_1": <price>,
      "target_2": <price>,
      "stop_loss": <price>,
      "trigger_condition": "<điều kiện>",
      "notes": "<ghi chú>"
    }},
    "B": {{}},
    "C": {{}},
    "D": {{}}
  }},
  "weekly_outlook": "<nhận định 3-5 câu tiếng Việt>",
  "risk_warning": "<cảnh báo rủi ro>"
}}

A=bullish | B=bearish | C=sideways | D=black swan
Chỉ JSON.
"""


def _parse_json_liq(raw: str) -> dict:
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": str(e), "raw_response": raw, "analysis_confidence": "low"}


def _analyze_with_gemini(pil_img, ui_key: str = "") -> dict:
    model, status = _get_gemini_model(override_key=ui_key)
    if model is None:
        return {"error": f"Gemini unavailable: {status}"}
    resp = model.generate_content([_ANALYSIS_PROMPT_LIQ, pil_img])
    return _parse_json_liq(resp.text.strip())


def _analyze_with_claude(pil_img, ui_key: str = "") -> dict:
    client, status = _get_claude_client(override_key=ui_key)
    if client is None:
        return {"error": f"Claude unavailable: {status}"}
    import anthropic as _ant
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": _ANALYSIS_PROMPT_LIQ},
            ],
        }],
    )
    return _parse_json_liq(resp.content[0].text.strip())


def _generate_strategy_gemini(current_price: float, liq_data: dict, ui_key: str = "") -> dict:
    model, status = _get_gemini_model(override_key=ui_key)
    if model is None:
        return {"error": f"Gemini unavailable: {status}"}
    now = datetime.now(timezone.utc)
    week_label = f"Tuần {now.isocalendar()[1]} / {now.year}"
    prompt = _STRATEGY_PROMPT_LIQ.format(
        current_price=current_price,
        week_label=week_label,
        liq_analysis=json.dumps(liq_data, ensure_ascii=False, indent=2),
    )
    resp = model.generate_content(prompt)
    return _parse_json_liq(resp.text.strip())


def _generate_strategy_claude(current_price: float, liq_data: dict, ui_key: str = "") -> dict:
    client, status = _get_claude_client(override_key=ui_key)
    if client is None:
        return {"error": f"Claude unavailable: {status}"}
    now = datetime.now(timezone.utc)
    week_label = f"Tuần {now.isocalendar()[1]} / {now.year}"
    prompt = _STRATEGY_PROMPT_LIQ.format(
        current_price=current_price,
        week_label=week_label,
        liq_analysis=json.dumps(liq_data, ensure_ascii=False, indent=2),
    )
    import anthropic as _ant
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_liq(resp.content[0].text.strip())


# ── UI helpers ────────────────────────────────────────────────────────────────
_STRAT_STYLE_AI = {
    "A": ("#27ae60", "🟢", "#0d1f12"),
    "B": ("#e74c3c", "🔴", "#1f0d0d"),
    "C": ("#f39c12", "🟡", "#1f1a0d"),
    "D": ("#8e44ad", "🟣", "#150d1f"),
}
_BIAS_BADGE_AI = {
    "bullish": ("background:#1a4731;color:#4ade80;", "↑ BULLISH"),
    "bearish": ("background:#4c1919;color:#f87171;", "↓ BEARISH"),
    "neutral": ("background:#2c2c1a;color:#fbbf24;", "→ NEUTRAL"),
}
_STR_ICON = {"low": "▪", "medium": "◈", "high": "🔶", "extreme": "🔥"}

def _hcard(html):
    st.markdown(html, unsafe_allow_html=True)

def _render_ai_analysis_card(data: dict):
    conf = data.get("analysis_confidence", "low")
    conf_c = {"low": "#e74c3c", "medium": "#f39c12", "high": "#27ae60"}.get(conf, "#8b949e")
    cur = data.get("current_price")
    _hcard(f"""<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px;margin-bottom:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <span style="color:#e6edf3;font-weight:600;">📊 Kết quả phân tích · {data.get('data_source_hint','?')} · {data.get('chart_timeframe','?')}</span>
        <span style="background:#0d1117;color:{conf_c};font-size:.7rem;padding:2px 10px;border-radius:20px;border:1px solid {conf_c}44;">Tin cậy: {conf.upper()}</span>
      </div>
    </div>""")
    c1,c2,c3,c4 = st.columns(4)
    for col,lbl,val,color in [
        (c1,"Giá đọc được",f"${cur:,.0f}" if cur else "—","#f7931a"),
        (c2,"Long Liq",f"${data.get('total_long_liq_usd_millions',0):.0f}M","#f87171"),
        (c3,"Short Liq",f"${data.get('total_short_liq_usd_millions',0):.0f}M","#4ade80"),
        (c4,"Xu hướng",data.get("dominant_side","—").upper(),"#fbbf24"),
    ]:
        with col:
            _hcard(f"""<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:11px;text-align:center;margin-bottom:10px;">
              <div style="color:#8b949e;font-size:.68rem;margin-bottom:2px;">{lbl}</div>
              <div style="color:{color};font-size:1rem;font-weight:700;">{val}</div>
            </div>""")
    obs = data.get("raw_observations","")
    if obs:
        _hcard(f"""<div style="padding:10px 13px;background:#0d1117;border-left:3px solid #f7931a;border-radius:0 6px 6px 0;color:#c9d1d9;font-size:.83rem;line-height:1.6;margin-bottom:12px;">💬 {obs}</div>""")
    longs  = data.get("long_liquidation_clusters",[])
    shorts = data.get("short_liquidation_clusters",[])
    if longs or shorts:
        t1,t2 = st.columns(2)
        with t1:
            if longs:
                st.markdown("**🔴 Long Liq Clusters**")
                st.dataframe(pd.DataFrame([{
                    "Price":f"${c.get('price_level',0):,.0f}",
                    "Size":f"${c.get('size_usd_millions',0):.0f}M",
                    "Level":f"{_STR_ICON.get(c.get('strength','low'),'▪')} {c.get('strength','').upper()}",
                } for c in sorted(longs,key=lambda x:x.get("price_level",0),reverse=True)[:8]]),hide_index=True,use_container_width=True)
        with t2:
            if shorts:
                st.markdown("**🟢 Short Liq Clusters**")
                st.dataframe(pd.DataFrame([{
                    "Price":f"${c.get('price_level',0):,.0f}",
                    "Size":f"${c.get('size_usd_millions',0):.0f}M",
                    "Level":f"{_STR_ICON.get(c.get('strength','low'),'▪')} {c.get('strength','').upper()}",
                } for c in sorted(shorts,key=lambda x:x.get("price_level",0))[:8]]),hide_index=True,use_container_width=True)


def _render_ai_strategy_card(label: str, strat: dict):
    color,icon,bg = _STRAT_STYLE_AI.get(label,("#8b949e","⚪","#0d1117"))
    d = strat.get("direction","neutral")
    dl = {"long":"↑ LONG","short":"↓ SHORT","neutral":"→ RANGE"}.get(d,d)
    dc = {"long":"#4ade80","short":"#f87171","neutral":"#fbbf24"}.get(d,"#8b949e")
    p = strat.get("probability","medium")
    pc = {"low":"#8b949e","medium":"#fbbf24","high":"#4ade80"}.get(p,"#8b949e")
    ez = strat.get("entry_zone",{})
    es = f"${ez.get('from',0):,.0f}–${ez.get('to',0):,.0f}" if ez.get("from") else "—"
    t1,t2,sl = strat.get("target_1",0),strat.get("target_2",0),strat.get("stop_loss",0)
    _hcard(f"""
    <div style="background:{bg};border:1px solid {color}44;border-top:3px solid {color};border-radius:10px;padding:14px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:7px;">
        <span style="color:{color};font-weight:700;font-size:.92rem;">{icon} {strat.get('label',f'Chiến thuật {label}')}</span>
        <span style="background:#0d1117;color:{pc};font-size:.68rem;padding:2px 9px;border-radius:20px;border:1px solid {pc}44;">XS: {p.upper()}</span>
      </div>
      <div style="color:#8b949e;font-size:.77rem;font-style:italic;margin-bottom:10px;">{strat.get('scenario','')}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:7px;">
        <div style="background:#0d1117;border-radius:6px;padding:8px;text-align:center;">
          <div style="color:#8b949e;font-size:.62rem;">Hướng</div>
          <div style="color:{dc};font-weight:700;font-size:.83rem;">{dl}</div>
        </div>
        <div style="background:#0d1117;border-radius:6px;padding:8px;text-align:center;">
          <div style="color:#8b949e;font-size:.62rem;">Entry Zone</div>
          <div style="color:#e6edf3;font-weight:600;font-size:.73rem;">{es}</div>
        </div>
        <div style="background:#0d1117;border-radius:6px;padding:8px;text-align:center;">
          <div style="color:#8b949e;font-size:.62rem;">Stop Loss</div>
          <div style="color:#f87171;font-weight:700;font-size:.83rem;">${sl:,.0f}</div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px;">
        <div style="background:#0d1117;border-radius:6px;padding:7px;text-align:center;">
          <div style="color:#8b949e;font-size:.62rem;">Target 1</div>
          <div style="color:#4ade80;font-weight:700;">${t1:,.0f}</div>
        </div>
        <div style="background:#0d1117;border-radius:6px;padding:7px;text-align:center;">
          <div style="color:#8b949e;font-size:.62rem;">Target 2</div>
          <div style="color:#4ade80;font-weight:700;">${t2:,.0f}</div>
        </div>
      </div>
      <div style="background:#0d1117;border-left:2px solid {color};padding:7px 11px;border-radius:0 5px 5px 0;color:#8b949e;font-size:.75rem;">
        <span style="color:{color};font-weight:600;">Trigger: </span>{strat.get('trigger_condition','')}
      </div>
      {"<div style='margin-top:6px;color:#8b949e;font-size:.74rem;'>💡 " + strat.get('notes','') + "</div>" if strat.get('notes') else ""}
    </div>""")


def _render_ai_weekly_strategy(data: dict):
    if "error" in data:
        st.error(f"Lỗi: {data['error']}")
        with st.expander("Raw"): st.code(data.get("raw",""))
        return
    bias = data.get("market_bias","neutral")
    bs,bt = _BIAS_BADGE_AI.get(bias,_BIAS_BADGE_AI["neutral"])
    _hcard(f"""<div style="background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;margin-bottom:14px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:7px;">
        <div>
          <div style="color:#8b949e;font-size:.7rem;margin-bottom:2px;">{data.get('week_label','')}</div>
          <div style="color:#e6edf3;font-size:1.05rem;font-weight:700;">🎯 Chiến thuật tuần mới</div>
        </div>
        <span style="{bs}padding:5px 14px;border-radius:20px;font-weight:700;font-size:.83rem;">{bt}</span>
      </div>
      <div style="color:#c9d1d9;font-size:.83rem;line-height:1.6;">{data.get('bias_reason','')}</div>
    </div>""")
    kl = data.get("key_levels",{})
    s,r = kl.get("critical_support",[]),kl.get("critical_resistance",[])
    if s or r:
        k1,k2 = st.columns(2)
        with k1:
            if s: _hcard(f"""<div style="background:#1a0d0d;border:1px solid #f8717144;border-radius:8px;padding:9px;text-align:center;margin-bottom:10px;"><div style="color:#f87171;font-size:.68rem;margin-bottom:2px;">🔴 Support</div><div style="color:#fca5a5;font-weight:600;font-size:.82rem;">{" · ".join(f"${p:,.0f}" for p in s[:4])}</div></div>""")
        with k2:
            if r: _hcard(f"""<div style="background:#0d1a0d;border:1px solid #4ade8044;border-radius:8px;padding:9px;text-align:center;margin-bottom:10px;"><div style="color:#4ade80;font-size:.68rem;margin-bottom:2px;">🟢 Resistance</div><div style="color:#86efac;font-weight:600;font-size:.82rem;">{" · ".join(f"${p:,.0f}" for p in r[:4])}</div></div>""")
    strategies = data.get("strategies",{})
    a1,a2 = st.columns(2)
    with a1:
        if "A" in strategies: _render_ai_strategy_card("A",strategies["A"])
    with a2:
        if "B" in strategies: _render_ai_strategy_card("B",strategies["B"])
    b1,b2 = st.columns(2)
    with b1:
        if "C" in strategies: _render_ai_strategy_card("C",strategies["C"])
    with b2:
        if "D" in strategies: _render_ai_strategy_card("D",strategies["D"])
    outlook = data.get("weekly_outlook","")
    if outlook:
        _hcard(f"""<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin-top:4px;"><div style="color:#f7931a;font-weight:600;margin-bottom:6px;">📝 Nhận định tuần</div><div style="color:#c9d1d9;font-size:.84rem;line-height:1.7;">{outlook}</div></div>""")
    risk = data.get("risk_warning","")
    if risk:
        _hcard(f"""<div style="margin-top:9px;padding:8px 12px;background:#1c0a0a;border:1px solid #7f1d1d;border-radius:8px;color:#fca5a5;font-size:.76rem;">⚠️ {risk}</div>""")


def _try_import_paste_button():
    """
    Try to import streamlit-paste-button.
    Auto-install nếu chưa có. Trả về (PasteButton hoặc None, bool_ok).
    """
    try:
        from streamlit_paste_button import paste_image_button as _pb
        return _pb, True
    except ImportError:
        pass
    # Auto-install silently
    try:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "streamlit-paste-button", "-q"],
            capture_output=True, timeout=30,
        )
        from streamlit_paste_button import paste_image_button as _pb
        return _pb, True
    except Exception:
        return None, False


def _render_liq_image_inline(current_price: float = 0.0):
    """
    Liquidation Image AI Analyzer — compact inline section phía dưới chart giá BTC.
    Hỗ trợ Gemini FREE + Claude. Upload ảnh → AI phân tích → Chiến thuật A/B/C/D.
    """
    # ── Session state keys riêng cho inline section ───────────────────────────
    for k in ("_inline_liq_analysis", "_inline_liq_strategy", "_inline_pil_cache",
              "_inline_strat_saved_at"):
        if k not in st.session_state:
            st.session_state[k] = None

    # Khôi phục chiến thuật từ disk nếu session chưa có (sau restart/reconnect)
    if (st.session_state._inline_liq_strategy is None
            and st.session_state._inline_liq_analysis is None):
        _disk = _load_strategy_from_disk()
        if _disk.get("strategy") and _disk.get("source") == "image":
            st.session_state._inline_liq_analysis  = _disk.get("analysis")
            st.session_state._inline_liq_strategy  = _disk.get("strategy")
            st.session_state._inline_strat_saved_at = _disk.get("saved_at")

    with st.expander("🖼 Liquidation Image AI Analyzer  —  Paste ảnh heatmap → AI phân tích → Chiến thuật A/B/C/D", expanded=False):

        # ── Header ────────────────────────────────────────────────────────────
        _hcard("""
        <div style="margin-bottom:12px;padding:10px 14px;background:#0d1117;
             border:1px solid #30363d;border-radius:10px;">
          <div style="color:#e6edf3;font-size:.98rem;font-weight:700;margin-bottom:3px;">
            🖼 Liquidation Image AI Analyzer
            <span style="background:#1a4020;color:#4ade80;font-size:.6rem;
                  padding:2px 8px;border-radius:10px;margin-left:8px;font-weight:600;">
              Gemini FREE
            </span>
            <span style="background:#1e1a40;color:#818cf8;font-size:.6rem;
                  padding:2px 8px;border-radius:10px;margin-left:4px;font-weight:600;">
              Claude
            </span>
          </div>
          <div style="color:#8b949e;font-size:.79rem;">
            Paste ảnh liquidation heatmap → AI đọc phân tích → Chiến thuật A/B/C/D cho tuần mới
          </div>
        </div>""")

        # ── Engine selector + key status ──────────────────────────────────────
        eng_col, key_col = st.columns([2, 2])
        with eng_col:
            _inline_engine = st.radio(
                "🤖 AI Engine",
                options=["Gemini (FREE)", "Claude (có phí)"],
                index=0,
                horizontal=True,
                key="inline_engine_choice",
            )
            _use_gemini_inline = _inline_engine.startswith("Gemini")

        with key_col:
            _hcard('<div style="color:#8b949e;font-size:.76rem;margin-bottom:5px;">🔑 Nhập API Key (lưu trong session)</div>')
            _ui_gemini_key = st.text_input(
                "Gemini API Key",
                value=st.session_state.get("_ui_gemini_key", ""),
                type="password",
                placeholder="AIzaSy...",
                key="inline_gemini_key_input",
                label_visibility="collapsed",
            )
            st.caption("Gemini key — [Lấy miễn phí](https://aistudio.google.com/apikey)")
            _ui_claude_key = st.text_input(
                "Claude API Key",
                value=st.session_state.get("_ui_claude_key", ""),
                type="password",
                placeholder="sk-ant-api03-...",
                key="inline_claude_key_input",
                label_visibility="collapsed",
            )
            st.caption("Claude key — [console.anthropic.com](https://console.anthropic.com)")
            # Lưu vào session state để giữ qua rerun
            if _ui_gemini_key:
                st.session_state._ui_gemini_key = _ui_gemini_key
            if _ui_claude_key:
                st.session_state._ui_claude_key = _ui_claude_key
            # Status badge
            _active_key = _ui_gemini_key if _use_gemini_inline else _ui_claude_key
            _key_prefix = "AIza" if _use_gemini_inline else "sk-ant-"
            if _active_key and _active_key.startswith(_key_prefix):
                _hcard('<div style="background:#0d1f12;border:1px solid #27ae6044;border-radius:6px;'
                       'padding:5px 10px;font-size:.72rem;color:#4ade80;margin-top:4px;">✅ Key hợp lệ</div>')
            elif _active_key:
                _hcard('<div style="background:#2d1a00;border:1px solid #f59e0b55;border-radius:6px;'
                       'padding:5px 10px;font-size:.72rem;color:#fbbf24;margin-top:4px;">⚠️ Key có vẻ không đúng định dạng</div>')

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Step 1: Upload ────────────────────────────────────────────────────
        _hcard('<div style="color:#f7931a;font-weight:600;font-size:.79rem;margin-bottom:6px;">'
               '📤 Bước 1 — Upload ảnh</div>')

        up_col, prev_col = st.columns([1, 1], gap="medium")

        active_pil_inline = None

        with up_col:
            _hcard("""
            <div style="background:#0d1117;border:2px dashed #f7931a55;border-radius:10px;
                 padding:18px 12px;text-align:center;margin-bottom:8px;">
              <div style="font-size:1.5rem;margin-bottom:4px;">📊</div>
              <div style="color:#8b949e;font-size:.74rem;line-height:1.6;">
                Kéo thả hoặc click để chọn <b style="color:#f7931a;">tối đa 10 ảnh</b><br>
                <span style="color:#6e7681;">PNG · JPG · WEBP · từ Coinglass, Hyblock, Velo, v.v.</span>
              </div>
            </div>""")
            uploaded_inline_files = st.file_uploader(
                "Chọn ảnh liquidation heatmap (tối đa 10)",
                type=["png", "jpg", "jpeg", "webp"],
                key="inline_liq_uploader",
                accept_multiple_files=True,
                label_visibility="collapsed",
            )

            active_pils_inline = []   # list of PIL images
            if uploaded_inline_files:
                try:
                    from PIL import Image as _PILImg
                    for _uf in uploaded_inline_files[:10]:
                        _uf.seek(0)
                        active_pils_inline.append(_PILImg.open(io.BytesIO(_uf.read())))
                    st.session_state._inline_pil_cache = active_pils_inline[0] if active_pils_inline else None
                    active_pil_inline = active_pils_inline[0] if active_pils_inline else None
                    if len(active_pils_inline) > 1:
                        st.caption(f"✅ Đã nhận {len(active_pils_inline)} ảnh")
                except Exception as _e:
                    st.error(f"Không đọc được ảnh: {_e}")
            elif st.session_state._inline_pil_cache is not None:
                active_pil_inline = st.session_state._inline_pil_cache
                active_pils_inline = [active_pil_inline]

            # Action buttons
            if active_pils_inline:
                st.markdown("<br>", unsafe_allow_html=True)
                _n_imgs = len(active_pils_inline)
                btn_a, btn_b = st.columns(2)
                with btn_a:
                    do_analyze_inline = st.button(
                        f"🔍 Phân tích {_n_imgs} ảnh",
                        key="inline_btn_analyze",
                        type="primary",
                        use_container_width=True,
                    )
                with btn_b:
                    do_full_inline = st.button(
                        f"⚡ Phân tích + Chiến thuật ({_n_imgs} ảnh)",
                        key="inline_btn_full",
                        use_container_width=True,
                    )

                if do_analyze_inline or do_full_inline:
                    _engine_label = "Gemini" if _use_gemini_inline else "Claude"
                    _pass_key = st.session_state.get("_ui_gemini_key", "") if _use_gemini_inline else st.session_state.get("_ui_claude_key", "")
                    with st.spinner(f"🤖 {_engine_label} đang đọc {_n_imgs} ảnh liquidation…"):
                        _all_analyses = []
                        for _img_idx, _pil_img in enumerate(active_pils_inline):
                            st.spinner(f"  Ảnh {_img_idx+1}/{_n_imgs}…")
                            _a = (_analyze_with_gemini(_pil_img, ui_key=_pass_key) if _use_gemini_inline
                                  else _analyze_with_claude(_pil_img, ui_key=_pass_key))
                            _all_analyses.append(_a)
                        _ana = _merge_liq_analyses(_all_analyses)
                    st.session_state._inline_liq_analysis = _ana
                    st.session_state._inline_liq_strategy = None
                    if do_full_inline and "error" not in _ana:
                        with st.spinner("📋 Đang tạo chiến thuật A/B/C/D…"):
                            _strat = (_generate_strategy_gemini(current_price, _ana, ui_key=_pass_key) if _use_gemini_inline
                                      else _generate_strategy_claude(current_price, _ana, ui_key=_pass_key))
                            st.session_state._inline_liq_strategy = _strat
                            # ── Lưu cứng vào disk ──
                            if "error" not in _strat:
                                _save_strategy_to_disk(_ana, _strat, source="image")
                                st.session_state._inline_strat_saved_at = datetime.now(timezone.utc).isoformat()
                    st.rerun()

                if st.button("🗑 Reset & Phân tích lại", key="inline_btn_reset", use_container_width=True):
                    st.session_state._inline_liq_analysis = None
                    st.session_state._inline_liq_strategy = None
                    st.session_state._inline_pil_cache = None
                    st.session_state._inline_strat_saved_at = None
                    _clear_strategy_from_disk()
                    st.rerun()

        with prev_col:
            if active_pils_inline:
                # Hiển thị preview ảnh đầu tiên + count
                st.image(active_pils_inline[0], caption=f"Preview ảnh 1/{len(active_pils_inline)}", use_container_width=True)
                if len(active_pils_inline) > 1:
                    _hcard(f'<div style="color:#8b949e;font-size:.74rem;text-align:center;margin-top:4px;">'
                           f'+{len(active_pils_inline)-1} ảnh nữa đã được tải</div>')

        # ── Step 2: Kết quả phân tích ─────────────────────────────────────────
        _ana_result = st.session_state._inline_liq_analysis
        if _ana_result is not None:
            st.markdown("---")
            _hcard('<div style="color:#f7931a;font-weight:600;font-size:.8rem;margin-bottom:8px;">'
                   '📊 Bước 2 — Kết quả phân tích AI</div>')

            if "error" in _ana_result:
                st.error(f"Lỗi phân tích: {_ana_result['error']}")
            else:
                # Summary metrics
                conf = _ana_result.get("analysis_confidence", "low")
                conf_c = {"low": "#e74c3c", "medium": "#f39c12", "high": "#27ae60"}.get(conf, "#8b949e")
                cur_p = _ana_result.get("current_price")

                _hcard(f"""
                <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;
                     padding:12px 14px;margin-bottom:10px;">
                  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                    <span style="color:#e6edf3;font-weight:600;font-size:.87rem;">
                      Kết quả phân tích ảnh · {_ana_result.get('data_source_hint','?')}
                    </span>
                    <span style="background:#0d1117;color:{conf_c};font-size:.68rem;
                          padding:2px 10px;border-radius:20px;border:1px solid {conf_c}44;">
                      Độ tin cậy: {conf.upper()}
                    </span>
                  </div>
                </div>""")

                mc1, mc2, mc3, mc4 = st.columns(4)
                for _col, _lbl, _val, _clr in [
                    (mc1, "Giá đọc được", f"${cur_p:,.0f}" if cur_p else "—", "#f7931a"),
                    (mc2, "Tổng Long Liq", f"${_ana_result.get('total_long_liq_usd_millions', 0):.0f}M", "#f87171"),
                    (mc3, "Tổng Short Liq", f"${_ana_result.get('total_short_liq_usd_millions', 0):.0f}M", "#4ade80"),
                    (mc4, "Xu hướng", _ana_result.get("dominant_side", "—").upper(), "#fbbf24"),
                ]:
                    with _col:
                        _hcard(f"""<div style="background:#0d1117;border:1px solid #21262d;
                             border-radius:8px;padding:10px;text-align:center;">
                          <div style="color:#8b949e;font-size:.67rem;margin-bottom:2px;">{_lbl}</div>
                          <div style="color:{_clr};font-size:.95rem;font-weight:700;">{_val}</div>
                        </div>""")

                obs = _ana_result.get("raw_observations", "")
                if obs:
                    _hcard(f"""<div style="margin-top:8px;padding:9px 13px;background:#0d1117;
                         border-left:3px solid #f7931a;border-radius:0 6px 6px 0;
                         color:#c9d1d9;font-size:.81rem;line-height:1.6;">💬 {obs}</div>""")

                # Liq cluster tables
                longs_i  = _ana_result.get("long_liquidation_clusters", [])
                shorts_i = _ana_result.get("short_liquidation_clusters", [])
                if longs_i or shorts_i:
                    st.markdown("<br>", unsafe_allow_html=True)
                    tc1, tc2 = st.columns(2)
                    with tc1:
                        if longs_i:
                            _hcard('<div style="color:#f87171;font-weight:600;font-size:.8rem;'
                                   'margin-bottom:5px;">🔴 Long Liq Clusters (dưới giá)</div>')
                            st.dataframe(pd.DataFrame([{
                                "Price": f"${c.get('price_level', 0):,.0f}",
                                "Size":  f"${c.get('size_usd_millions', 0):.0f}M",
                                "Level": f"{_STR_ICON.get(c.get('strength','low'),'▪')} {c.get('strength','').upper()}",
                            } for c in sorted(longs_i, key=lambda x: x.get("price_level", 0), reverse=True)[:6]]),
                            hide_index=True, use_container_width=True)
                    with tc2:
                        if shorts_i:
                            _hcard('<div style="color:#4ade80;font-weight:600;font-size:.8rem;'
                                   'margin-bottom:5px;">🟢 Short Liq Clusters (trên giá)</div>')
                            st.dataframe(pd.DataFrame([{
                                "Price": f"${c.get('price_level', 0):,.0f}",
                                "Size":  f"${c.get('size_usd_millions', 0):.0f}M",
                                "Level": f"{_STR_ICON.get(c.get('strength','low'),'▪')} {c.get('strength','').upper()}",
                            } for c in sorted(shorts_i, key=lambda x: x.get("price_level", 0))[:6]]),
                            hide_index=True, use_container_width=True)

        # ── Step 3: Chiến thuật A/B/C/D ───────────────────────────────────────
        _strat_result = st.session_state._inline_liq_strategy
        if _strat_result is not None:
            st.markdown("---")
            _saved_at = st.session_state.get("_inline_strat_saved_at")
            _disk_data = _load_strategy_from_disk()
            _from_disk = (_saved_at is None and _disk_data.get("strategy") is not None)
            if _from_disk:
                _disk_ts = _disk_data.get("saved_at", "")
                try:
                    _disk_dt = datetime.fromisoformat(_disk_ts).strftime("%d/%m/%Y %H:%M UTC")
                except Exception:
                    _disk_dt = _disk_ts
                _hcard(f'<div style="background:#0d1f1a;border:1px solid #4ade8055;border-radius:8px;'
                       f'padding:8px 13px;margin-bottom:8px;font-size:.76rem;color:#86efac;">'
                       f'💾 Chiến thuật được khôi phục từ disk — lưu lúc {_disk_dt} · '
                       f'<span style="color:#6e7681;">Nhấn "Reset & Phân tích lại" để xóa và phân tích mới</span></div>')
            else:
                _hcard('<div style="color:#f7931a;font-weight:600;font-size:.8rem;margin-bottom:8px;">'
                       '🎯 Bước 3 — Chiến thuật tuần A / B / C / D</div>')
            _render_ai_weekly_strategy(_strat_result)


def render_liq_image_tab(current_price: float = 0.0, estimated_clusters=None):
    """
    Render tab Liq Image AI — Gemini FREE + Claude.
    Input: (1) Paste button — Ctrl+V clipboard  (2) File uploader
    """
    for k in ("_liq_ai_analysis", "_liq_ai_strategy", "_liq_ai_engine",
              "_liq_pil_cache", "_liq_strat_saved_at"):
        if k not in st.session_state:
            st.session_state[k] = None

    # Khôi phục chiến thuật từ disk nếu session chưa có
    if (st.session_state._liq_ai_strategy is None
            and st.session_state._liq_ai_analysis is None):
        _disk2 = _load_strategy_from_disk()
        if _disk2.get("strategy") and _disk2.get("source") == "image":
            st.session_state._liq_ai_analysis   = _disk2.get("analysis")
            st.session_state._liq_ai_strategy   = _disk2.get("strategy")
            st.session_state._liq_strat_saved_at = _disk2.get("saved_at")

    # ── Header ────────────────────────────────────────────────────────────────
    _hcard("""
    <div style="margin-bottom:14px;">
      <div style="color:#e6edf3;font-size:1.12rem;font-weight:700;margin-bottom:4px;">
        🖼 Liquidation Image AI Analyzer
        <span style="background:#1a4020;color:#4ade80;font-size:.62rem;
              padding:2px 8px;border-radius:10px;margin-left:8px;font-weight:600;">
          Gemini FREE · Claude
        </span>
        <span style="background:#1e1a40;color:#818cf8;font-size:.62rem;
              padding:2px 8px;border-radius:10px;margin-left:4px;font-weight:600;">
          📋 Clipboard Paste
        </span>
      </div>
      <div style="color:#8b949e;font-size:.81rem;">
        Paste ảnh (Ctrl+V) hoặc Upload từ Coinglass · Hyblock · Velo
        → AI phân tích → Overlay chart → Chiến thuật A/B/C/D
      </div>
    </div>""")

    # ── Engine selector ───────────────────────────────────────────────────────
    ecol1, ecol2 = st.columns([2, 2])
    with ecol1:
        engine = st.radio(
            "🤖 AI Engine",
            options=["Gemini (FREE)", "Claude (có phí)"],
            index=0, horizontal=True,
            key="liq_ai_engine_choice",
        )
        use_gemini = engine.startswith("Gemini")

    with ecol2:
        if use_gemini:
            g_ok, g_key_src = False, ""
            try:
                gk = st.secrets["gemini"]["api_key"]
                g_ok = bool(gk and gk.startswith("AIza"))
                if g_ok: g_key_src = "secrets.toml"
            except Exception:
                pass
            if not g_ok:
                gk2 = os.environ.get("GEMINI_API_KEY", "").strip()
                g_ok = bool(gk2 and gk2.startswith("AIza"))
                if g_ok: g_key_src = "Replit / env"
            if g_ok:
                _hcard(f'<div style="background:#0d1f12;border:1px solid #27ae6044;border-radius:8px;'
                       f'padding:7px 12px;margin-top:6px;font-size:.78rem;color:#4ade80;">'
                       f'✅ Gemini key OK ({g_key_src})</div>')
            else:
                _hcard('<div style="background:#1c1400;border:1px solid #f59e0b55;border-radius:8px;'
                       'padding:8px 12px;margin-top:6px;font-size:.77rem;color:#fbbf24;">'
                       '⚙️ Chưa có Gemini key — '
                       '<a href="https://aistudio.google.com/apikey" target="_blank" style="color:#f7931a;">'
                       'Lấy miễn phí</a><br>'
                       'Lưu: <code>[gemini] api_key = "AIza..."</code> trong secrets.toml</div>')
        else:
            c_ok = False
            try:
                ck = st.secrets["claude"]["api_key"]
                c_ok = bool(ck and ck.startswith("sk-ant-"))
            except Exception:
                c_ok = bool(os.environ.get("ANTHROPIC_API_KEY", ""))
            if c_ok:
                _hcard('<div style="background:#0d1f12;border:1px solid #27ae6044;border-radius:8px;'
                       'padding:7px 12px;margin-top:6px;font-size:.78rem;color:#4ade80;">✅ Claude key OK</div>')
            else:
                _hcard('<div style="background:#1c1400;border:1px solid #f59e0b55;border-radius:8px;'
                       'padding:8px 12px;margin-top:6px;font-size:.77rem;color:#fbbf24;">'
                       '⚙️ Chưa có Claude key — '
                       '<a href="https://console.anthropic.com" target="_blank" style="color:#f7931a;">'
                       'console.anthropic.com</a><br>'
                       'Lưu: <code>[claude] api_key = "sk-ant-..."</code></div>')

    st.markdown("<br>", unsafe_allow_html=True)

    # ═════════════════════════════════════════════════════════════════════════
    # INPUT ZONE — 2 cột: PASTE (trái) | UPLOAD (phải)
    # ═════════════════════════════════════════════════════════════════════════
    col_paste, col_upload = st.columns([1, 1], gap="medium")

    active_pil = None   # PIL image được chọn (paste ưu tiên hơn upload)
    active_label = ""

    # ── CỘT TRÁI: CLIPBOARD PASTE ────────────────────────────────────────────
    with col_paste:
        _hcard("""
        <div style="color:#818cf8;font-weight:600;font-size:.82rem;margin-bottom:6px;">
          📋 Cách 1 — Paste từ Clipboard
        </div>
        <div style="color:#6e7681;font-size:.74rem;margin-bottom:8px;">
          Chụp màn hình (PrtSc / Cmd+Shift+4) → nhấn nút bên dưới → Ctrl+V
        </div>""")

        paste_btn, paste_ok = _try_import_paste_button()

        if paste_ok and paste_btn is not None:
            # streamlit-paste-button hoạt động: render nút paste
            paste_result = paste_btn(
                label="📋  Click đây rồi Ctrl+V",
                text_color="#818cf8",
                background_color="#161b22",
                hover_background_color="#1e1a40",
                errors="ignore",
                key="liq_paste_btn",
            )
            if paste_result is not None and paste_result.image_data is not None:
                # Lưu PIL vào session để dùng sau khi rerun
                st.session_state._liq_pil_cache = ("paste", paste_result.image_data)
                _hcard('<div style="color:#4ade80;font-size:.78rem;margin-top:4px;">'
                       '✅ Ảnh đã nhận từ clipboard!</div>')
        else:
            # Fallback: hướng dẫn copy file
            _hcard("""
            <div style="background:#0d1117;border:2px dashed #818cf855;border-radius:10px;
                 padding:16px;text-align:center;min-height:90px;">
              <div style="font-size:1.8rem;margin-bottom:6px;">📋</div>
              <div style="color:#6e7681;font-size:.76rem;line-height:1.6;">
                Để dùng tính năng paste, cài thêm:<br>
                <code style="color:#a5b4fc;">pip install streamlit-paste-button</code><br>
                rồi restart app
              </div>
            </div>""")

        # Hiển thị preview nếu đang có ảnh paste trong cache
        if (st.session_state._liq_pil_cache is not None
                and st.session_state._liq_pil_cache[0] == "paste"):
            cached_pil = st.session_state._liq_pil_cache[1]
            st.image(cached_pil, caption="📋 Clipboard", use_container_width=True)
            active_pil = cached_pil
            active_label = "📋 Clipboard Paste"
            if st.button("🗑 Xóa ảnh paste", key="liq_clear_paste",
                         use_container_width=True):
                st.session_state._liq_pil_cache = None
                st.rerun()

    # ── CỘT PHẢI: FILE UPLOADER (multi-image, tối đa 10) ─────────────────────
    with col_upload:
        _hcard("""
        <div style="color:#f7931a;font-weight:600;font-size:.82rem;margin-bottom:6px;">
          📤 Cách 2 — Upload tối đa 10 ảnh
        </div>
        <div style="color:#6e7681;font-size:.74rem;margin-bottom:8px;">
          PNG · JPG · WEBP từ Coinglass, Hyblock, Velo...
        </div>""")

        uploaded_files = st.file_uploader(
            "Chọn file ảnh (tối đa 10)",
            type=["png", "jpg", "jpeg", "webp"],
            key="liq_img_uploader_main",
            accept_multiple_files=True,
            label_visibility="collapsed",
        )

        active_upload_pils = []
        if uploaded_files:
            try:
                from PIL import Image as _PILImage
                for _uf in uploaded_files[:10]:
                    _uf.seek(0)
                    active_upload_pils.append(_PILImage.open(io.BytesIO(_uf.read())))
                # Hiển thị preview ảnh đầu tiên
                st.image(active_upload_pils[0],
                         caption=f"📄 {uploaded_files[0].name}{' (+'+str(len(active_upload_pils)-1)+' ảnh)' if len(active_upload_pils)>1 else ''}",
                         use_container_width=True)
                # Upload chỉ được dùng nếu không có paste
                if active_pil is None:
                    active_pil   = active_upload_pils[0]
                    active_label = f"📤 {len(active_upload_pils)} ảnh ({uploaded_files[0].name}...)"
            except ImportError:
                st.error("❌ Cần cài: `pip install pillow`")
            except Exception as _eu:
                st.error(f"❌ Không đọc được: {_eu}")

    # Gộp tất cả pil sources: clipboard + upload
    _all_active_pils = []
    if (st.session_state._liq_pil_cache is not None
            and st.session_state._liq_pil_cache[0] == "paste"):
        _all_active_pils.append(st.session_state._liq_pil_cache[1])
    _all_active_pils.extend(active_upload_pils)

    # ── Current price badge ───────────────────────────────────────────────────
    if _all_active_pils:
        _n_total = len(_all_active_pils)
        _hcard(f"""
        <div style="display:flex;gap:12px;align-items:center;margin:10px 0;flex-wrap:wrap;">
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;
               padding:8px 14px;font-size:.8rem;">
            <span style="color:#8b949e;">Nguồn: </span>
            <span style="color:#c9d1d9;font-weight:600;">{active_label or '—'}</span>
            <span style="color:#f7931a;margin-left:10px;font-weight:600;">{_n_total} ảnh</span>
          </div>
          <div style="background:#0d1117;border:1px solid #f7931a44;border-radius:8px;
               padding:8px 14px;font-size:.8rem;">
            <span style="color:#8b949e;">Giá hiện tại: </span>
            <span style="color:#f7931a;font-weight:700;">${current_price:,.0f}</span>
          </div>
        </div>""")

    # ═════════════════════════════════════════════════════════════════════════
    # ACTION BUTTONS
    # ═════════════════════════════════════════════════════════════════════════
    if _all_active_pils:
        _n_imgs_tab = len(_all_active_pils)
        st.markdown("<br>", unsafe_allow_html=True)
        _hcard('<div style="color:#f7931a;font-weight:600;font-size:.82rem;margin-bottom:7px;">'
               '⚡ Bước 2 — Chạy AI phân tích</div>')

        ba1, ba2, ba3 = st.columns([1, 1.5, 0.8])
        with ba1:
            do_analyze = st.button(f"🔍 Phân tích {_n_imgs_tab} ảnh",
                                   type="primary", use_container_width=True)
        with ba2:
            do_full = st.button(f"⚡ Phân tích + Chiến thuật A/B/C/D ({_n_imgs_tab} ảnh)",
                                use_container_width=True)
        with ba3:
            do_reset = st.button("🔄 Reset & Phân tích lại", use_container_width=True,
                                 key="liq_reset_top")

        if do_reset:
            st.session_state._liq_ai_analysis   = None
            st.session_state._liq_ai_strategy   = None
            st.session_state._liq_pil_cache     = None
            st.session_state._liq_strat_saved_at = None
            _clear_strategy_from_disk()
            st.rerun()

        if do_analyze or do_full:
            with st.spinner(f"🤖 {'Gemini' if use_gemini else 'Claude'} đang đọc {_n_imgs_tab} ảnh…"):
                _analyses_tab = []
                for _pi in _all_active_pils:
                    _a = (_analyze_with_gemini(_pi) if use_gemini
                          else _analyze_with_claude(_pi))
                    _analyses_tab.append(_a)
                analysis = _merge_liq_analyses(_analyses_tab)
            st.session_state._liq_ai_analysis  = analysis
            st.session_state._liq_ai_engine    = "gemini" if use_gemini else "claude"
            st.session_state._liq_ai_strategy  = None
            st.session_state._liq_strat_saved_at = None

            if do_full and "error" not in analysis:
                with st.spinner("📋 Đang tạo chiến thuật A/B/C/D…"):
                    _tab_strat = (_generate_strategy_gemini(current_price, analysis) if use_gemini
                                  else _generate_strategy_claude(current_price, analysis))
                    st.session_state._liq_ai_strategy = _tab_strat
                    if "error" not in _tab_strat:
                        _save_strategy_to_disk(analysis, _tab_strat, source="image")
                        st.session_state._liq_strat_saved_at = datetime.now(timezone.utc).isoformat()
            st.rerun()

    # ═════════════════════════════════════════════════════════════════════════
    # KẾT QUẢ PHÂN TÍCH
    # ═════════════════════════════════════════════════════════════════════════
    if st.session_state._liq_ai_analysis:
        st.markdown("---")
        _hcard('<div style="color:#f7931a;font-weight:600;font-size:.83rem;margin-bottom:9px;">'
               '📊 Bước 3 — Kết quả phân tích AI</div>')
        analysis = st.session_state._liq_ai_analysis

        if "error" in analysis:
            st.error(f"Lỗi: {analysis['error']}")
            with st.expander("Raw response"):
                st.code(analysis.get("raw_response", ""))
        else:
            _render_ai_analysis_card(analysis)

            # Overlay notice
            _n_long  = len(analysis.get("long_liquidation_clusters",  []))
            _n_short = len(analysis.get("short_liquidation_clusters", []))
            if _n_long + _n_short > 0:
                _hcard(f"""
                <div style="background:#0d1f1a;border:1px solid #4ade8055;border-radius:8px;
                     padding:10px 14px;font-size:.79rem;color:#86efac;margin-bottom:10px;">
                  🎯 <b>AI Overlay đã sẵn sàng!</b>
                  Tìm thấy <b>{_n_long}</b> Long clusters · <b>{_n_short}</b> Short clusters.<br>
                  <span style="color:#6e7681;">→ Chuyển sang tab
                  <b>🎯 Options &amp; OI</b> để xem overlay trên chart.</span>
                </div>""")

            if st.session_state._liq_ai_strategy is None:
                eng = st.session_state._liq_ai_engine or "gemini"
                if st.button("📋 Tạo chiến thuật A/B/C/D cho tuần mới",
                             type="primary", key="liq_gen_strategy"):
                    with st.spinner("📋 Đang tạo chiến thuật…"):
                        _tab_strat2 = (_generate_strategy_gemini(current_price, analysis)
                                       if eng == "gemini"
                                       else _generate_strategy_claude(current_price, analysis))
                        st.session_state._liq_ai_strategy = _tab_strat2
                        if "error" not in _tab_strat2:
                            _save_strategy_to_disk(analysis, _tab_strat2, source="image")
                            st.session_state._liq_strat_saved_at = datetime.now(timezone.utc).isoformat()
                    st.rerun()

            with st.expander("🔎 Raw JSON phân tích", expanded=False):
                st.json(analysis)

    # ═════════════════════════════════════════════════════════════════════════
    # CHIẾN THUẬT A/B/C/D
    # ═════════════════════════════════════════════════════════════════════════
    if st.session_state._liq_ai_strategy:
        st.markdown("---")
        # Badge nếu khôi phục từ disk
        _ts2 = st.session_state.get("_liq_strat_saved_at")
        _disk3 = _load_strategy_from_disk()
        _from_disk2 = (_ts2 is None and _disk3.get("strategy") is not None)
        if _from_disk2:
            try:
                _dt3 = datetime.fromisoformat(_disk3.get("saved_at","")).strftime("%d/%m/%Y %H:%M UTC")
            except Exception:
                _dt3 = _disk3.get("saved_at","")
            _hcard(f'<div style="background:#0d1f1a;border:1px solid #4ade8055;border-radius:8px;'
                   f'padding:8px 13px;margin-bottom:8px;font-size:.76rem;color:#86efac;">'
                   f'💾 Chiến thuật được khôi phục từ disk — lưu lúc {_dt3} · '
                   f'<span style="color:#6e7681;">Nhấn "Reset & Phân tích lại" để phân tích mới</span></div>')
        else:
            _hcard('<div style="color:#f7931a;font-weight:600;font-size:.83rem;margin-bottom:9px;">'
                   '🎯 Bước 4 — Chiến thuật tuần A / B / C / D</div>')
        _render_ai_weekly_strategy(st.session_state._liq_ai_strategy)

        ce, cr = st.columns([3, 1])
        with ce:
            with st.expander("🔎 Raw JSON chiến thuật", expanded=False):
                st.json(st.session_state._liq_ai_strategy)
        with cr:
            if st.button("🔄 Reset & Phân tích lại", use_container_width=True,
                         key="liq_reset_bot"):
                st.session_state._liq_ai_analysis    = None
                st.session_state._liq_ai_strategy    = None
                st.session_state._liq_pil_cache      = None
                st.session_state._liq_strat_saved_at = None
                _clear_strategy_from_disk()
                st.rerun()

    # ═════════════════════════════════════════════════════════════════════════
    # HƯỚNG DẪN (chỉ hiện khi chưa có ảnh và kết quả)
    # ═════════════════════════════════════════════════════════════════════════
    if st.session_state._liq_ai_analysis is None and not _all_active_pils:
        _hcard("""
        <div style="background:#161b22;border:1px solid #21262d;border-radius:10px;
             padding:18px;margin-top:12px;">
          <div style="color:#e6edf3;font-weight:600;margin-bottom:10px;">📖 Hướng dẫn</div>
          <div style="color:#8b949e;font-size:.81rem;line-height:2.0;">

            <span style="color:#818cf8;font-weight:600;">📋 Cách 1 — Paste clipboard (nhanh nhất):</span><br>
            &nbsp;&nbsp;① Vào <a href="https://coinglass.com/LiquidationData" target="_blank"
               style="color:#f7931a;">Coinglass</a> ·
               <a href="https://app.hyblock.io" target="_blank" style="color:#f7931a;">Hyblock</a> ·
               <a href="https://velo.xyz" target="_blank" style="color:#f7931a;">Velo</a>
               → chụp màn hình chart liquidation<br>
            &nbsp;&nbsp;② Nhấn nút <b style="color:#818cf8;">📋 Click đây rồi Ctrl+V</b>
               ở cột trái bên trên<br>
            &nbsp;&nbsp;③ Nhấn <b style="color:#f7931a;">⚡ Phân tích + Chiến thuật A/B/C/D</b><br><br>

            <span style="color:#f7931a;font-weight:600;">📤 Cách 2 — Upload file:</span><br>
            &nbsp;&nbsp;① Lưu ảnh liquidation ra file PNG/JPG<br>
            &nbsp;&nbsp;② Upload qua ô bên phải → nhấn phân tích<br><br>

            <span style="color:#4ade80;font-weight:600;">🎯 Kết quả bạn nhận được:</span><br>
            &nbsp;&nbsp;• AI đọc ảnh → trích xuất clusters long/short theo giá<br>
            &nbsp;&nbsp;• Overlay lên chart <b style="color:#c9d1d9;">Estimated Liquidation</b>
              trong tab 🎯 Options &amp; OI<br>
            &nbsp;&nbsp;• 4 chiến thuật A/B/C/D với entry · target 1 · target 2 · stop loss<br><br>

            <span style="color:#c9d1d9;font-weight:600;">⚙️ Cài đặt API key</span>
            trong <code>.streamlit/secrets.toml</code>:<br>
            <code style="color:#86efac;font-size:.78rem;">
              [gemini]<br>
              api_key = "AIza..."   # miễn phí — aistudio.google.com/apikey<br><br>
              [claude]<br>
              api_key = "sk-ant-..."  # trả phí — console.anthropic.com
            </code>

          </div>
        </div>""")

    # ═════════════════════════════════════════════════════════════════════════
    # LỊCH SỬ PHÂN TÍCH AI — 14 ngày gần nhất
    # ═════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    with st.expander("📅 Lịch sử phân tích AI — 14 ngày gần nhất", expanded=False):
        _history = _load_all_history(days=_CACHE_TTL_DAYS)
        if not _history:
            st.info("Chưa có lịch sử phân tích. Hãy upload ảnh và chạy AI phân tích để lưu.")
        else:
            st.markdown(
                f'<div style="color:#8b949e;font-size:.78rem;margin-bottom:10px;">'
                f'📂 Tìm thấy <b style="color:#e6edf3;">{len(_history)}</b> lần phân tích'
                f' · Dữ liệu được giữ tối đa <b style="color:#f7931a;">14 ngày</b>'
                f' · Lưu tại: <code>btc_ai_history/</code></div>',
                unsafe_allow_html=True,
            )
            for _idx, _entry in enumerate(_history):
                try:
                    _saved_raw = _entry.get("saved_at", "")
                    try:
                        _saved_dt = datetime.fromisoformat(_saved_raw)
                        _saved_label = _saved_dt.strftime("%d/%m/%Y %H:%M UTC")
                    except Exception:
                        _saved_label = _saved_raw or "Unknown"

                    _src  = _entry.get("source", "?").upper()
                    _ana  = _entry.get("analysis", {}) or {}
                    _strat = _entry.get("strategy", {}) or {}

                    _cp   = _ana.get("current_price", 0)
                    _conf = _ana.get("analysis_confidence", "?").upper()
                    _dom  = _ana.get("dominant_side", "?").upper()
                    _bias = _strat.get("market_bias", "?").upper()
                    _n_long  = len(_ana.get("long_liquidation_clusters",  []))
                    _n_short = len(_ana.get("short_liquidation_clusters", []))

                    _dom_color  = {"LONG": "#f87171", "SHORT": "#4ade80", "BALANCED": "#fbbf24"}.get(_dom, "#8b949e")
                    _bias_color = {"BULLISH": "#4ade80", "BEARISH": "#f87171", "NEUTRAL": "#fbbf24"}.get(_bias, "#8b949e")
                    _conf_color = {"HIGH": "#4ade80", "MEDIUM": "#fbbf24", "LOW": "#f87171"}.get(_conf, "#8b949e")

                    _hcard(f"""
                    <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;
                                padding:10px 14px;margin-bottom:8px;display:flex;
                                flex-wrap:wrap;gap:14px;align-items:center;">
                      <div style="color:#8b949e;font-size:.72rem;min-width:130px;">
                        🕐 {_saved_label}
                      </div>
                      <div style="color:#c9d1d9;font-size:.78rem;">
                        💰 <b style="color:#f7931a;">${_cp:,}</b>
                      </div>
                      <div style="font-size:.72rem;">
                        Dominant: <b style="color:{_dom_color};">{_dom}</b>
                      </div>
                      <div style="font-size:.72rem;">
                        Bias: <b style="color:{_bias_color};">{_bias}</b>
                      </div>
                      <div style="font-size:.72rem;">
                        Conf: <b style="color:{_conf_color};">{_conf}</b>
                      </div>
                      <div style="font-size:.72rem;color:#8b949e;">
                        🔴 {_n_long} Long · 🟢 {_n_short} Short clusters
                      </div>
                      <div style="font-size:.68rem;color:#6e7681;">
                        src: {_src}
                      </div>
                    </div>""")

                    # Nút restore
                    _col_r, _col_d = st.columns([3, 1])
                    with _col_r:
                        if _strat and _ana:
                            if st.button(
                                f"♻️ Khôi phục phân tích này",
                                key=f"hist_restore_{_idx}",
                                help=f"Load lại phân tích từ {_saved_label}",
                            ):
                                st.session_state._liq_ai_analysis   = _ana
                                st.session_state._liq_ai_strategy   = _strat
                                st.session_state._liq_strat_saved_at = _saved_raw
                                st.session_state._liq_ai_analysis   = _ana
                                # Cũng set cho inline analyzer
                                st.session_state._inline_liq_analysis  = _ana
                                st.session_state._inline_liq_strategy  = _strat
                                st.session_state._inline_strat_saved_at = _saved_raw
                                st.success(f"✅ Đã khôi phục phân tích từ {_saved_label}")
                                st.rerun()
                    with _col_d:
                        # Xem chi tiết
                        with st.expander("🔍 Chi tiết", expanded=False):
                            st.json(_entry)

                except Exception as _he:
                    st.warning(f"Không đọc được entry {_idx}: {_he}")

            # Nút xóa toàn bộ lịch sử
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🗑 Xóa toàn bộ lịch sử AI (14 ngày)", key="hist_clear_all",
                         help="Không thể hoàn tác"):
                _clear_strategy_from_disk()
                st.success("✅ Đã xóa toàn bộ lịch sử AI.")
                st.rerun()

# ── END LIQ IMAGE AI MODULE ───────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Vietnam timezone  UTC+7
# ---------------------------------------------------------------------------
VN_TZ = timezone(timedelta(hours=7))


def _now_vn() -> datetime:
    """Thời gian hiện tại theo giờ Việt Nam (UTC+7), trả về naive datetime."""
    return datetime.now(VN_TZ).replace(tzinfo=None)


def get_weekly_anchor_vn():
    """
    Trả về điểm neo cố định = thứ 6 gần nhất lúc 15:05 giờ Việt Nam (UTC+7).

    Quy tắc:
      • Nếu hôm nay là thứ 6 VÀ giờ VN >= 15:05  → anchor = hôm nay 15:05 VN
      • Các trường hợp còn lại                     → anchor = thứ 6 vừa qua 15:05 VN
      • Sau thứ 6 tiếp theo 15:05, anchor tự động
        nhảy sang thứ 6 mới (không cần can thiệp).

    Returns:
        anchor_utc  : naive datetime ở UTC  (dùng so sánh với df["open_time"])
        anchor_vn   : naive datetime ở VN   (hiển thị)
        next_fri_vn : naive datetime ở VN   (thứ 6 kế tiếp 15:05)
        W           : float = 7.0  (full-week window cố định)
    """
    now_vn = _now_vn()
    weekday = now_vn.weekday()  # 0=Mon … 4=Fri … 6=Sun

    anchor_hour, anchor_min = 15, 5

    # Số ngày lùi về thứ 6 gần nhất
    days_back = (weekday - 4) % 7  # 0 nếu hôm nay là thứ 6
    candidate_vn = now_vn.replace(hour=anchor_hour, minute=anchor_min,
                                  second=0, microsecond=0) - timedelta(days=days_back)

    # Nếu là thứ 6 nhưng chưa đến 15:05 → lùi thêm 7 ngày
    if days_back == 0 and now_vn < candidate_vn:
        candidate_vn -= timedelta(days=7)

    anchor_vn  = candidate_vn
    anchor_utc = anchor_vn - timedelta(hours=7)   # VN → UTC naive
    next_fri_vn = anchor_vn + timedelta(days=7)

    return anchor_utc, anchor_vn, next_fri_vn, 7.0


def _thread_running(name: str) -> bool:
    """Kiểm tra thread tên 'name' đã đang chạy chưa."""
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


def get_liq_df():
    """Stub — liquidation stream đã tắt."""
    return pd.DataFrame()



# ---------------------------------------------------------------------------
# Multi-Exchange Liquidation — DISABLED (streams removed)
# ---------------------------------------------------------------------------

_EXCHANGE_LOCK   = threading.Lock()
_EXCHANGE_BUFFER: deque = deque(maxlen=100)
_exchange_colors = {"Binance":"#f3ba2f","Bybit":"#ff6b35","OKX":"#00d4aa","Hyperliquid":"#9b59b6","Coinbase":"#0052ff"}

def ensure_multi_exchange_streams():
    pass  # disabled

def get_multi_liq_df() -> pd.DataFrame:
    return pd.DataFrame()

def persist_liq_to_session(df: pd.DataFrame):
    pass  # disabled

def get_persisted_liq() -> pd.DataFrame:
    return st.session_state.get("liq_history", pd.DataFrame())

def calc_exchange_stats(df: pd.DataFrame) -> dict:
    return {}


import asyncio
import threading
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
import requests
import pandas as pd
import numpy as np
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# ─── Config tường ────────────────────────────────────────────────────────────
_WALL_EXPIRE_UTC_HOUR = 15      # 15:00 UTC → tường vô hiệu
_FREE_RUN_DAY         = 21      # ngày không có tường (MM free run)
_WALL_REFRESH_SEC     = 60      # refresh mỗi 60s (REST, không WS)
_DERIBIT_REST         = "https://www.deribit.com/api/v2/public"

# ─── Shared state cho options wall ───────────────────────────────────────────
_wall_lock   = threading.Lock()
_wall_state: Dict = {
    # key: date_str "YYYY-MM-DD" → {"call_wall": float, "put_wall": float,
    #                                "call_oi": dict, "put_oi": dict,
    #                                "expiry_label": str}
}
_wall_thread_started = False


# ─── Utility ─────────────────────────────────────────────────────────────────
def _expiry_label_for_date(d: datetime) -> str:
    """
    Trả về label expiry Deribit cho ngày d.
    VD: datetime(2025,5,17) → '17MAY25'
    Linux: %-d, Windows: %#d — ta dùng lstrip('0') để cross-platform
    """
    day = str(d.day)            # không có leading zero
    mon = d.strftime("%b").upper()
    yr  = d.strftime("%y")
    return f"{day}{mon}{yr}"


def _week_dates_utc() -> list:
    """
    Trả về list datetime cho các ngày trong tuần hiện tại đến thứ 6 expire.
    Bắt đầu từ hôm nay đến thứ 6 tiếp theo (inclusive).
    """
    now = datetime.now(timezone.utc)
    days_to_fri = (4 - now.weekday()) % 7
    if days_to_fri == 0:
        days_to_fri = 7
    dates = []
    for i in range(0, days_to_fri + 1):
        dates.append(now + timedelta(days=i))
    return dates


def _is_wall_active_for_day(d: datetime) -> bool:
    """
    Tường call/put hoạt động trong ngày d nếu:
    - Không phải ngày _FREE_RUN_DAY
    - Chưa đến 15:00 UTC của ngày đó (nếu là hôm nay)
    """
    now = datetime.now(timezone.utc)
    if d.day == _FREE_RUN_DAY:
        return False
    if d.date() == now.date():
        # Hôm nay: chỉ active nếu chưa 15:00 UTC
        return now.hour < _WALL_EXPIRE_UTC_HOUR
    if d.date() < now.date():
        # Ngày đã qua → không active
        return False
    return True  # Ngày tương lai → active


# ─── Fetch options data từ Deribit REST (nhẹ hơn WS, phù hợp Streamlit) ─────
def _fetch_wall_for_expiry(expiry_label: str) -> Tuple[Optional[float], Optional[float], dict, dict]:
    """
    Lấy toàn bộ OI cho một ngày expiry cụ thể.
    Trả về (call_wall, put_wall, call_oi_by_strike, put_oi_by_strike).
    """
    try:
        # Lấy book summary theo currency (đã có trong fetch_deribit_options gốc)
        # Nhưng ta cần filter theo expiry_label cụ thể
        r = requests.get(
            f"{_DERIBIT_REST}/get_book_summary_by_currency",
            params={"currency": "BTC", "kind": "option"},
            timeout=12,
        )
        r.raise_for_status()
        items = r.json().get("result", [])
    except Exception as e:
        logger.warning(f"Deribit REST wall fetch error: {e}")
        return None, None, {}, {}

    call_oi: Dict[float, float] = {}
    put_oi:  Dict[float, float] = {}

    for item in items:
        name = item.get("instrument_name", "")
        # VD: BTC-17MAY25-80000-C
        parts = name.split("-")
        if len(parts) != 4:
            continue
        if parts[1] != expiry_label:
            continue
        try:
            strike = float(parts[2])
            otype  = parts[3]        # "C" hoặc "P"
            oi     = float(item.get("open_interest", 0) or 0)
        except (ValueError, TypeError):
            continue

        if otype == "C":
            call_oi[strike] = call_oi.get(strike, 0) + oi
        else:
            put_oi[strike] = put_oi.get(strike, 0) + oi

    if not call_oi and not put_oi:
        return None, None, {}, {}

    call_wall = float(max(call_oi, key=lambda k: call_oi[k])) if call_oi else None
    put_wall  = float(max(put_oi,  key=lambda k: put_oi[k]))  if put_oi  else None
    return call_wall, put_wall, call_oi, put_oi


def _refresh_all_walls():
    """
    Refresh tường cho tất cả ngày trong tuần đến thứ 6.
    Chạy trong background thread.
    """
    week = _week_dates_utc()
    new_state = {}
    for d in week:
        label = _expiry_label_for_date(d)
        date_key = d.strftime("%Y-%m-%d")
        cw, pw, c_oi, p_oi = _fetch_wall_for_expiry(label)
        new_state[date_key] = {
            "call_wall":    cw,
            "put_wall":     pw,
            "call_oi":      c_oi,
            "put_oi":       p_oi,
            "expiry_label": label,
            "updated_at":   datetime.now(timezone.utc),
        }
        logger.info(f"Wall [{label}] C={cw}  P={pw}")
        time.sleep(0.5)   # tránh rate limit

    with _wall_lock:
        _wall_state.clear()
        _wall_state.update(new_state)


def _run_wall_refresh_loop():
    """Background thread: refresh walls mỗi _WALL_REFRESH_SEC giây."""
    while True:
        try:
            _refresh_all_walls()
        except Exception as e:
            logger.error(f"Wall refresh loop error: {e}")
        time.sleep(_WALL_REFRESH_SEC)


def ensure_options_wall_stream():
    """Khởi động background thread refresh options wall — guard bằng thread name."""
    if not _thread_running("OptionsWallRefresh"):
        t = threading.Thread(
            target=_run_wall_refresh_loop,
            daemon=True,
            name="OptionsWallRefresh",
        )
        t.start()
        logger.info("Options wall background thread started.")


def get_wall_snapshot() -> Dict:
    """Thread-safe snapshot của _wall_state."""
    with _wall_lock:
        return dict(_wall_state)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION B — Hàm tính daily_walls từ opts_df (dùng data đã có sẵn)
# Gọi trong main() sau fetch_deribit_options()
# ═════════════════════════════════════════════════════════════════════════════

def get_daily_walls(opts_df: pd.DataFrame) -> Dict:
    """
    Từ opts_df (đã fetch_deribit_options), tính Call Wall / Put Wall
    cho từng ngày trong tuần (đến thứ 6 expire).

    Trả về dict:
      {
        "2025-05-17": {
          "call_wall": 85000.0,
          "put_wall":  77500.0,
          "call_oi":   {80000: 120.5, 85000: 210.3, ...},
          "put_oi":    {77500: 195.2, 75000: 88.1,  ...},
          "expiry_label": "17MAY25",
          "wall_active": True,
        },
        ...
      }
    """
    result: Dict = {}
    if opts_df is None or opts_df.empty or "expiry" not in opts_df.columns:
        return result

    week = _week_dates_utc()

    for d in week:
        date_key = d.strftime("%Y-%m-%d")
        label    = _expiry_label_for_date(d)
        active   = _is_wall_active_for_day(d)

        # Filter opts_df theo ngày expiry này
        day_df = opts_df[opts_df["expiry"].apply(
            lambda x: (
                x.date() == d.date()
                if (x is not None and not (isinstance(x, float) and np.isnan(x)))
                else False
            )
        )]

        if day_df.empty:
            result[date_key] = {
                "call_wall": None, "put_wall": None,
                "daily_max_pain": None,
                "call_oi": {}, "put_oi": {},
                "expiry_label": label, "wall_active": active,
            }
            continue

        calls = day_df[day_df["type"] == "C"].groupby("strike")["oi"].sum()
        puts  = day_df[day_df["type"] == "P"].groupby("strike")["oi"].sum()

        call_oi_dict = calls.to_dict()
        put_oi_dict  = puts.to_dict()

        call_wall = float(calls.idxmax()) if not calls.empty else None
        put_wall  = float(puts.idxmax())  if not puts.empty  else None

        # ── Daily Max Pain cho ngày này ──────────────────────────────────────
        daily_mp = None
        if not calls.empty or not puts.empty:
            all_strikes = sorted(set(list(calls.index) + list(puts.index)))
            if all_strikes:
                best_pain = float("inf")
                for settle in all_strikes:
                    c_pain = sum((settle - k) * v for k, v in calls.items() if k < settle)
                    p_pain = sum((k - settle) * v for k, v in puts.items()  if k > settle)
                    total  = c_pain + p_pain
                    if total < best_pain:
                        best_pain = total
                        daily_mp  = settle

        result[date_key] = {
            "call_wall":    call_wall,
            "put_wall":     put_wall,
            "daily_max_pain": daily_mp,
            "call_oi":      call_oi_dict,
            "put_oi":       put_oi_dict,
            "expiry_label": label,
            "wall_active":  active,
        }

    # Merge với real-time data từ background thread (nếu có)
    rt = get_wall_snapshot()
    for date_key, rt_data in rt.items():
        if date_key in result:
            # Real-time data override nếu có call/put wall
            if rt_data.get("call_wall") and rt_data.get("put_wall"):
                result[date_key]["call_wall"] = rt_data["call_wall"]
                result[date_key]["put_wall"]  = rt_data["put_wall"]
                if rt_data.get("call_oi"):
                    result[date_key]["call_oi"] = rt_data["call_oi"]
                if rt_data.get("put_oi"):
                    result[date_key]["put_oi"] = rt_data["put_oi"]

    return result


# ═════════════════════════════════════════════════════════════════════════════
# SECTION C — Patch cho make_price_chart()
# Thay thế toàn bộ make_price_chart bằng make_price_chart_patched
# Hoặc chỉ copy phần thêm shapes/annotations vào trong make_price_chart gốc
# ═════════════════════════════════════════════════════════════════════════════

def _build_wall_shapes_annotations(
    daily_walls: Dict,
    last_t,       # pd.Timestamp: candle cuối cùng
    timeframe: str = "15m",
) -> Tuple[list, list]:
    """
    Tạo shapes + annotations cho Call Wall / Put Wall của từng ngày.
    Chỉ hoạt động trong timeframe == '15m'.

    Logic:
    - Mỗi ngày: vẽ 2 đường ngang (call=xanh lá, put=đỏ)
    - Đường chỉ kéo dài trong khoảng thời gian của ngày đó (00:00–15:00 UTC)
    - Sau 15:00 UTC hoặc ngày 21 → tường biến mất
    - Ngày 21 → vẽ banner "Free Run"
    """
    shapes:      list = []
    annotations: list = []

    if not daily_walls:
        return shapes, annotations

    now_utc = datetime.now(timezone.utc)

    for date_key, wall in daily_walls.items():
        try:
            d = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        cw     = wall.get("call_wall")
        pw     = wall.get("put_wall")
        active = wall.get("wall_active", False)
        label  = wall.get("expiry_label", date_key)
        is_free_run = (d.day == _FREE_RUN_DAY)

        # Chỉ vẽ thứ 2–thứ 6 (Mon=0 … Fri=4), bỏ Sat/Sun
        if d.weekday() > 4:
            continue

        # Khoảng thời gian của ngày này trên trục x
        x0_day = d.replace(hour=0,  minute=0,  second=0)   # 00:00 UTC ngày đó
        x1_day = d.replace(hour=15, minute=0,  second=0)   # 15:00 UTC ngày đó (tường hết hạn)

        # Nếu ngày trong quá khứ so với candle cuối → bỏ qua
        if x1_day < pd.Timestamp(last_t).tz_localize("UTC") if hasattr(last_t, "tzinfo") and last_t.tzinfo is None else pd.Timestamp(x1_day):
            pass   # vẫn vẽ để show lịch sử

        # ── Free Run Day ──────────────────────────────────────────────────────
        if is_free_run:
            annotations.append(dict(
                x=x0_day + timedelta(hours=7.5),
                xref="x", yref="paper",
                y=0.98,
                text=f"⚡ {label} FREE RUN DAY",
                showarrow=False,
                font=dict(color="#ffd700", size=9, family="monospace"),
                bgcolor="rgba(20,18,0,0.85)",
                bordercolor="#ffd700",
                borderwidth=1,
                borderpad=3,
                xanchor="center", yanchor="top",
            ))
            continue   # không vẽ tường cho ngày này

        # ── Tường đã vô hiệu sau 15:00 UTC ──────────────────────────────────
        if not active:
            if d.date() == now_utc.date():
                # Hôm nay đã qua 15:00 → thêm annotation "đã expire"
                annotations.append(dict(
                    x=x1_day + timedelta(minutes=15),
                    xref="x", yref="paper",
                    y=0.96,
                    text=f"🔕 {label} wall expired 15:00",
                    showarrow=False,
                    font=dict(color="#555", size=8, family="monospace"),
                    xanchor="left", yanchor="top",
                ))
            continue   # không vẽ tường

        if cw is None or pw is None:
            continue

        # ── Call Wall (xanh lá) ───────────────────────────────────────────────
        shapes.append(dict(
            type="line",
            xref="x",
            x0=x0_day, x1=x1_day,
            yref="y",
            y0=cw, y1=cw,
            line=dict(color="#00e676", width=2, dash="dot"),
            opacity=0.90,
        ))
        annotations.append(dict(
            x=x1_day,
            xref="x",
            y=cw,
            yref="y",
            text=f"C ${cw:,.0f}",
            showarrow=False,
            font=dict(color="#00e676", size=10, family="monospace"),
            bgcolor="rgba(0,20,10,0.85)",
            bordercolor="#00e676",
            borderwidth=1,
            borderpad=2,
            xanchor="right", yanchor="bottom",
        ))

        # ── Put Wall (đỏ) ─────────────────────────────────────────────────────
        shapes.append(dict(
            type="line",
            xref="x",
            x0=x0_day, x1=x1_day,
            yref="y",
            y0=pw, y1=pw,
            line=dict(color="#ff1744", width=2, dash="dot"),
            opacity=0.90,
        ))
        annotations.append(dict(
            x=x1_day,
            xref="x",
            y=pw,
            yref="y",
            text=f"P ${pw:,.0f}",
            showarrow=False,
            font=dict(color="#ff1744", size=10, family="monospace"),
            bgcolor="rgba(20,0,5,0.85)",
            bordercolor="#ff1744",
            borderwidth=1,
            borderpad=2,
            xanchor="right", yanchor="top",
        ))

        # ── Safe zone: shaded region giữa Put Wall và Call Wall ──────────────
        if cw > pw:
            shapes.append(dict(
                type="rect",
                xref="x", x0=x0_day, x1=x1_day,
                yref="y", y0=pw, y1=cw,
                fillcolor="rgba(255,255,255,0.025)",
                line=dict(width=0),
                layer="below",
            ))

        # ── Daily Max Pain (trắng nhạt, nét đứt mảnh) ────────────────────────
        dmp = wall.get("daily_max_pain")
        if dmp:
            shapes.append(dict(
                type="line",
                xref="x", x0=x0_day, x1=x1_day,
                yref="y", y0=dmp, y1=dmp,
                line=dict(color="rgba(200,180,255,0.60)", width=1, dash="dashdot"),
                opacity=0.80,
            ))
            # Label MP ở giữa ngày, dùng yanchor để tránh đè Call/Put
            _is_above_mid = (dmp > (cw + pw) / 2) if (cw and pw) else False
            annotations.append(dict(
                x=x0_day + (x1_day - x0_day) / 2,
                xref="x", y=dmp, yref="y",
                text=f"MP ${dmp:,.0f}",
                showarrow=False,
                font=dict(color="rgba(200,180,255,0.85)", size=7, family="monospace"),
                bgcolor="rgba(10,8,20,0.75)",
                borderpad=1,
                xanchor="center",
                yanchor="top" if _is_above_mid else "bottom",
            ))

    return shapes, annotations


def make_price_chart_patched(
    df, tf_label="", current_price=None, max_pain=None,
    weekly_max_pain=None, scenarios=None,
    ls_ratio_val=0.5, net_long_pct=50.0, current_fr=0.0,
    long_cluster=None, short_cluster=None,
    long_cluster_liq_usd=0.0, short_cluster_liq_usd=0.0,
    timeframe="1H",
    daily_walls: Optional[Dict] = None,
    monthly_max_pain=None,
    ai_liq_clusters=None,
):
    """
    Wrapper của make_price_chart() gốc — thêm options wall vào shapes/annotations.
    Hoạt động cho cả 15m và 1H.
    """
    # Gọi hàm gốc để lấy fig
    fig = make_price_chart(
        df=df, tf_label=tf_label,
        current_price=current_price, max_pain=max_pain,
        weekly_max_pain=weekly_max_pain, scenarios=scenarios,
        ls_ratio_val=ls_ratio_val, net_long_pct=net_long_pct,
        current_fr=current_fr,
        long_cluster=long_cluster, short_cluster=short_cluster,
        long_cluster_liq_usd=long_cluster_liq_usd,
        short_cluster_liq_usd=short_cluster_liq_usd,
        timeframe=timeframe,
        daily_walls=daily_walls,
        monthly_max_pain=monthly_max_pain,
        ai_liq_clusters=ai_liq_clusters,
    )

    # Thêm wall cho cả 15m và 1H
    if (timeframe not in ("15m", "1H")) or not daily_walls or df.empty:
        return fig

    last_t = df["open_time"].iloc[-1]

    wall_shapes, wall_annots = _build_wall_shapes_annotations(
        daily_walls, last_t, timeframe
    )

    if not wall_shapes and not wall_annots:
        return fig

    # Merge vào layout hiện có
    existing_shapes = list(fig.layout.shapes or [])
    existing_annots = list(fig.layout.annotations or [])

    fig.update_layout(
        shapes      = existing_shapes + wall_shapes,
        annotations = existing_annots + wall_annots,
    )

    return fig


# ═════════════════════════════════════════════════════════════════════════════
# SECTION D — Mini chart: Volume by Strike Price (theo từng ngày)
# Hiển thị ngay bên dưới price chart khi timeframe == "15m"
# ═════════════════════════════════════════════════════════════════════════════

def make_daily_strike_chart(daily_walls: Dict, current_price: float) -> Optional[go.Figure]:
    """
    Tạo stacked bar chart: Call OI / Put OI theo strike, cho ngày hôm nay
    (hoặc ngày tiếp theo nếu tường hôm nay đã expire).
    Tương tự hình Coinglass bạn cung cấp — nén lại từng ngày.

    Trả về None nếu không có dữ liệu.
    """
    if not daily_walls:
        return None

    now_utc  = datetime.now(timezone.utc)
    today_key = now_utc.strftime("%Y-%m-%d")

    # Chọn ngày ưu tiên: hôm nay nếu còn wall, nếu không lấy ngày tiếp theo có wall
    target_wall = None
    target_label = ""
    for date_key in sorted(daily_walls.keys()):
        w = daily_walls[date_key]
        if w.get("wall_active") and w.get("call_oi") and w.get("put_oi"):
            target_wall  = w
            target_label = w.get("expiry_label", date_key)
            break

    if target_wall is None:
        return None

    call_oi: Dict = target_wall.get("call_oi", {})
    put_oi:  Dict = target_wall.get("put_oi",  {})
    cw  = target_wall.get("call_wall")
    pw  = target_wall.get("put_wall")

    if not call_oi and not put_oi:
        return None

    all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
    if not all_strikes:
        return None

    # Lọc ±20% quanh current_price
    lo = current_price * 0.80
    hi = current_price * 1.20
    all_strikes = [s for s in all_strikes if lo <= s <= hi]
    if not all_strikes:
        return None

    call_vals = [call_oi.get(s, 0) for s in all_strikes]
    put_vals  = [put_oi.get(s, 0)  for s in all_strikes]

    # Max pain calculation
    max_pain_strike = _calc_max_pain_from_oi(call_oi, put_oi, all_strikes)

    # Put/Call ratio
    total_call = sum(call_vals)
    total_put  = sum(put_vals)
    pc_ratio   = total_put / total_call if total_call > 0 else 0

    fig = go.Figure()

    # Cột Puts (đỏ)
    fig.add_trace(go.Bar(
        x=all_strikes,
        y=put_vals,
        name="Puts OI",
        marker_color="rgba(248,81,73,0.80)",
        hovertemplate="Strike $%{x:,.0f}<br>Put OI: %{y:,.2f} BTC<extra></extra>",
    ))

    # Cột Calls (xanh lá)
    fig.add_trace(go.Bar(
        x=all_strikes,
        y=call_vals,
        name="Calls OI",
        marker_color="rgba(0,230,118,0.80)",
        hovertemplate="Strike $%{x:,.0f}<br>Call OI: %{y:,.2f} BTC<extra></extra>",
    ))

    # Max Pain vertical line
    if max_pain_strike:
        fig.add_vline(
            x=max_pain_strike,
            line_color="#ffffff", line_dash="dot", line_width=1.5,
            annotation_text=f"Max Pain: ${max_pain_strike:,.0f}",
            annotation_position="top",
            annotation_font=dict(color="#ffffff", size=9),
        )

    # Call Wall line
    if cw:
        fig.add_vline(
            x=cw,
            line_color="#00e676", line_dash="dash", line_width=2,
            annotation_text=f"📞 Call Wall: ${cw:,.0f}",
            annotation_position="top right",
            annotation_font=dict(color="#00e676", size=9),
        )

    # Put Wall line
    if pw:
        fig.add_vline(
            x=pw,
            line_color="#ff1744", line_dash="dash", line_width=2,
            annotation_text=f"🛡 Put Wall: ${pw:,.0f}",
            annotation_position="top left",
            annotation_font=dict(color="#ff1744", size=9),
        )

    # Current price line
    if current_price:
        fig.add_vline(
            x=current_price,
            line_color="#f7931a", line_dash="dot", line_width=1.5,
            annotation_text=f"${current_price:,.0f}",
            annotation_position="top",
            annotation_font=dict(color="#f7931a", size=10),
        )

    fig.update_layout(
        title=dict(
            text=(
                f"Volume by Strike Price  ·  Expiry {target_label}  ·  "
                f"Calls: {total_call:,.2f}  Puts: {total_put:,.2f}  "
                f"Put/Call: {pc_ratio:.2f}"
            ),
            font=dict(color="#e6edf3", size=12),
        ),
        paper_bgcolor="#0e1117",
        plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        barmode="group",
        bargap=0.15,
        xaxis=dict(
            gridcolor="#21262d",
            color="#8b949e",
            tickprefix="$",
            title="Strike Price",
            tickformat=",.0f",
        ),
        yaxis=dict(
            gridcolor="#21262d",
            color="#8b949e",
            title="Open Interest (BTC)",
        ),
        legend=dict(
            bgcolor="rgba(22,27,34,0.90)",
            bordercolor="#30363d",
            borderwidth=1,
            font=dict(color="#e6edf3", size=10),
            orientation="h",
            yanchor="top", y=-0.12,
        ),
        margin=dict(l=10, r=10, t=60, b=10),
        height=300,
    )

    return fig


def _calc_max_pain_from_oi(call_oi: dict, put_oi: dict, strikes: list) -> Optional[float]:
    """
    Tính Max Pain strike = nơi tổng intrinsic value options nhỏ nhất.
    """
    if not strikes:
        return None
    min_pain = float("inf")
    mp_strike = None
    for settle in strikes:
        call_pain = sum((settle - k) * v for k, v in call_oi.items() if k < settle)
        put_pain  = sum((k - settle) * v for k, v in put_oi.items()  if k > settle)
        total = call_pain + put_pain
        if total < min_pain:
            min_pain  = total
            mp_strike = settle
    return mp_strike


# ═════════════════════════════════════════════════════════════════════════════
# SECTION E — Streamlit UI card: hiển thị trạng thái tường
# Đặt ngay dưới price chart trong main()
# ═════════════════════════════════════════════════════════════════════════════

def render_options_wall_card(daily_walls: Dict, current_price: float):
    """
    Hiển thị card tóm tắt trạng thái Call Wall / Put Wall từng ngày trong tuần.
    Chỉ render khi timeframe == '15m'.
    """
    if not daily_walls:
        return

    import streamlit as st

    now_utc = datetime.now(timezone.utc)

    st.markdown(
        '<div class="section-title">🧱 Options Wall (Call/Put)  ·  Deribit Real-Time</div>',
        unsafe_allow_html=True,
    )

    cols_data = []
    for date_key in sorted(daily_walls.keys()):
        w      = daily_walls[date_key]
        cw     = w.get("call_wall")
        pw     = w.get("put_wall")
        active = w.get("wall_active", False)
        label  = w.get("expiry_label", date_key)
        d      = datetime.strptime(date_key, "%Y-%m-%d")

        is_today    = (d.date() == now_utc.date())
        is_free_run = (d.day == _FREE_RUN_DAY)
        day_name    = d.strftime("%a %d/%m")

        if is_free_run:
            status_html = '<span style="color:#ffd700">⚡ FREE RUN</span>'
            cw_html = pw_html = "—"
        elif not active:
            status_html = '<span style="color:#555">🔕 Expired</span>'
            cw_html = pw_html = "—"
        else:
            status_html = '<span style="color:#3fb950">🟢 Active</span>'
            cw_html = f'<span style="color:#00e676">${cw:,.0f}</span>' if cw else "—"
            pw_html = f'<span style="color:#ff1744">${pw:,.0f}</span>'  if pw else "—"

            # Kiểm tra giá hiện tại có vượt tường không
            if current_price and cw and current_price > cw:
                cw_html += ' <span style="color:#f85149;font-size:0.7rem">⚠️ BREACH</span>'
            if current_price and pw and current_price < pw:
                pw_html += ' <span style="color:#f85149;font-size:0.7rem">⚠️ BREACH</span>'

        today_border = "border:1.5px solid #ffd700;" if is_today else "border:1px solid #30363d;"
        cols_data.append({
            "day_name": day_name,
            "is_today": is_today,
            "status_html": status_html,
            "cw_html": cw_html,
            "pw_html": pw_html,
            "today_border": today_border,
            "label": label,
        })

    # Render theo cột (tối đa 7 cột = 7 ngày trong tuần)
    if cols_data:
        cols = st.columns(len(cols_data))
        for i, col in enumerate(cols):
            cd = cols_data[i]
            with col:
                st.markdown(
                    f'<div style="background:#161b22;{cd["today_border"]}'
                    f'border-radius:8px;padding:.6rem .8rem;text-align:center;">'
                    f'<div style="font-size:0.7rem;color:{"#ffd700" if cd["is_today"] else "#8b949e"};'
                    f'font-weight:{"700" if cd["is_today"] else "400"};margin-bottom:.3rem;">'
                    f'{"📅 " if cd["is_today"] else ""}{cd["day_name"]}</div>'
                    f'<div style="font-size:0.72rem;margin-bottom:.4rem;">{cd["status_html"]}</div>'
                    f'<div style="font-size:0.75rem;line-height:1.6;">'
                    f'<b style="color:#8b949e">C:</b> {cd["cw_html"]}<br>'
                    f'<b style="color:#8b949e">P:</b> {cd["pw_html"]}'
                    f'</div></div>',
                    unsafe_allow_html=True,
                )

    # Disclaimer nhỏ
    st.caption(
        "📌 Call Wall = strike OI Call cao nhất  ·  Put Wall = strike OI Put cao nhất  ·  "
        "Tường vô hiệu 15:00 UTC  ·  Ngày 21 = Free Run (không có tường)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# SECTION F — Hướng dẫn chi tiết tích hợp vào app_v4.py


# ---------------------------------------------------------------------------
# Streamlit page config (must be first st call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="BTC Dashboard",
    page_icon="₿",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Start background threads once at module load
ensure_options_wall_stream()
ensure_cf_poll_thread()   # CounterFlow REST poll — bền vững qua restart

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .metric-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 1rem 1.5rem;
        text-align: center;
    }
    .metric-label {
        font-size: 0.75rem;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.25rem;
    }
    .metric-value {
        font-size: 1.75rem;
        font-weight: 700;
        color: #e6edf3;
    }
    .metric-value.green { color: #3fb950; }
    .metric-value.red { color: #f85149; }
    .metric-value.orange { color: #f7931a; }
    .metric-sub { font-size: 0.8rem; margin-top: 0.15rem; }
    .green { color: #3fb950; }
    .red { color: #f85149; }
    .last-updated {
        font-size: 0.75rem;
        color: #8b949e;
        text-align: right;
        margin-bottom: 0.5rem;
    }
    .section-title {
        font-size: 1rem;
        font-weight: 600;
        color: #e6edf3;
        margin: 1.2rem 0 0.5rem 0;
        padding-bottom: 0.3rem;
        border-bottom: 1px solid #21262d;
    }
    div[data-testid="stHorizontalBlock"] > div { padding: 0 0.3rem; }
    h1, h2, h3 { color: #e6edf3 !important; }

    /* Ẩn Streamlit running spinner overlay — không flash tối khi rerun */
    div[data-testid="stStatusWidget"] { display: none !important; }
    div[data-testid="stDecoration"]   { display: none !important; }
    /* Ẩn iframe border của JS ticker component */
    iframe[title="streamlit_components.v1.html"] {
        display: none !important;
        height: 0 !important;
    }
    /* Tắt transition Streamlit dùng khi re-render (gây flash) */
    .main .block-container { transition: none !important; }
    div[data-testid="stAppViewContainer"] { transition: none !important; }
</style>
""", unsafe_allow_html=True)

BINANCE_BASE = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"

# ---------------------------------------------------------------------------
# REST data fetchers
# ---------------------------------------------------------------------------

def fetch_live_price(symbol="BTCUSDT") -> float:
    """Lấy giá BTC live tức thì — KHÔNG cache, gọi trực tiếp Binance REST."""
    try:
        r = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": symbol}, timeout=5,
        )
        r.raise_for_status()
        return float(r.json().get("price", 0))
    except Exception:
        return 0.0


@st.cache_data(ttl=30, show_spinner=False)   # price: refresh every 30s
def fetch_price_klines(symbol="BTCUSDT", interval="1m", limit=120):
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         timeout=10)
        r.raise_for_status()
        df = pd.DataFrame(r.json(), columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        st.error(f"Price fetch error: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=30, show_spinner=False)   # ticker: refresh every 30s
def fetch_ticker_24h(symbol="BTCUSDT"):
    try:
        r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr",
                         params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Ticker fetch error: {e}")
        return {}


@st.cache_data(ttl=60, show_spinner=False)   # OI hist: refresh every 60s
def fetch_open_interest_hist(symbol="BTCUSDT", period="5m", limit=60):
    try:
        r = requests.get(f"{BINANCE_FUTURES}/futures/data/openInterestHist",
                         params={"symbol": symbol, "period": period, "limit": limit},
                         timeout=10)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df["sumOpenInterest"] = df["sumOpenInterest"].astype(float)
        df["sumOpenInterestValue"] = df["sumOpenInterestValue"].astype(float)
        return df
    except Exception as e:
        st.error(f"Open Interest fetch error: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)  # funding history: refresh every 5 min
def fetch_funding_rate(symbol="BTCUSDT", limit=30):
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/fundingRate",
                         params={"symbol": symbol, "limit": limit}, timeout=10)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms")
        df["fundingRate"] = df["fundingRate"].astype(float) * 100
        return df
    except Exception as e:
        st.error(f"Funding Rate fetch error: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)   # funding current: refresh every 60s
def fetch_current_funding(symbol="BTCUSDT"):
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex",
                         params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


@st.cache_data(ttl=900, show_spinner=False)  # options: refresh every 15 min
def fetch_deribit_options(currency="BTC"):
    """Fetch all live BTC options from Deribit public API (includes expiry date)."""
    try:
        r = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            params={"currency": currency, "kind": "option"},
            timeout=15,
        )
        r.raise_for_status()
        result = r.json().get("result", [])
        rows = []
        for item in result:
            name = item.get("instrument_name", "")
            parts = name.split("-")
            if len(parts) != 4:
                continue
            try:
                strike   = float(parts[2])
                opt_type = parts[3]           # "C" or "P"
                oi       = float(item.get("open_interest", 0) or 0)
                try:
                    expiry = datetime.strptime(parts[1], "%d%b%y")
                except ValueError:
                    expiry = None
                rows.append({"strike": strike, "type": opt_type, "oi": oi,
                             "expiry": expiry})
            except (ValueError, TypeError):
                continue
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def calculate_max_pain(opts_df):
    """
    Return (max_pain_price, strikes_list, pain_values_list).
    Max pain = strike where total intrinsic value of all options is minimised.
    """
    if opts_df.empty:
        return None, [], []
    calls = opts_df[opts_df["type"] == "C"].groupby("strike")["oi"].sum()
    puts  = opts_df[opts_df["type"] == "P"].groupby("strike")["oi"].sum()
    strikes = sorted(opts_df["strike"].unique())
    if not strikes:
        return None, [], []

    pain_values = []
    for p in strikes:
        call_pain = sum((p - k) * v for k, v in calls.items() if k < p)
        put_pain  = sum((k - p) * v for k, v in puts.items()  if k > p)
        pain_values.append(call_pain + put_pain)

    min_idx = pain_values.index(min(pain_values))
    return strikes[min_idx], strikes, pain_values


def fetch_weekly_max_pain(opts_df):
    """
    Filter opts_df to next Friday's expiry and return max pain price.
    Falls back to the nearest available Friday within 14 days.
    Cached via opts_df which is itself cached for 15 min.
    """
    if opts_df.empty or "expiry" not in opts_df.columns:
        return None
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    # Find next Friday (weekday 4)
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7   # already Friday → go to next one
    for offset in range(days_ahead, days_ahead + 14, 7):
        target = today + timedelta(days=offset)
        friday_df = opts_df[opts_df["expiry"].apply(
            lambda x: x.date() == target if (x is not None and not (isinstance(x, float) and np.isnan(x))) else False
        )]
        if not friday_df.empty:
            pain, _, _ = calculate_max_pain(friday_df)
            return pain
    return None

## ════════════════════════════════════════════════════════════════

def fetch_monthly_max_pain(opts_df):
    """
    Tìm Max Pain của kỳ hạn tháng kế tiếp.
    Deribit expire options tháng vào thứ 6 cuối tháng.
    """
    if opts_df is None or opts_df.empty or "expiry" not in opts_df.columns:
        return None, None

    today = datetime.now(timezone.utc).replace(tzinfo=None).date()

    # Tìm thứ 6 cuối cùng của tháng hiện tại và tháng sau
    def last_friday_of_month(year, month):
        last_day = _monthrange(year, month)[1]
        d = datetime(year, month, last_day).date()
        # Lùi về thứ 6 gần nhất
        while d.weekday() != 4:
            d -= timedelta(days=1)
        return d

    # Thử tháng hiện tại trước, nếu đã qua thì lấy tháng sau
    candidates = []
    for month_offset in range(0, 4):
        year  = today.year + (today.month + month_offset - 1) // 12
        month = (today.month + month_offset - 1) % 12 + 1
        lf    = last_friday_of_month(year, month)
        if lf > today:
            candidates.append(lf)
        if len(candidates) >= 2:
            break

    for target_date in candidates:
        monthly_df = opts_df[opts_df["expiry"].apply(
            lambda x: (
                x.date() == target_date
                if (x is not None and not (isinstance(x, float) and np.isnan(x)))
                else False
            )
        )]
        if not monthly_df.empty:
            pain, _, _ = calculate_max_pain(monthly_df)
            if pain:
                return pain, target_date

    return None, None


def fetch_quarterly_max_pain(opts_df):
    """
    Tìm Max Pain kỳ hạn quý — thứ 6 cuối tháng 3, 6, 9, 12.
    """
    if opts_df is None or opts_df.empty or "expiry" not in opts_df.columns:
        return None, None

    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    quarter_months = [3, 6, 9, 12]

    def last_friday_of_month(year, month):
        last_day = _monthrange(year, month)[1]
        d = datetime(year, month, last_day).date()
        while d.weekday() != 4:
            d -= timedelta(days=1)
        return d

    for year_offset in range(0, 2):
        for qm in quarter_months:
            yr = today.year + year_offset
            lf = last_friday_of_month(yr, qm)
            if lf > today + timedelta(days=20):   # ít nhất 20 ngày nữa
                qdf = opts_df[opts_df["expiry"].apply(
                    lambda x: (
                        x.date() == lf
                        if (x is not None and not (isinstance(x, float) and np.isnan(x)))
                        else False
                    )
                )]
                if not qdf.empty:
                    pain, _, _ = calculate_max_pain(qdf)
                    if pain:
                        return pain, lf

    return None, None

## ════════════════════════════════════════════════════════════════

def calculate_scenarios(current_price, weekly_max_pain, long_cluster,
                        short_cluster, pc_ratio, funding_rate_pct, oi_df,
                        monthly_max_pain=None, quarterly_max_pain=None,
                        anchor_price=None):
    """
    4 MM scenario projections — tuần (1h) + tháng (4h).
    Scenario D kết hợp 3 Max Pain levels + 3 liquidity clusters.
    Tất cả scenarios hội tụ đúng ngày thứ 6 expire.

    anchor_price : giá BTC tại 15:05 thứ 6 VN — điểm Y gốc cố định của scenarios.
                   Nếu None → dùng current_price (fallback).
    """
    if not current_price:
        return {}

    # Giá neo cố định tại điểm anchor (15:05 thứ 6 VN)
    origin_price = float(anchor_price) if anchor_price and anchor_price > 0 else float(current_price)

    wmp  = weekly_max_pain    or current_price
    mmp  = monthly_max_pain   or wmp
    qmp  = quarterly_max_pain or mmp

    # ── W = 7 ngày cố định — full week từ 15:05 thứ 6 VN đến 15:05 thứ 6 kế tiếp ──
    # Dùng VN anchor để luôn có cửa sổ tuần cố định, không bị scale/bóp méo
    anchor_utc, anchor_vn, next_fri_vn, _W_full = get_weekly_anchor_vn()
    today_vn = _now_vn()
    W = 7.0    # tổng chiều rộng biểu đồ luôn = 7 ngày (không đổi)

    # ── Tính số ngày đến thứ 6 cuối tháng (dùng UTC cho monthly/quarterly) ──
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    days_to_friday = max(1, (next_fri_vn - today_vn).days)

    def days_to_last_friday(yr, mo):
        last_day = _monthrange(yr, mo)[1]
        d = datetime(yr, mo, last_day)
        while d.weekday() != 4:
            d -= timedelta(days=1)
        return max(1, (d - today).days)

    # Tháng này hoặc tháng sau
    M = float(days_to_last_friday(today.year, today.month))
    if M <= days_to_friday + 2:   # tháng này đã qua thứ 6 cuối → tháng sau
        nm = today.month + 1 if today.month < 12 else 1
        ny = today.year + (1 if today.month == 12 else 0)
        M  = float(days_to_last_friday(ny, nm))
    M = max(M, W + 3)

    # ── OI trend ─────────────────────────────────────────────────────────────
    oi_rising = False
    if oi_df is not None and not oi_df.empty and len(oi_df) >= 4:
        oi_vals   = oi_df["sumOpenInterestValue"].values.astype(float)
        oi_chg    = (oi_vals[-1] - oi_vals[0]) / max(oi_vals[0], 1)
        oi_rising = oi_chg > 0.015

    pc_safe = pc_ratio if (pc_ratio and pc_ratio > 0) else 0.69

    # ── Weights ───────────────────────────────────────────────────────────────
    w_A, w_B, w_C, w_D = 0.28, 0.28, 0.24, 0.20
    pc_bias    = float(np.clip((1.0 - pc_safe) * 0.12, -0.10, 0.10))
    w_A += pc_bias;  w_B -= pc_bias
    fr_bias    = float(np.clip(funding_rate_pct * 8.0, -0.10, 0.10))
    w_A -= fr_bias;  w_B += fr_bias
    dist_short = (short_cluster - current_price) / current_price
    dist_long  = (current_price - long_cluster)  / current_price
    dist_boost = float(np.clip((dist_long - dist_short) * 0.6, -0.08, 0.08))
    w_A += dist_boost;  w_B -= dist_boost
    if oi_rising:
        w_C -= 0.04; w_A += 0.02; w_B += 0.02

    # D boost khi có monthly/quarterly target rõ ràng
    if abs(funding_rate_pct) > 0.02:   w_D += 0.04
    if abs(1.0 - pc_safe)    > 0.30:   w_D += 0.03
    if oi_rising:                       w_D += 0.03
    # Nếu monthly MP xa hơn weekly → D thêm hấp dẫn
    if mmp and abs(mmp - current_price) > abs(wmp - current_price) * 1.5:
        w_D += 0.04

    w_A, w_B, w_C, w_D = max(w_A,0.06), max(w_B,0.06), max(w_C,0.06), max(w_D,0.10)
    tot = w_A + w_B + w_C + w_D
    w_A, w_B, w_C, w_D = w_A/tot, w_B/tot, w_C/tot, w_D/tot

    # Cap clusters to ±8% from current price — prevents wild OI-estimated values
    pump_top = min(short_cluster, current_price * 1.08) * 1.008
    dump_bot = max(long_cluster,  current_price * 0.92) * 0.992

    # ── Scenario A — Tuần — khung 1H — hội tụ đúng thứ 6 ──────────────────────
    move_up        = pump_top - origin_price
    fake_reject_sz = move_up * 0.055
    a_end = min(max(wmp, long_cluster * 1.01), pump_top * 0.97)
    # Scale time points theo W = 7 ngày cố định (anchor thứ 6 15:05 VN)
    pts_A = [
        (0.0,        origin_price),
        (W * 0.22,   origin_price + move_up * 0.45),
        (W * 0.37,   pump_top),
        (W * 0.40,   pump_top - fake_reject_sz * 0.55),
        (W * 0.43,   pump_top + fake_reject_sz * 0.25),
        (W * 0.46,   pump_top - fake_reject_sz * 1.10),
        (W * 0.68,   (pump_top + a_end) / 2),
        (W,          a_end),                    # ← thứ 6 expire
    ]

    # ── Scenario B — Tuần — khung 1H — hội tụ đúng thứ 6 ──────────────────────
    move_dn        = origin_price - dump_bot
    fake_bounce_sz = move_dn * 0.055
    b_end = max(min(wmp, short_cluster * 0.99), dump_bot * 1.01)
    pts_B = [
        (0.0,        origin_price),
        (W * 0.22,   origin_price - move_dn * 0.45),
        (W * 0.37,   dump_bot),
        (W * 0.40,   dump_bot + fake_bounce_sz * 0.55),
        (W * 0.43,   dump_bot - fake_bounce_sz * 0.25),
        (W * 0.46,   dump_bot + fake_bounce_sz * 1.10),
        (W * 0.68,   (dump_bot + b_end) / 2),
        (W,          b_end),                    # ← thứ 6 expire
    ]

    # ── Scenario C — Tuần — khung 1H — hội tụ đúng thứ 6 ──────────────────────
    mid = (origin_price + wmp) / 2
    pts_C = [
        (0.0,      origin_price),
        (W * 0.27, origin_price + (mid - origin_price) * 0.30),
        (W * 0.52, mid),
        (W * 0.80, (mid + wmp) / 2),
        (W,        wmp),                        # ← thứ 6 expire
    ]

    # ═══════════════════════════════════════════════════════════════════════
    # SCENARIO D — MM Master Plan (REBUILT v2)
    # Logic: tối ưu ROI từ ABC bằng cách kết hợp:
    #   1. Áp lực tường Call/Put từng ngày (daily_walls) → gravity band mỗi ngày
    #   2. Thanh khoản cluster (long_cluster / short_cluster) → sweep targets
    #   3. Max Pain 3 tầng (weekly/monthly/quarterly) → settle targets
    #
    # Tuần 15m (W ngày): 5 phase rõ ràng
    #   Phase 1 (0 → W*0.20): Ngày Mon-Tue → áp lực Put Wall, kiểm tra long_cluster
    #   Phase 2 (W*0.20 → W*0.45): Sweep long_cluster nếu gần, bounce → Mid MP
    #   Phase 3 (W*0.45 → W*0.72): Thu-Free Run → pump về Call Wall / sweep short_cluster
    #   Phase 4 (W*0.72 → W*0.92): Fake reject tại short_cluster (quét liq)
    #   Phase 5 (W*0.92 → W): Hội tụ về Weekly Max Pain (thứ 6 expire)
    # ═══════════════════════════════════════════════════════════════════════

    # ── Compute dynamic expire dates for sweep annotation (giờ VN) ────────────
    _fri_sw      = next_fri_vn   # thứ 6 tiếp theo lấy từ VN anchor
    _wk1_label   = _fri_sw.strftime("%d %b").lstrip("0")
    _wk3_label   = (_fri_sw + timedelta(weeks=2)).strftime("%d %b").lstrip("0")

    # ── Xác định hướng sweep tối ưu theo ROI ────────────────────────────────
    if dist_short <= dist_long:
        week_first_dir = "UP"
        week_sweep1    = pump_top
        week_sweep2    = dump_bot
        sweep_note = (
            f"[WEEK] Sweep longs ${long_cluster:,.0f} → shorts ${short_cluster:,.0f}"
            f" → Wk1 MP ${wmp:,.0f} [{_wk1_label}]"
            f" → Wk3 MP ${mmp:,.0f} [{_wk3_label}]"
        )
        sweep_first  = f"↑ Pump ${pump_top:,.0f}"
        sweep_second = f"↓ Dump ${dump_bot:,.0f}"
    else:
        week_first_dir = "DOWN"
        week_sweep1    = dump_bot
        week_sweep2    = pump_top
        sweep_note = (
            f"[WEEK] Sweep longs ${long_cluster:,.0f} → shorts ${short_cluster:,.0f}"
            f" → Wk1 MP ${wmp:,.0f} [{_wk1_label}]"
            f" → Wk3 MP ${mmp:,.0f} [{_wk3_label}]"
        )
        sweep_first  = f"↓ Dump ${dump_bot:,.0f}"
        sweep_second = f"↑ Pump ${pump_top:,.0f}"

    # ── Phase anchors với fake reversal ─────────────────────────────────────
    fake1 = abs(week_sweep1 - origin_price) * 0.04
    fake2 = abs(week_sweep2 - week_sweep1)   * 0.04

    if week_sweep1 > origin_price:
        sw1_a = week_sweep1 - fake1 * 0.7
        sw1_b = week_sweep1 + fake1 * 0.35
        sw2_a = week_sweep2 + fake2 * 0.7
        sw2_b = week_sweep2 - fake2 * 0.35
    else:
        sw1_a = week_sweep1 + fake1 * 0.7
        sw1_b = week_sweep1 - fake1 * 0.35
        sw2_a = week_sweep2 - fake2 * 0.7
        sw2_b = week_sweep2 + fake2 * 0.35

    # ── Sweep short_cluster: cap +7%, overshot +1.2% để quét liq ────────────
    _sc_cap     = min(short_cluster, origin_price * 1.07)
    _sc_sweep   = _sc_cap * 1.012
    _sc_fake    = _sc_cap * 1.005
    _sc_rej     = _sc_cap * 0.992
    _lc_cap     = max(long_cluster, origin_price * 0.93)

    # ── Tính mid-points theo Max Pain gravity ───────────────────────────────
    mp_mid_early  = (origin_price + wmp) / 2
    mp_mid_late   = (week_sweep2   + wmp) / 2

    # ── Call Wall / Put Wall pressure ────────────────────────────────────────
    call_wall_pressure = min(short_cluster * 0.998, wmp * 1.06)
    put_wall_pressure  = max(long_cluster  * 1.002, wmp * 0.94)

    # ── pts_D: 5 phase rõ ràng ───────────────────────────────────────────────
    pts_D = [
        # Phase 1: 0 → W*0.25 — Thứ 2-3 (Mon-Tue)
        (0.0,         origin_price),
        (W * 0.10,    origin_price + (week_sweep1 - origin_price) * 0.40),
        (W * 0.22,    week_sweep1),
        (W * 0.24,    sw1_a),
        (W * 0.26,    sw1_b),

        # Phase 2: W*0.26 → W*0.48 — Thứ 3-4 (Tue-Wed)
        (W * 0.35,    mp_mid_early),
        (W * 0.44,    wmp * 0.998),            # chạm gần Weekly MP
        (W * 0.48,    mp_mid_early),           # bounce nhẹ

        # Phase 3: W*0.48 → W*0.75 — Thứ 4-5 Free Run (Wed-Thu)
        # Pump về Call Wall → sweep short_cluster
        (W * 0.58,    (mp_mid_early + week_sweep2) / 2),
        (W * 0.68,    week_sweep2),            # chạm cluster 2
        (W * 0.70,    sw2_a),                  # fake reversal
        (W * 0.72,    sw2_b),                  # vượt qua

        # Phase 4: W*0.72 → W*0.92 — Sau Free Run (Thu sau 15h UTC)
        # Pump thêm vào Call Wall, quét short_cluster lần cuối
        (W * 0.78,    (sw2_b + _sc_fake) / 2),
        (W * 0.83,    _sc_fake),               # chạm Call Wall zone
        (W * 0.87,    _sc_sweep),              # phá qua: kích liq short (đỉnh sweep)
        (W * 0.90,    _sc_fake),               # fake reject: bắt đầu rớt
        (W * 0.92,    _sc_rej),                # về dưới Call Wall

        # Phase 5: W*0.92 → W — Thứ 6 expire
        # Gravity về Weekly Max Pain
        (W * 0.96,    (wmp + _sc_rej) / 2),
        (W,           wmp),                    # ← Weekly Max Pain (thứ 6 expire)

        # ── Monthly leg (4H khung, sau W) ──
        (M * 0.40,    (wmp + mmp) / 2 * 0.99),
        (M * 0.65,    (wmp + mmp) / 2),
        (M * 0.82,    mmp * 0.998),
        (M,           mmp),                    # ← Monthly Max Pain

        # ── Quarterly leg ──
        (M * 1.20,    (mmp + qmp) / 2),
        (M * 1.45,    qmp),                    # ← Quarterly Max Pain
    ]

    # Gắn thêm short_cluster_ref vào scen dict để dùng trong make_price_chart
    _d_short_cluster_ref = _sc_cap

    return {
        "A": {
            "label": "Pump→Dump [1H]",
            "prob": w_A, "color": "#3fb950", "points": pts_A,
            "sweep": None, "timeframe": "1H · 7 ngày",
            "rebound_pt": (2.5, pump_top,
                           f"Fake rejection ~{fake_reject_sz/pump_top*100:.1f}%"
                           f" | ~{max(2, int(fake_reject_sz/pump_top*65))}h"
                           f" | Short liq sweep"),
        },
        "B": {
            "label": "Dump→Pump [1H]",
            "prob": w_B, "color": "#f85149", "points": pts_B,
            "sweep": None, "timeframe": "1H · 7 ngày",
            "rebound_pt": (2.5, dump_bot,
                           f"Fake bounce ~{fake_bounce_sz/dump_bot*100:.1f}%"
                           f" | ~{max(2, int(fake_bounce_sz/dump_bot*65))}h"
                           f" | Long liq sweep"),
        },
        "C": {
            "label": "→Weekly MP [1H]",
            "prob": w_C, "color": "#e3b341", "points": pts_C,
            "sweep": None, "timeframe": "1H · 7 ngày",
            "rebound_pt": None,
        },
        "D": {
            "label": "MM Master Plan [4H · 30d]",
            "prob": w_D, "color": "#ffd700", "points": pts_D,
            "sweep": sweep_note,
            "sweep_first":          sweep_first,
            "sweep_second":         sweep_second,
            "timeframe":            "1H→4H→1D cascade",
            "weekly_mp":            wmp,
            "monthly_mp":           mmp,
            "quarterly_mp":         qmp,
            "W_days":               W,
            "M_days":               M,
            "short_cluster_ref":    _d_short_cluster_ref,   # capped +7%
            "long_cluster_ref":     _lc_cap,
            "call_wall_pressure":   call_wall_pressure,
            "put_wall_pressure":    put_wall_pressure,
            "rebound_pt":           None,
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
# MM WEEKLY TACTICS MODULE v1.0
# Tự động vẽ chiến thuật MM đến thứ 6 expire, làm mới mỗi tuần
# Tích hợp: Liq Clusters (ảnh AI) + Options (Max Pain / Call-Put Wall) + FR
# ═════════════════════════════════════════════════════════════════════════════

def _mm_week_key() -> str:
    """Cache key theo tuần ISO: 'YYYY-W##'  — đổi mỗi thứ 2 lúc 00:00 UTC."""
    now = datetime.now(timezone.utc)
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def _mm_hours_to_friday() -> float:
    """Số giờ từ NOW đến 15:00 UTC thứ 6 gần nhất (tối thiểu 1h)."""
    now = datetime.now(timezone.utc)
    days_to_fri = (4 - now.weekday()) % 7
    if days_to_fri == 0 and now.hour >= 15:
        days_to_fri = 7
    friday_15 = (now + timedelta(days=days_to_fri)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )
    return max(1.0, (friday_15 - now).total_seconds() / 3600.0)


def _mm_friday_dt() -> datetime:
    """Trả về datetime của 15:00 UTC thứ 6 gần nhất (naive UTC)."""
    now = datetime.now(timezone.utc)
    days_to_fri = (4 - now.weekday()) % 7
    if days_to_fri == 0 and now.hour >= 15:
        days_to_fri = 7
    return (now + timedelta(days=days_to_fri)).replace(
        hour=15, minute=0, second=0, microsecond=0, tzinfo=None
    )


def _mm_bezier_smooth(waypoints: list, n_pts: int = 120) -> list:
    """
    Tạo đường cong mượt qua waypoints bằng Catmull-Rom spline.
    Input:  [(h, price), ...]  — h tính theo giờ từ now
    Output: [(h, price), ...]  — n_pts điểm
    """
    if len(waypoints) < 2:
        return waypoints
    import random as _rnd
    rng = _rnd.Random(sum(int(p) for _, p in waypoints))

    xs = [w[0] for w in waypoints]
    ys = [w[1] for w in waypoints]

    def _catmull_rom(p0, p1, p2, p3, t):
        return 0.5 * (
            2 * p1
            + (-p0 + p2) * t
            + (2 * p0 - 5 * p1 + 4 * p2 - p3) * (t ** 2)
            + (-p0 + 3 * p1 - 3 * p2 + p3) * (t ** 3)
        )

    pts = []
    total_h = xs[-1] - xs[0]
    step = total_h / n_pts

    for i in range(n_pts + 1):
        h_cur = xs[0] + i * step
        # Find segment
        seg = 0
        for j in range(len(xs) - 1):
            if xs[j] <= h_cur <= xs[j + 1]:
                seg = j
                break
        if h_cur >= xs[-1]:
            pts.append((xs[-1], ys[-1]))
            continue

        p0 = ys[max(0, seg - 1)]
        p1 = ys[seg]
        p2 = ys[min(len(ys) - 1, seg + 1)]
        p3 = ys[min(len(ys) - 1, seg + 2)]
        span = xs[seg + 1] - xs[seg]
        t = (h_cur - xs[seg]) / span if span > 0 else 0.0
        y_cur = _catmull_rom(p0, p1, p2, p3, t)
        # Tiny noise cho tự nhiên (±0.05%)
        noise = y_cur * 0.0005 * rng.gauss(0, 1)
        pts.append((h_cur, y_cur + noise))

    return pts


def _compute_mm_weekly_tactics(
    current_price: float,
    weekly_max_pain: float,
    monthly_max_pain: float = 0.0,
    long_liq_clusters: list = None,
    short_liq_clusters: list = None,
    call_wall: float = 0.0,
    put_wall: float = 0.0,
    funding_rate_pct: float = 0.0,
) -> dict:
    """
    Tính 4 chiến thuật MM khả năng cao đến thứ 6 expire.

    Lý thuyết:
    - MM (Dealers/Market Maker) tối ưu P&L từ options → muốn giá settle tại Max Pain.
    - Trước khi settle, MM sweep liquidity clusters để tăng P&L thực thi.
    - Thứ tự sweep phụ thuộc vào vị trí giá, FR, P/C ratio.

    Input:
        long_liq_clusters  : list[{"price_level", "size_usd_millions", "strength"}]
        short_liq_clusters : same format

    Output: dict tactic với 4 kịch bản T1-T4
    """
    cp   = float(current_price)
    wmp  = float(weekly_max_pain)  if weekly_max_pain  else cp
    mmp  = float(monthly_max_pain) if monthly_max_pain else wmp * 0.95
    cw   = float(call_wall)  if call_wall  else cp * 1.05
    pw   = float(put_wall)   if put_wall   else cp * 0.95
    fr   = float(funding_rate_pct)

    H_remaining = _mm_hours_to_friday()   # giờ còn lại từ NOW đến thứ 6
    now  = datetime.now(timezone.utc).replace(tzinfo=None)
    # H_full = tổng giờ từ thứ 6 tuần trước đến thứ 6 này (trục x đầy đủ)
    # _now_h = vị trí NOW trên trục h (từ last_fri)
    # Waypoints dùng h tuyệt đối từ last_fri: 0.0 = thứ 6 trước, H_full = thứ 6 này
    H_full = 168.0   # 7 ngày × 24h — trục x cố định thứ 6 → thứ 6
    H      = H_full  # dùng H_full cho _clip_wp và P1-P5

    # ── Loc liq clusters theo khoảng ±8% từ giá hiện tại ──────────────────
    long_liq_clusters  = long_liq_clusters  or []
    short_liq_clusters = short_liq_clusters or []

    sorted_long  = sorted(
        [c for c in long_liq_clusters  if abs(c.get("price_level",0) - cp)/cp <= 0.12],
        key=lambda x: x.get("price_level", 0), reverse=True
    )
    sorted_short = sorted(
        [c for c in short_liq_clusters if abs(c.get("price_level",0) - cp)/cp <= 0.12],
        key=lambda x: x.get("price_level", 0)
    )

    # ── Xác định cluster gần nhất + mạnh nhất ──────────────────────────────
    # Long liq gần nhất (ngay dưới giá, đích sweep khi giá giảm)
    _ll_near = next(
        (c for c in sorted_long if c["price_level"] < cp * 0.999),
        {"price_level": cp * 0.930, "size_usd_millions": 50, "strength": "medium"}
    )
    # Long liq mạnh nhất (extreme/highest USD)
    _ll_big  = max(long_liq_clusters, key=lambda x: x.get("size_usd_millions", 0)) \
               if long_liq_clusters else _ll_near

    # Short liq gần nhất (ngay trên giá, đích sweep khi giá tăng)
    _sl_near = next(
        (c for c in sorted_short if c["price_level"] > cp * 1.001),
        {"price_level": cp * 1.060, "size_usd_millions": 50, "strength": "medium"}
    )
    # Short liq mạnh nhất (extreme/highest USD)
    _sl_big  = max(short_liq_clusters, key=lambda x: x.get("size_usd_millions", 0)) \
               if short_liq_clusters else _sl_near

    ll_near  = float(_ll_near["price_level"])
    ll_near_sz = float(_ll_near.get("size_usd_millions", 50))
    ll_big   = float(_ll_big["price_level"])
    ll_big_sz  = float(_ll_big.get("size_usd_millions", 50))
    sl_near  = float(_sl_near["price_level"])
    sl_near_sz = float(_sl_near.get("size_usd_millions", 50))
    sl_big   = float(_sl_big["price_level"])
    sl_big_sz  = float(_sl_big.get("size_usd_millions", 50))

    # ── Khoảng cách đến Max Pain (dương = giá cần tăng lên MP) ──────────────
    dist_to_wmp  = wmp - cp          # > 0 → giá cần TĂNG để đến weekly MP
    dist_to_mmp  = mmp - cp          # có thể âm
    dist_to_sl   = sl_near - cp      # > 0 luôn
    dist_to_ll   = cp - ll_near      # > 0 luôn

    # ── Xác suất cơ bản theo vị trí tương đối ──────────────────────────────
    # T1 (sweep short liq trước → max pain): ưu tiên khi giá dưới MP và FR âm
    # T2 (sweep long liq trước → bounce lên MP): ưu tiên khi FR dương + long liq gần
    # T3 (sweep cả 2 sides rồi settle MP): ưu tiên khi cả 2 cluster đều lớn
    # T4 (miss weekly MP, hướng về monthly MP): ưu tiên khi giá đã gần monthly MP

    # Base weights
    p1 = 0.38  # T1 sweep lên
    p2 = 0.28  # T2 sweep xuống
    p3 = 0.22  # T3 sweep hai chiều
    p4 = 0.12  # T4 monthly target

    # Điều chỉnh theo FR
    if fr > 0.01:     # FR cao → longs bị cầm chân → T2 có lợi
        p2 += 0.06; p1 -= 0.04; p3 -= 0.02
    elif fr < -0.01:  # FR âm → shorts bị squeeeze → T1 có lợi
        p1 += 0.06; p2 -= 0.04; p3 -= 0.02

    # Điều chỉnh theo khoảng cách đến Max Pain
    if dist_to_wmp > 0:   # giá dưới weekly MP → bullish bias cho T1
        p1 += min(0.06, abs(dist_to_wmp) / cp * 0.8)
        p2 -= 0.03
    else:                  # giá trên weekly MP → cần kéo xuống
        p2 += min(0.06, abs(dist_to_wmp) / cp * 0.8)
        p1 -= 0.03

    # Điều chỉnh theo kích thước short liq gần nhất (càng lớn → T1 càng hấp dẫn MM)
    if sl_near_sz > 500:   # EXTREME → sweep lên rất hấp dẫn
        p1 += 0.05; p3 += 0.02; p2 -= 0.04; p4 -= 0.03
    if ll_big_sz > 500:    # EXTREME long liq → T2/T3 hấp dẫn
        p2 += 0.04; p3 += 0.02; p1 -= 0.03; p4 -= 0.03

    # Khoảng cách đến monthly MP (nếu xa hơn 5% → T4 ít xảy ra)
    if abs(dist_to_mmp) > cp * 0.05:
        p4 = max(p4 - 0.04, 0.06)
    elif abs(dist_to_mmp) < cp * 0.02:
        p4 += 0.04

    # Normalize
    _tot = p1 + p2 + p3 + p4
    p1, p2, p3, p4 = p1/_tot, p2/_tot, p3/_tot, p4/_tot

    # ── Xây dựng waypoints cho từng tactic ─────────────────────────────────
    # Trục h tuyệt đối: 0.0 = thứ 6 tuần trước, 168.0 = thứ 6 này
    # _now_h = vị trí NOW trên trục h
    # Các chiến lược bắt đầu từ _now_h (giá hiện tại), kết thúc tại H_full

    _now_h = H_full - H_remaining   # giờ đã trôi qua từ thứ 6 trước đến NOW

    # Phase markers tính từ NOW đến expire (H_remaining giờ còn lại)
    _rem = H_remaining
    P1 = _now_h + _rem * 0.20   # Phase 1 end
    P2 = _now_h + _rem * 0.42   # Phase 2 end (Wed Free Run)
    P3 = _now_h + _rem * 0.65   # Phase 3 end
    P4 = _now_h + _rem * 0.85   # Phase 4 end
    P5 = H_full                 # Phase 5 end = thứ 6 expire

    # Fake movements (small noise để tạo cảm giác tự nhiên)
    fake_sz = cp * 0.006   # ~0.6% fake reversal

    # ── T1: Sweep Short Liq → Settle Weekly Max Pain ────────────────────────
    # Kịch bản: MM dùng Free Run Day (Wed) để pump lên sweep short cluster
    # sau đó kéo về Max Pain để tối ưu options expire
    _sl_target = min(sl_near, cp * 1.07)           # cap +7%
    _sl_sweep  = _sl_target * 1.008                 # vượt qua để trigger stops
    _sl_fake   = _sl_target * 0.996                 # fake reject ngay sau sweep
    wp_T1 = [
        (_now_h, cp),   # ← bắt đầu từ NOW trên trục thứ 6→thứ 6
        (P1*0.4, cp - fake_sz * 0.3),               # nhúng nhẹ lấy liquidity
        (P1,     cp + (_sl_target - cp) * 0.25),
        (P2,     cp + (_sl_target - cp) * 0.65),
        (P2*1.1, _sl_target),                        # chạm short cluster
        (P2*1.2, _sl_sweep),                         # vượt qua sweep stops
        (P2*1.35,_sl_fake),                          # fake reject
        (P3,     (_sl_fake + wmp) / 2),              # kéo về hướng MP
        (P4,     wmp + fake_sz * 0.5),               # chạm gần MP
        (P5,     wmp),                               # ← SETTLE tại Weekly Max Pain
    ]

    # ── T2: Sweep Long Liq → Bounce → Settle Weekly Max Pain ───────────────
    # Kịch bản: MM dump trước để sweep long liquidation cluster, sau đó bounce
    # lên Weekly Max Pain — lấy liquidity từ cả 2 phía
    _ll_target = max(ll_near, cp * 0.93)            # cap -7%
    _ll_sweep  = _ll_target * 0.992                 # vượt qua để trigger stops
    _ll_fake   = _ll_target * 1.004                 # fake bounce sau sweep
    wp_T2 = [
        (_now_h, cp),   # ← bắt đầu từ NOW
        (P1*0.5, cp + fake_sz * 0.4),               # fake pump nhỏ
        (P1,     cp - (cp - _ll_target) * 0.30),
        (P2,     cp - (cp - _ll_target) * 0.72),
        (P2*1.1, _ll_target),                        # chạm long cluster
        (P2*1.2, _ll_sweep),                         # vượt qua — sweep stops
        (P2*1.35,_ll_fake),                          # fake bottom/bounce
        (P3,     (cp + wmp) / 2),                    # bounce mạnh lên
        (P4,     wmp - fake_sz * 0.4),
        (P5,     wmp),                               # ← SETTLE Weekly Max Pain
    ]

    # ── T3: Double Sweep (Sweep Cả Hai Phía) → Max Pain ────────────────────
    # Kịch bản: MM tận dụng Free Run Day để sweep cả long liq (xuống) VÀ
    # short liq (lên) trong 1 tuần, tối đa hóa liquidity thu về
    # Thứ tự sweep phụ thuộc vào FR (FR+ → dump trước; FR- → pump trước)
    if fr >= 0:   # funding dương → long bias → pump trước sweep short, rồi dump
        wp_T3 = [
            (_now_h, cp),   # ← bắt đầu từ NOW
            (P1,     cp + (_sl_target - cp) * 0.50),
            (P1*1.5, _sl_target),                    # Sweep short cluster đầu tiên
            (P1*1.6, _sl_sweep),
            (P2,     (cp + _ll_target) / 2),         # Đảo chiều dump
            (P2*1.4, _ll_target),                    # Sweep long cluster
            (P2*1.5, _ll_sweep),
            (P3,     (cp + wmp) / 2),               # Bounce lên
            (P4,     wmp + fake_sz * 0.3),
            (P5,     wmp),                           # ← SETTLE
        ]
    else:          # funding âm → short bias → dump trước, pump sau
        wp_T3 = [
            (_now_h, cp),   # ← bắt đầu từ NOW
            (P1,     cp - (cp - _ll_target) * 0.50),
            (P1*1.5, _ll_target),                    # Sweep long cluster đầu tiên
            (P1*1.6, _ll_sweep),
            (P2,     (cp + _sl_target) / 2),         # Đảo chiều pump
            (P2*1.4, _sl_target),                    # Sweep short cluster
            (P2*1.5, _sl_sweep),
            (P3,     (wmp + _sl_target) / 2),        # Kéo về
            (P4,     wmp + fake_sz * 0.4),
            (P5,     wmp),                           # ← SETTLE
        ]

    # ── T4: Bearish Breakdown → Monthly Max Pain ───────────────────────────
    # Kịch bản: Weekly Max Pain bị xuyên thủng, giá hướng về Monthly Max Pain
    # Xảy ra khi: options flow đổi chiều, whale position lớn trái chiều
    _mmp_target = mmp if mmp < cp * 0.98 else cp * 0.92   # ít nhất -8%
    wp_T4 = [
        (_now_h, cp),   # ← bắt đầu từ NOW
        (P1,     (cp + _mmp_target) / 2 + fake_sz),
        (P2,     (cp + _mmp_target) / 2),
        (P2*1.3, _mmp_target + (cp - _mmp_target) * 0.20),
        (P3,     _mmp_target + fake_sz * 1.5),      # fake bounce
        (P3*1.2, _mmp_target - fake_sz * 0.5),      # duy trì áp lực
        (P4,     _mmp_target + fake_sz * 0.2),
        (P5,     _mmp_target),                      # ← SETTLE tại Monthly MP
    ]

    # ── Chuẩn hóa waypoints — đảm bảo H đúng ──────────────────────────────
    def _clip_wp(wps):
        clipped = []
        for h, p in wps:
            clipped.append((min(float(h), H_full), max(cp * 0.7, min(cp * 1.3, float(p)))))
        # Đảm bảo điểm cuối luôn là H_full (thứ 6 expire)
        if clipped and abs(clipped[-1][0] - H_full) > 0.1:
            clipped.append((H_full, clipped[-1][1]))
        return clipped

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    fri_dt  = _mm_friday_dt()

    return {
        "week_key":       _mm_week_key(),
        "generated_at":   now_utc.isoformat(),
        "hours_to_friday": H,
        "friday_dt":      fri_dt.isoformat(),
        "current_price":  cp,
        "weekly_max_pain": wmp,
        "monthly_max_pain": _mmp_target,
        "call_wall":      cw,
        "put_wall":       pw,
        "long_liq_near":  ll_near,
        "short_liq_near": sl_near,
        "short_liq_near_sz": sl_near_sz,
        "long_liq_near_sz":  ll_near_sz,
        "funding_rate":   fr,
        "tactics": {
            "T1": {
                "name":       "Sweep Short Liq → Max Pain",
                "name_short": "T1 ↑ Sweep → MP",
                "desc":       (
                    f"MM pump quét vùng short liq ${sl_near:,.0f} "
                    f"(${sl_near_sz:.0f}M {_sl_near.get('strength','').upper()}) "
                    f"→ kích nổ stop shorts → rớt về Weekly Max Pain ${wmp:,.0f} expire thứ 6."
                ),
                "rationale":  (
                    f"Giá hiện tại ${cp:,.0f} dưới Weekly MP ${wmp:,.0f} "
                    f"({'FR âm ← short bị squeeze' if fr < 0 else 'FR dương ← long bị cầm'}). "
                    f"Short cluster gần nhất cách {dist_to_sl/cp*100:.1f}%. "
                    f"MM pump để thu options premium từ longs expire."
                ),
                "prob":        round(p1, 3),
                "color":       "#4ade80",
                "badge_color": "#0d1f12",
                "icon":        "↑",
                "waypoints":   _clip_wp(wp_T1),
                "key_levels":  [sl_near, _sl_sweep, wmp],
                "entry_hint":  f"LONG ${cp - fake_sz:.0f} → TP ${wmp:,.0f}",
                "sl_hint":     f"SL dưới ${ll_near:,.0f}",
            },
            "T2": {
                "name":       "Sweep Long Liq → Bounce MP",
                "name_short": "T2 ↓ Sweep → Bounce",
                "desc":       (
                    f"MM dump quét vùng long liq ${ll_near:,.0f} "
                    f"(${ll_near_sz:.0f}M {_ll_near.get('strength','').upper()}) "
                    f"→ stop hunt longs → bounce mạnh về Weekly Max Pain ${wmp:,.0f}."
                ),
                "rationale":  (
                    f"Funding rate {fr:+.4f}% "
                    f"{'→ long FOMO cao, MM sẽ dump để lấy liquidity từ stops.' if fr > 0 else '→ short FOMO, MM sẽ tạo fake dump.'}. "
                    f"Long cluster cách {dist_to_ll/cp*100:.1f}% dưới. "
                    f"Bounce lên Max Pain ${wmp:,.0f} sau khi sweep."
                ),
                "prob":        round(p2, 3),
                "color":       "#fb923c",
                "badge_color": "#1f1208",
                "icon":        "↓",
                "waypoints":   _clip_wp(wp_T2),
                "key_levels":  [ll_near, _ll_sweep, wmp],
                "entry_hint":  f"LONG ${ll_near:,.0f} (sau khi wick xuống) → TP ${wmp:,.0f}",
                "sl_hint":     f"SL dưới ${ll_big:,.0f}",
            },
            "T3": {
                "name":       "Double Sweep Cả 2 Phía → Max Pain",
                "name_short": "T3 ↕ Double Sweep",
                "desc":       (
                    f"MM {'pump trước' if fr >= 0 else 'dump trước'} sweep "
                    f"{'short' if fr >= 0 else 'long'} liq, rồi đảo chiều sweep "
                    f"{'long' if fr >= 0 else 'short'} liq — thu tối đa liquidity "
                    f"cả 2 phía → settle tại ${wmp:,.0f}."
                ),
                "rationale":  (
                    f"Thứ 4 Free Run Day (không có tường options) → "
                    f"MM tự do biến động mạnh. Với liq ${ll_near_sz:.0f}M long + "
                    f"${sl_near_sz:.0f}M short, double sweep tối ưu ROI nhất."
                ),
                "prob":        round(p3, 3),
                "color":       "#ffd700",
                "badge_color": "#1a1600",
                "icon":        "↕",
                "waypoints":   _clip_wp(wp_T3),
                "key_levels":  [ll_near, sl_near, wmp],
                "entry_hint":  f"Watch cả 2 phía, entry tại đáy sweep → TP ${wmp:,.0f}",
                "sl_hint":     f"SL dưới ${ll_big:,.0f}",
            },
            "T4": {
                "name":       "Phá Weekly MP → Monthly Max Pain",
                "name_short": "T4 ↓ Monthly MP",
                "desc":       (
                    f"Kịch bản Black Swan: giá xuyên thủng Weekly Max Pain ${wmp:,.0f} "
                    f"→ options flow đổi chiều → MM kéo về Monthly Max Pain "
                    f"${_mmp_target:,.0f} tại expire."
                ),
                "rationale":  (
                    f"Xảy ra khi: macro shock, whale position lớn, "
                    f"hoặc OI đột biến. Monthly MP ${mmp:,.0f} là target thứ cấp. "
                    f"Xác suất thấp nhưng bắt buộc phải plan cho."
                ),
                "prob":        round(p4, 3),
                "color":       "#f87171",
                "badge_color": "#1f0d0d",
                "icon":        "↓↓",
                "waypoints":   _clip_wp(wp_T4),
                "key_levels":  [_ll_sweep, _mmp_target],
                "entry_hint":  f"SHORT khi giá phá ${wmp:,.0f} → TP ${_mmp_target:,.0f}",
                "sl_hint":     f"SL trên ${wmp + fake_sz:,.0f}",
            },
        },
    }


def _build_mm_tactics_chart(tactics_data: dict) -> "go.Figure":
    """
    Vẽ biểu đồ Plotly cho 4 chiến thuật MM đến thứ 6 expire.
    Fix: dùng pd.Timestamp + shapes/annotations thay vì add_vline
    để tránh lỗi 'int + datetime' trong Plotly.
    """
    if not tactics_data or "tactics" not in tactics_data:
        return None

    cp    = float(tactics_data["current_price"])
    wmp   = float(tactics_data["weekly_max_pain"])
    mmp   = float(tactics_data.get("monthly_max_pain", 0) or 0)
    cw    = float(tactics_data.get("call_wall", 0) or 0)
    pw    = float(tactics_data.get("put_wall",  0) or 0)
    ll    = float(tactics_data.get("long_liq_near",  0) or 0)
    sl    = float(tactics_data.get("short_liq_near", 0) or 0)
    H     = float(tactics_data["hours_to_friday"])

    fri_dt_str = tactics_data.get("friday_dt", "")

    # ── Dùng pd.Timestamp cho toàn bộ time axis ─────────────────────────────
    # pd.Timestamp tương thích Plotly 100% — tránh lỗi 'int + datetime'
    now_ts  = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    try:
        fri_ts = pd.Timestamp(datetime.fromisoformat(fri_dt_str))
    except Exception:
        fri_ts = now_ts + pd.Timedelta(hours=H)

    # Thứ 6 tuần trước (chart bắt đầu từ đây)
    last_fri_ts = fri_ts - pd.Timedelta(days=7)

    # h=0.0 = thứ 6 tuần trước (last_fri_ts), h tăng dần đến H = thứ 6 này
    # NOW là điểm trên trục thời gian = elapsed giờ kể từ last_fri_ts
    _elapsed_h = (now_ts - last_fri_ts).total_seconds() / 3600.0

    def _h_to_ts(h: float) -> pd.Timestamp:
        """Giờ tuyệt đối từ thứ 6 tuần trước. h=0 → last_fri_ts, h=H → fri_ts."""
        return last_fri_ts + pd.Timedelta(hours=float(h))

    # ── Tính y-range cho layout trước để canh annotation ────────────────────
    all_ys = [cp, wmp]
    if mmp: all_ys.append(mmp)
    for tac in tactics_data["tactics"].values():
        for _, p in tac.get("waypoints", []):
            all_ys.append(float(p))
    y_min = min(all_ys) * 0.992
    y_max = max(all_ys) * 1.008

    fig = go.Figure()
    shapes      = []
    annotations = []
    tactics = tactics_data["tactics"]

    # ── Vẽ từng tactic line ─────────────────────────────────────────────────
    for tkey, tac in tactics.items():
        wp   = tac["waypoints"]
        pts  = _mm_bezier_smooth(wp, n_pts=150)
        col  = tac["color"]
        prob = float(tac["prob"])
        lw   = 3.5 if prob >= 0.35 else (2.5 if prob >= 0.20 else 1.8)
        dash = "solid" if prob >= 0.35 else ("dash" if prob >= 0.20 else "dot")
        op   = 0.92 if prob >= 0.35 else (0.75 if prob >= 0.20 else 0.55)
        name_short = tac["name_short"]
        prob_pct = f"{prob*100:.0f}%"

        xs = [_h_to_ts(h) for h, _ in pts]
        ys = [float(p) for _, p in pts]

        # ── Shadow band ──────────────────────────────────────────────────────
        band_w = cp * 0.003
        r, g, b = (int(col.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        fig.add_trace(go.Scatter(
            x=xs + xs[::-1],
            y=[y + band_w for y in ys] + [y - band_w for y in ys[::-1]],
            fill="toself",
            fillcolor=f"rgba({r},{g},{b},0.06)",
            line=dict(width=0), showlegend=False, hoverinfo="skip",
        ))

        # ── Main line ────────────────────────────────────────────────────────
        fig.add_trace(go.Scatter(
            x=xs, y=ys,
            mode="lines",
            name=f"{name_short} ({prob_pct})",
            line=dict(color=col, width=lw, dash=dash),
            opacity=op,
            hovertemplate=(
                f"<b>{tac['name']}</b><br>"
                f"Giá: $%{{y:,.0f}}<br>"
                f"XS: {prob_pct}<extra></extra>"
            ),
        ))

        # ── Endpoint dot ─────────────────────────────────────────────────────
        end_ts = _h_to_ts(float(wp[-1][0]))
        end_y  = float(wp[-1][1])
        fig.add_trace(go.Scatter(
            x=[end_ts], y=[end_y],
            mode="markers+text",
            marker=dict(color=col, size=9, symbol="circle",
                        line=dict(color="#0d1117", width=2)),
            text=[f" {tac['icon']} {prob_pct}"],
            textfont=dict(color=col, size=8, family="monospace"),
            textposition="middle right",
            showlegend=False, hoverinfo="skip",
        ))

    # ── Vẽ vlines dùng shapes (không dùng add_vline — tránh lỗi int+datetime) ─
    def _vline_shape(ts: pd.Timestamp, color: str, width: float,
                     dash: str, layer: str = "above") -> dict:
        return dict(
            type="line", xref="x", yref="paper",
            x0=ts, x1=ts, y0=0, y1=1,
            line=dict(color=color, width=width, dash=dash),
            layer=layer,
        )

    def _vline_ann(ts: pd.Timestamp, text: str, color: str,
                   size: int = 9, yanchor: str = "top") -> dict:
        return dict(
            x=ts, xref="x", y=1.01, yref="paper",
            text=f"<b>{text}</b>",
            showarrow=False,
            font=dict(color=color, size=size, family="monospace"),
            xanchor="center", yanchor="bottom",
            bgcolor="rgba(13,17,23,0.85)",
            borderpad=2,
        )

    # NOW line
    shapes.append(_vline_shape(now_ts, "#f7931a", 1.8, "dashdot"))
    annotations.append(_vline_ann(now_ts, "▶ NOW", "#f7931a", size=10))

    # Last Friday (start of week) line
    shapes.append(_vline_shape(last_fri_ts, "#484f58", 1.5, "dot"))
    annotations.append(_vline_ann(last_fri_ts,
                                  f"↩ Prev Expire {last_fri_ts.strftime('%d/%m')}",
                                  "#484f58", size=8))

    # Friday expire line
    shapes.append(_vline_shape(fri_ts, "#ffd700", 2.2, "solid"))
    annotations.append(_vline_ann(fri_ts, "⏰ EXPIRE Thứ 6", "#ffd700", size=10))

    # Day separator lines — toàn bộ tuần từ thứ 6 trước đến thứ 6 này
    _day_names_full = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    # Vẽ từng ngày trong khoảng [last_fri_ts, fri_ts)
    _cur = last_fri_ts + pd.Timedelta(days=1)
    while _cur < fri_ts + pd.Timedelta(hours=1):
        d_ts = pd.Timestamp(_cur.year, _cur.month, _cur.day, 0, 0, 0)
        if d_ts <= last_fri_ts or d_ts > fri_ts:
            _cur += pd.Timedelta(days=1)
            continue
        _wd     = d_ts.weekday()
        _dn     = _day_names_full[_wd]
        _is_wed = (_wd == 2)   # thứ 4 FREE RUN
        _is_fri = (_wd == 4)   # thứ 6 expire (đã có đường riêng)
        if _is_fri:
            _cur += pd.Timedelta(days=1)
            continue
        _col = "#ffd700" if _is_wed else "#2a3240"
        _lw  = 1.2 if _is_wed else 0.7
        shapes.append(_vline_shape(d_ts, _col, _lw, "dot", layer="below"))
        _txt = f"⚡ {_dn} FREE RUN" if _is_wed else _dn
        annotations.append(_vline_ann(d_ts, _txt, _col,
                                      size=9 if _is_wed else 8))
        _cur += pd.Timedelta(days=1)

    # ── Horizontal reference lines (dùng shapes — add_hline cũng OK cho floats)
    ref_lines = []
    if wmp:
        ref_lines.append((wmp, "#bc8cff", "dash", 2.0,
                          f"Weekly Max Pain ${wmp:,.0f}"))
    if mmp and abs(mmp - wmp) > cp * 0.005:
        ref_lines.append((mmp, "#58a6ff", "dash", 1.5,
                          f"Monthly Max Pain ${mmp:,.0f}"))
    if cw and abs(cw - cp) / cp < 0.12:
        ref_lines.append((cw, "#4ade80", "dot", 1.2,
                          f"Call Wall ${cw:,.0f}"))
    if pw and abs(pw - cp) / cp < 0.12:
        ref_lines.append((pw, "#fb923c", "dot", 1.2,
                          f"Put Wall ${pw:,.0f}"))
    if sl and 0 < abs(sl - cp) / cp < 0.12:
        ref_lines.append((sl, "#22c55e", "dashdot", 1.1,
                          f"Short Liq ${sl:,.0f}"))
    if ll and 0 < abs(ll - cp) / cp < 0.12:
        ref_lines.append((ll, "#ef4444", "dashdot", 1.1,
                          f"Long Liq ${ll:,.0f}"))
    # Current price
    ref_lines.append((cp, "#f7931a", "dot", 1.0, f"NOW ${cp:,.0f}"))

    for y_val, col_h, dash_h, lw_h, label_h in ref_lines:
        shapes.append(dict(
            type="line", xref="paper", yref="y",
            x0=0, x1=1, y0=y_val, y1=y_val,
            line=dict(color=col_h, width=lw_h, dash=dash_h),
            opacity=0.65, layer="below",
        ))
        annotations.append(dict(
            xref="paper", x=1.002, yref="y", y=y_val,
            text=f" {label_h}",
            showarrow=False,
            font=dict(color=col_h, size=8, family="monospace"),
            xanchor="left", yanchor="middle",
        ))

    # ── Layout ──────────────────────────────────────────────────────────────
    week_k   = tactics_data.get("week_key", "")
    days_lbl = f"{H/24:.1f} ngày"
    H_int    = float(H)
    _fri_lbl      = fri_ts.strftime("%d/%m")
    _last_fri_lbl = last_fri_ts.strftime("%d/%m")

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0d1117",
        plot_bgcolor="#0f1318",
        height=520,
        title=dict(
            text=(
                f"🎯 MM Weekly Tactics — {week_k}  ·  "
                f"<span style='color:#f7931a'>${cp:,.0f}</span>  →  "
                f"<span style='color:#bc8cff'>MP ${wmp:,.0f}</span>  ·  "
                f"<span style='color:#ffd700'>{days_lbl} đến expire</span>"
                f"  ·  "
                f"<span style='color:#484f58'>Thứ 6 {_last_fri_lbl} → Thứ 6 {_fri_lbl}</span>"
            ),
            font=dict(color="#e6edf3", size=12, family="monospace"),
            x=0.01,
        ),
        xaxis=dict(
            type="date",
            gridcolor="#1a2028",
            tickfont=dict(size=9, color="#8b949e"),
            tickformat="%a %d/%m\n%H:%M",
            range=[
                (last_fri_ts - pd.Timedelta(hours=1)).isoformat(),
                (fri_ts + pd.Timedelta(hours=4)).isoformat(),
            ],
        ),
        yaxis=dict(
            gridcolor="#1a2028",
            tickprefix="$",
            tickformat=",.0f",
            tickfont=dict(size=9, color="#8b949e"),
            title=dict(text="BTC Price (USD)", font=dict(size=9, color="#484f58")),
            range=[y_min, y_max],
        ),
        legend=dict(
            bgcolor="rgba(13,17,23,0.92)",
            bordercolor="#30363d",
            borderwidth=1,
            font=dict(size=9, color="#c9d1d9"),
            x=0.0, y=-0.13,
            xanchor="left", yanchor="top",
            orientation="h",
        ),
        shapes=shapes,
        annotations=annotations,
        margin=dict(l=10, r=145, t=55, b=60),
        hovermode="x unified",
    )

    return fig


def render_mm_weekly_tactics_widget(
    current_price: float = 0.0,
    weekly_max_pain: float = 0.0,
    monthly_max_pain: float = 0.0,
    daily_walls: dict = None,
    funding_rate_pct: float = 0.0,
):
    """
    Widget hiển thị 4 chiến thuật MM đến thứ 6 expire.

    Features:
    - Auto cache theo tuần ISO — vẽ lại mỗi tuần mới (thứ 2)
    - Tích hợp liq clusters từ AI ảnh (session_state._inline_liq_analysis)
    - Tích hợp options data (Max Pain, Call/Put Wall)
    - 4 tactic paths (T1-T4) với xác suất + rationale
    - Manual refresh button
    """
    # ── Session state init ───────────────────────────────────────────────────
    for _k in ("_mm_tactics_data", "_mm_tactics_fig", "_mm_tactics_week_key"):
        if _k not in st.session_state:
            st.session_state[_k] = None

    with st.expander(
        "🎯 MM Weekly Tactics — Chiến Thuật Market Maker đến Thứ 6 Expire",
        expanded=True,
    ):
        # ── Header card ─────────────────────────────────────────────────────
        _now_utc = datetime.now(timezone.utc)
        _dtf     = (4 - _now_utc.weekday()) % 7
        if _dtf == 0 and _now_utc.hour >= 15:
            _dtf = 7
        _fri_str = (_now_utc + timedelta(days=_dtf)).strftime("%d/%m/%Y 15:00 UTC")
        _week_k  = _mm_week_key()

        _hcard(f"""
        <div style="background:#0d1117;border:1px solid #bc8cff44;border-radius:10px;
             padding:12px 16px;margin-bottom:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <div style="color:#e6edf3;font-weight:700;font-size:.95rem;">
                🎯 Chiến Thuật MM → Expire Thứ 6 &nbsp;
                <span style="color:#ffd700;font-size:.75rem;">{_fri_str}</span>
              </div>
              <div style="color:#8b949e;font-size:.75rem;margin-top:2px;">
                Dựa trên: Liquidation Clusters · Options Max Pain · Call/Put Wall · Funding Rate
                &nbsp;·&nbsp; <span style="color:#4ade80;">Cache key: {_week_k}</span>
              </div>
            </div>
            <div style="text-align:right;font-size:.7rem;color:#484f58;">
              Auto-refresh mỗi tuần mới (thứ 2)
            </div>
          </div>
        </div>""")

        # ── Get call/put wall from daily_walls ───────────────────────────────
        _call_wall, _put_wall = 0.0, 0.0
        if daily_walls:
            for _dk in sorted(daily_walls.keys()):
                _w = daily_walls.get(_dk, {})
                if _w.get("wall_active") and _w.get("call_wall"):
                    _call_wall = float(_w.get("call_wall") or 0)
                    _put_wall  = float(_w.get("put_wall")  or 0)
                    break

        # ── Get liq clusters from AI analysis ────────────────────────────────
        _ai_liq = (st.session_state.get("_inline_liq_analysis")
                   or st.session_state.get("_liq_ai_analysis"))
        _long_liq_clusters  = []
        _short_liq_clusters = []
        if _ai_liq and "error" not in _ai_liq:
            _long_liq_clusters  = _ai_liq.get("long_liquidation_clusters",  [])
            _short_liq_clusters = _ai_liq.get("short_liquidation_clusters", [])

        # ── Context badges ────────────────────────────────────────────────────
        _has_liq = bool(_long_liq_clusters or _short_liq_clusters)
        _has_mp  = bool(weekly_max_pain)
        _has_cw  = bool(_call_wall or _put_wall)
        _badges = []
        if _has_liq:
            _nl = len(_long_liq_clusters); _ns = len(_short_liq_clusters)
            _badges.append(f'<span style="background:#1a0d0d;color:#f87171;border:1px solid #f8717144;'
                          f'padding:2px 10px;border-radius:20px;font-size:.72rem;">'
                          f'🔴 {_nl} Long Liq</span>')
            _badges.append(f'<span style="background:#0d1a0d;color:#4ade80;border:1px solid #4ade8044;'
                          f'padding:2px 10px;border-radius:20px;font-size:.72rem;">'
                          f'🟢 {_ns} Short Liq</span>')
        else:
            _badges.append('<span style="background:#1a1400;color:#fbbf24;border:1px solid #fbbf2444;'
                          'padding:2px 10px;border-radius:20px;font-size:.72rem;">'
                          '⚠ Chưa có liq data (upload ảnh AI để cải thiện)</span>')
        if _has_mp:
            _badges.append(f'<span style="background:#150d20;color:#bc8cff;border:1px solid #bc8cff44;'
                          f'padding:2px 10px;border-radius:20px;font-size:.72rem;">'
                          f'Max Pain ${weekly_max_pain:,.0f}</span>')
        if _has_cw:
            _badges.append(f'<span style="background:#0d1a0d;color:#4ade80;border:1px solid #4ade8044;'
                          f'padding:2px 10px;border-radius:20px;font-size:.72rem;">'
                          f'CW ${_call_wall:,.0f} / PW ${_put_wall:,.0f}</span>')

        _hcard(f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;">'
               + "".join(_badges) + "</div>")

        # ── Check if we need to regenerate (new week or no data) ─────────────
        _stored_week  = st.session_state._mm_tactics_week_key
        _need_regen   = (_stored_week != _week_k
                         or st.session_state._mm_tactics_data is None
                         or st.session_state._mm_tactics_fig  is None)

        # ── Action buttons ────────────────────────────────────────────────────
        _bc1, _bc2, _bc3 = st.columns([2, 1, 1])
        with _bc1:
            if _need_regen:
                _btn_label = "🎯 Tạo Chiến Thuật MM Tuần Này"
                _btn_type  = "primary"
            else:
                _btn_label = f"🔄 Làm Mới (đang dùng cache {_stored_week})"
                _btn_type  = "secondary"
            _do_gen = st.button(_btn_label, type=_btn_type,
                                use_container_width=True, key="mm_tac_gen_btn")
        with _bc2:
            _do_reset = st.button("🗑 Xóa Cache", use_container_width=True,
                                  key="mm_tac_reset_btn")
        with _bc3:
            if current_price > 0:
                st.markdown(
                    f'<div style="background:#0d1117;border:1px solid #f7931a44;'
                    f'border-radius:8px;padding:6px;text-align:center;font-size:.78rem;">'
                    f'<span style="color:#8b949e;">BTC:</span> '
                    f'<span style="color:#f7931a;font-weight:700;">${current_price:,.0f}</span>'
                    f'</div>', unsafe_allow_html=True,
                )

        if _do_reset:
            st.session_state._mm_tactics_data     = None
            st.session_state._mm_tactics_fig      = None
            st.session_state._mm_tactics_week_key = None
            st.rerun()

        # ── Auto-generate on new week ─────────────────────────────────────────
        if _need_regen and not _do_gen:
            st.info(
                f"⏳ Tuần mới ({_week_k}) hoặc chưa có tactics — "
                f"nhấn **🎯 Tạo Chiến Thuật MM Tuần Này** để vẽ."
            )

        if _do_gen or (_need_regen and st.session_state._mm_tactics_data is None and current_price > 0):
            if current_price <= 0:
                st.warning("Cần có giá BTC để tạo chiến thuật.")
            else:
                with st.spinner("📐 Đang tính toán 4 chiến thuật MM…"):
                    _tactics = _compute_mm_weekly_tactics(
                        current_price     = current_price,
                        weekly_max_pain   = weekly_max_pain or current_price * 1.02,
                        monthly_max_pain  = monthly_max_pain or current_price * 0.93,
                        long_liq_clusters = _long_liq_clusters,
                        short_liq_clusters= _short_liq_clusters,
                        call_wall         = _call_wall,
                        put_wall          = _put_wall,
                        funding_rate_pct  = funding_rate_pct,
                    )
                    _fig = _build_mm_tactics_chart(_tactics)
                st.session_state._mm_tactics_data     = _tactics
                st.session_state._mm_tactics_fig      = _fig
                st.session_state._mm_tactics_week_key = _week_k
                st.rerun()

        # ── Render stored tactics ─────────────────────────────────────────────
        _tdata = st.session_state._mm_tactics_data
        _tfig  = st.session_state._mm_tactics_fig

        if _tdata and _tfig:
            # Chart
            st.plotly_chart(_tfig, use_container_width=True,
                            config={"displayModeBar": False})

            st.markdown("---")
            _hcard('<div style="color:#e6edf3;font-weight:700;font-size:.85rem;'
                   'margin-bottom:10px;">📋 Chi Tiết 4 Chiến Thuật MM</div>')

            # Tactics cards — 2×2 grid
            _tactics_dict = _tdata["tactics"]
            _T_keys = list(_tactics_dict.keys())
            _row1 = st.columns(2)
            _row2 = st.columns(2)
            _cols  = [_row1[0], _row1[1], _row2[0], _row2[1]]

            for _col, _tk in zip(_cols, _T_keys):
                _t   = _tactics_dict[_tk]
                _col_c = _t["color"]
                _bg_c  = _t["badge_color"]
                _prob  = _t["prob"]
                _prob_pct = f"{_prob*100:.0f}%"
                _bar_w = f"{_prob*100:.0f}%"
                _prob_c = ("#4ade80" if _prob >= 0.35 else
                           "#fbbf24" if _prob >= 0.20 else "#8b949e")
                _lv_str = " · ".join(f"${lv:,.0f}" for lv in _t.get("key_levels", []))
                with _col:
                    _hcard(f"""
                    <div style="background:{_bg_c};border:1px solid {_col_c}44;
                         border-top:3px solid {_col_c};border-radius:10px;
                         padding:12px 14px;margin-bottom:10px;">
                      <div style="display:flex;justify-content:space-between;
                           align-items:center;margin-bottom:6px;">
                        <span style="color:{_col_c};font-weight:700;font-size:.88rem;">
                          {_t['icon']} {_t['name_short']}
                        </span>
                        <span style="background:#0d1117;color:{_prob_c};
                              font-size:.68rem;padding:2px 9px;border-radius:20px;
                              border:1px solid {_prob_c}44;">
                          XS: {_prob_pct}
                        </span>
                      </div>
                      <!-- Probability bar -->
                      <div style="background:#161b22;border-radius:4px;height:4px;margin-bottom:8px;">
                        <div style="background:{_col_c};width:{_bar_w};height:4px;
                             border-radius:4px;opacity:.8;"></div>
                      </div>
                      <div style="color:#c9d1d9;font-size:.77rem;line-height:1.5;
                           margin-bottom:8px;">{_t['desc']}</div>
                      <div style="background:#0d1117;border-radius:6px;padding:8px;
                           font-size:.72rem;">
                        <div style="color:#8b949e;margin-bottom:3px;">💡 Lý do:</div>
                        <div style="color:#c9d1d9;line-height:1.5;">{_t['rationale']}</div>
                      </div>
                      <div style="margin-top:8px;display:grid;grid-template-columns:1fr 1fr;
                           gap:4px;font-size:.7rem;">
                        <div style="background:#0d1117;border-radius:5px;padding:5px 7px;">
                          <div style="color:#8b949e;">Entry hint</div>
                          <div style="color:{_col_c};font-weight:600;">{_t.get('entry_hint','—')}</div>
                        </div>
                        <div style="background:#0d1117;border-radius:5px;padding:5px 7px;">
                          <div style="color:#8b949e;">Key levels</div>
                          <div style="color:#e6edf3;font-size:.65rem;">{_lv_str}</div>
                        </div>
                      </div>
                    </div>""")

            # ── Probability summary bar ───────────────────────────────────────
            st.markdown("<br>", unsafe_allow_html=True)
            _probs = {_tk: _tactics_dict[_tk]["prob"] for _tk in _T_keys}
            _colors = {_tk: _tactics_dict[_tk]["color"] for _tk in _T_keys}
            _prob_html = '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 14px;">'
            _prob_html += '<div style="color:#8b949e;font-size:.7rem;margin-bottom:6px;">📊 Phân bổ xác suất chiến thuật tuần này:</div>'
            _prob_html += '<div style="display:flex;gap:0;border-radius:6px;overflow:hidden;height:20px;">'
            for _tk in _T_keys:
                _p  = _probs[_tk]
                _c  = _colors[_tk]
                _n  = _tactics_dict[_tk]["name_short"]
                _prob_html += (
                    f'<div style="width:{_p*100:.1f}%;background:{_c};'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'font-size:.65rem;font-weight:700;color:#0d1117;'
                    f'white-space:nowrap;overflow:hidden;padding:0 4px;" '
                    f'title="{_n}: {_p*100:.0f}%">{_p*100:.0f}%</div>'
                )
            _prob_html += '</div>'
            _prob_html += '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:6px;">'
            for _tk in _T_keys:
                _c = _colors[_tk]
                _n = _tactics_dict[_tk]["name_short"]
                _p = _probs[_tk]
                _prob_html += (
                    f'<span style="font-size:.7rem;">'
                    f'<span style="color:{_c};">■</span> {_n}: <b style="color:#e6edf3;">{_p*100:.0f}%</b>'
                    f'</span>'
                )
            _prob_html += '</div></div>'
            _hcard(_prob_html)

            # ── Meta info ─────────────────────────────────────────────────────
            _gen = _tdata.get("generated_at","")[:16].replace("T"," ")
            _hcard(
                f'<div style="color:#484f58;font-size:.7rem;margin-top:6px;text-align:right;">'
                f'Generated: {_gen} UTC  ·  Week: {_tdata.get("week_key","")}  ·  '
                f'H đến expire: {_tdata.get("hours_to_friday",0):.1f}h</div>'
            )


# ---------------------------------------------------------------------------
# Liquidation cluster estimation
# ---------------------------------------------------------------------------

def fetch_futures_klines_1h(symbol="BTCUSDT", limit=168):
    """1-hour futures klines — giữ lại cho price chart."""
    try:
        import requests as _req
        r = _req.get(
            f"{BINANCE_FUTURES}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1h", "limit": limit},
            timeout=12,
        )
        r.raise_for_status()
        df = pd.DataFrame(r.json(), columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        return df[["open_time", "open", "high", "low", "close"]].rename(
            columns={"open_time": "ts", "close": "price"}
        )
    except Exception:
        return pd.DataFrame()

def fetch_oi_1h(symbol="BTCUSDT", limit=168):
    """1-hour open interest history."""
    try:
        import requests as _req
        r = _req.get(
            f"{BINANCE_FUTURES}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "1h", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        df["ts"] = pd.to_datetime(df["timestamp"], unit="ms")
        df["oi"] = df["sumOpenInterest"].astype(float)
        return df[["ts", "oi"]]
    except Exception:
        return pd.DataFrame()

def calculate_liq_clusters(price_df, oi_df, ls_df, current_price):
    """REMOVED — long/short cluster calculation disabled."""
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Chart builders — market data
# ---------------------------------------------------------------------------

# Candle signal colors: (fill_color, line_color)
SIGNAL_COLORS = {
    "STRONG_BULL":   ("#ffd700", "#b8860b"),
    "WEAK_BULL":     ("#6b5800", "#4a3e00"),
    "NEUTRAL":       ("#484f58", "#30363d"),
    "WEAK_BEAR":     ("#6b3400", "#4a2500"),
    "STRONG_BEAR":   ("#f85149", "#c0392b"),
    "REVERSAL_UP":   ("#3fb950", "#2ea043"),
    "REVERSAL_DOWN": ("#e040fb", "#a000c0"),
}
_SIGNAL_LABELS = {
    "STRONG_BULL":   "Strong Bull",
    "WEAK_BULL":     "Weak Bull",
    "NEUTRAL":       "Neutral",
    "WEAK_BEAR":     "Weak Bear",
    "STRONG_BEAR":   "Strong Bear",
    "REVERSAL_UP":   "⬆ Reversal Up",
    "REVERSAL_DOWN": "⬇ Reversal Down",
}


def compute_candle_signals(df, ls_ratio_val=0.5):
    """
    Classify each candle by signal strength using CVD (volume delta) and momentum.
    Requires 'taker_buy_base' and 'volume' columns from Binance klines.
    """
    if df.empty or "taker_buy_base" not in df.columns:
        df = df.copy()
        df["signal"] = "NEUTRAL"
        return df
    df = df.copy()
    df["_tbb"]  = df["taker_buy_base"].astype(float)
    df["_vol"]  = df["volume"].astype(float)
    df["delta"] = 2.0 * df["_tbb"] - df["_vol"]   # positive = net buy pressure
    mu, sig     = df["delta"].mean(), df["delta"].std() + 1e-9
    df["dz"]    = (df["delta"] - mu) / sig
    df["mom"]   = df["close"].pct_change(3).fillna(0)

    net_long_pct  = float(ls_ratio_val) * 100.0
    net_short_pct = 100.0 - net_long_pct
    n = len(df)

    def _cls(idx, dz, mom):
        if idx >= n - 5:
            if net_short_pct > 58 and mom > -0.005:
                return "REVERSAL_UP"
            if net_long_pct  > 58 and mom <  0.005:
                return "REVERSAL_DOWN"
        if dz >  1.5: return "STRONG_BULL"
        if dz >  0.5: return "WEAK_BULL"
        if dz < -1.5: return "STRONG_BEAR"
        if dz < -0.5: return "WEAK_BEAR"
        return "NEUTRAL"

    df["signal"] = [_cls(i, row.dz, row.mom)
                    for i, row in enumerate(df.itertuples(index=False))]
    return df


def make_price_chart(df, tf_label="", current_price=None, max_pain=None,
                     weekly_max_pain=None, scenarios=None,
                     ls_ratio_val=0.5, net_long_pct=50.0, current_fr=0.0,
                     long_cluster=None, short_cluster=None,
                     long_cluster_liq_usd=0.0, short_cluster_liq_usd=0.0,
                     timeframe="1H", daily_walls=None,
                     monthly_max_pain=None, ai_liq_clusters=None):
    fig = go.Figure()

    # ── Standard green/red candles ────────────────────────────────────────────
    if not df.empty:
        fig.add_trace(go.Candlestick(
            x=df["open_time"],
            open=df["open"], high=df["high"],
            low=df["low"],  close=df["close"],
            increasing_fillcolor="#3fb950",
            decreasing_fillcolor="#f85149",
            increasing_line_color="#2ea043",
            decreasing_line_color="#c0392b",
            name="BTC/USDT",
            showlegend=False,
            whiskerwidth=0.3,
        ))

    title_text = f"BTC/USDT · {tf_label}" if tf_label else "BTC/USDT"

    shapes      = []
    annotations = []

    # ── Current price dotted line ─────────────────────────────────────────────
    if current_price:
        shapes.append(dict(
            type="line", xref="paper", x0=0, x1=1,
            yref="y", y0=current_price, y1=current_price,
            line=dict(color="#f7931a", width=1, dash="dot"),
        ))
        annotations.append(dict(
            xref="paper", x=1.0, yref="y", y=current_price,
            text=f"  ${current_price:,.2f}",
            showarrow=False,
            font=dict(color="#f7931a", size=11, family="monospace"),
            xanchor="left",
        ))

    # ── Monthly max pain line (blue, replaces all-expiry max pain) ───────────
    _monthly_mp = monthly_max_pain or max_pain
    if _monthly_mp:
        shapes.append(dict(
            type="line", xref="paper", x0=0, x1=1,
            yref="y", y0=_monthly_mp, y1=_monthly_mp,
            line=dict(color="#58a6ff", width=1.8, dash="dash"),
        ))
        _mp_label = "Monthly Max Pain" if monthly_max_pain else "Max Pain"
        annotations.append(dict(
            xref="paper", x=1.0, yref="y", y=_monthly_mp,
            text=f"  {_mp_label} ${_monthly_mp:,.0f}",
            showarrow=False,
            font=dict(color="#58a6ff", size=11, family="monospace"),
            xanchor="left",
        ))

    # ── Weekly (next Friday) max pain line (violet, brighter) ────────────────
    if weekly_max_pain and weekly_max_pain != max_pain:
        shapes.append(dict(
            type="line", xref="paper", x0=0, x1=1,
            yref="y", y0=weekly_max_pain, y1=weekly_max_pain,
            line=dict(color="#bc8cff", width=2, dash="dashdot"),
        ))
        annotations.append(dict(
            xref="paper", x=0.55, yref="y", y=weekly_max_pain,
            text=f"Max Pain Friday  ${weekly_max_pain:,.0f}",
            showarrow=False,
            font=dict(color="#bc8cff", size=11, family="monospace"),
            xanchor="left",
            yanchor="bottom",
            bgcolor="rgba(14,10,30,0.80)",
            borderpad=2,
        ))

    # ── Liquidation cluster lines — từ NOW → tương lai (không kẻ vào vùng nến) ─
    # Dùng xref="x" với x0 = thời điểm hiện tại, x1 = cuối trục (tương lai)
    # Khi giá chạm đến đường, đường vẫn còn (không bị xoá) vì nằm phía trước nến
    _long_clusters_to_draw  = []
    _short_clusters_to_draw = []

    if ai_liq_clusters and isinstance(ai_liq_clusters, dict) and "error" not in ai_liq_clusters:
        for c in ai_liq_clusters.get("long_liquidation_clusters", []):
            lvl = c.get("price_level")
            sz  = c.get("size_usd_millions", 0)
            lvl_str = c.get("strength", "medium")
            if lvl:
                _long_clusters_to_draw.append((float(lvl), float(sz), lvl_str))
        for c in ai_liq_clusters.get("short_liquidation_clusters", []):
            lvl = c.get("price_level")
            sz  = c.get("size_usd_millions", 0)
            lvl_str = c.get("strength", "medium")
            if lvl:
                _short_clusters_to_draw.append((float(lvl), float(sz), lvl_str))

    # Fallback: dùng long_cluster / short_cluster đơn lẻ nếu không có AI data
    if not _long_clusters_to_draw and long_cluster and long_cluster > 0:
        _long_clusters_to_draw = [(float(long_cluster), long_cluster_liq_usd / 1e6, "high")]
    if not _short_clusters_to_draw and short_cluster and short_cluster > 0:
        _short_clusters_to_draw = [(float(short_cluster), short_cluster_liq_usd / 1e6, "high")]

    # Tính x0 = thời điểm hiện tại (naive UTC, khớp với open_time của df)
    _now_x = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    if not df.empty:
        _last_candle_x = df["open_time"].iloc[-1]
        # x0 = max(now, last candle) để đường bắt đầu đúng tại điểm cuối nến
        _x0_liq = max(_now_x, _last_candle_x)
    else:
        _x0_liq = _now_x

    # x1 = cuối trục tương lai (7 ngày sau với 15m, 8 ngày sau với 1H)
    _future_days = 7 if timeframe == "15m" else 8
    _x1_liq = _x0_liq + pd.Timedelta(days=_future_days)

    # Strength → line width + dash style
    _str_width = {"low": 1.0, "medium": 1.5, "high": 2.2, "extreme": 3.0}
    _str_dash  = {"low": "dot", "medium": "dot", "high": "dashdot", "extreme": "solid"}

    for lvl, sz_m, strength in _long_clusters_to_draw:
        lw   = _str_width.get(strength, 1.5)
        dash = _str_dash.get(strength, "dot")
        shapes.append(dict(
            type="line",
            xref="x", x0=_x0_liq, x1=_x1_liq,
            yref="y", y0=lvl, y1=lvl,
            line=dict(color="#f85149", width=lw, dash=dash),
            opacity=0.85,
        ))
        sz_txt = f" · ${sz_m:.0f}M" if sz_m > 0 else ""
        annotations.append(dict(
            xref="paper", x=1.0, yref="y", y=lvl,
            text=f"  🔴 Long Liq ${lvl:,.0f}{sz_txt} [{strength.upper()}]",
            showarrow=False,
            font=dict(color="#f85149", size=9, family="monospace"),
            xanchor="left", bgcolor="rgba(14,17,23,0.75)", borderpad=2,
        ))

    for lvl, sz_m, strength in _short_clusters_to_draw:
        lw   = _str_width.get(strength, 1.5)
        dash = _str_dash.get(strength, "dot")
        shapes.append(dict(
            type="line",
            xref="x", x0=_x0_liq, x1=_x1_liq,
            yref="y", y0=lvl, y1=lvl,
            line=dict(color="#3fb950", width=lw, dash=dash),
            opacity=0.85,
        ))
        sz_txt = f" · ${sz_m:.0f}M" if sz_m > 0 else ""
        annotations.append(dict(
            xref="paper", x=1.0, yref="y", y=lvl,
            text=f"  🟢 Short Liq ${lvl:,.0f}{sz_txt} [{strength.upper()}]",
            showarrow=False,
            font=dict(color="#3fb950", size=9, family="monospace"),
            xanchor="left", bgcolor="rgba(14,17,23,0.75)", borderpad=2,
        ))

    # ── MM Scenario projections (Bezier spline curves) ────────────────────────
    is_15m = timeframe == "15m"

    # ── Pre-compute expiry horizons (needed for scenario clipping AND layout) ──
    # calendar already imported as _cal at top
    _today_lay = datetime.now(timezone.utc).replace(tzinfo=None)
    _dtf_lay   = (4 - _today_lay.weekday()) % 7
    if _dtf_lay == 0:
        _dtf_lay = 7
    _W_lay = float(max(_dtf_lay, 1))   # days to next Friday

    def _lf_days(yr, mo):
        ld = _monthrange(yr, mo)[1]
        d  = datetime(yr, mo, ld)
        while d.weekday() != 4:
            d -= timedelta(days=1)
        return max(1.0, float((d - _today_lay).days))

    _M_lay = _lf_days(_today_lay.year, _today_lay.month)
    if _M_lay <= _dtf_lay + 2:
        _nm = _today_lay.month + 1 if _today_lay.month < 12 else 1
        _ny = _today_lay.year + (1 if _today_lay.month == 12 else 0)
        _M_lay = _lf_days(_ny, _nm)
    _M_lay = max(_M_lay, _W_lay + 7)

    _Q_lay = _M_lay * 3  # safe fallback
    for _qm in [3, 6, 9, 12]:
        for _yo in range(0, 3):
            _qy = _today_lay.year + _yo
            _qd = _lf_days(_qy, _qm)
            if _qd > _dtf_lay + 20:
                _Q_lay = _qd
                break
        if _Q_lay < _M_lay * 3:
            break

    if is_15m:
        _max_D_day = _W_lay
    elif timeframe == "1H":
        _max_D_day = _W_lay
    elif timeframe == "4H":
        _max_D_day = _M_lay
    else:
        _max_D_day = _Q_lay

    # 15m mode: chỉ show vertical day separator lines (bỏ A/B/C/D scenarios)
    if scenarios and not df.empty:
        # ── Điểm neo CỐ ĐỊNH = thứ 6 gần nhất 15:05 giờ Việt Nam ──────────────
        _anchor_utc, _anchor_vn, _next_fri_vn, _ = get_weekly_anchor_vn()
        anchor_t = pd.Timestamp(_anchor_utc)

        last_t = df["open_time"].iloc[-1]
        if anchor_t > last_t:
            anchor_t = df["open_time"].iloc[0]

        # Vertical day separator — tính từ anchor cố định
        day_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        if is_15m:
            for d_offset in range(1, 8):
                day_t = anchor_t + pd.Timedelta(days=d_offset)
                day_name = day_names[day_t.dayofweek]
                is_friday = day_t.dayofweek == 4
                shapes.append(dict(
                    type="line", xref="x", x0=day_t, x1=day_t,
                    yref="paper", y0=0, y1=1,
                    line=dict(
                        color="#ffd700" if is_friday else "#30363d",
                        width=2 if is_friday else 1,
                        dash="dot",
                    ),
                ))
                annotations.append(dict(
                    x=day_t, xref="x",
                    y=1.02, yref="paper",
                    text=f"<b>{'📅 Expire ' if is_friday else ''}{day_name} {day_t.strftime('%d/%m')}</b>",
                    showarrow=False,
                    font=dict(
                        color="#ffd700" if is_friday else "#8b949e",
                        size=13 if is_friday else 11,
                        family="monospace",
                    ),
                    xanchor="center",
                ))

            # ── Annotation "Now" ──────────────────────────────────────────────
            _now_utc = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
            if anchor_t <= _now_utc <= anchor_t + pd.Timedelta(days=7):
                shapes.append(dict(
                    type="line", xref="x", x0=_now_utc, x1=_now_utc,
                    yref="paper", y0=0, y1=1,
                    line=dict(color="#f7931a", width=1.5, dash="dashdot"),
                ))
                annotations.append(dict(
                    x=_now_utc, xref="x",
                    y=1.02, yref="paper",
                    text="<b>▶ NOW</b>",
                    showarrow=False,
                    font=dict(color="#f7931a", size=11, family="monospace"),
                    xanchor="center",
                    bgcolor="rgba(14,17,23,0.85)",
                    borderpad=2,
                ))

        # ── Scenario D sweep annotation only (no colored lines) ──────────────
        if "D" in scenarios:
            scen_d = scenarios["D"]
            sweep_txt = scen_d.get("sweep", "")
            if sweep_txt and not df.empty:
                _anchor_utc2, _, _, _ = get_weekly_anchor_vn()
                _at2 = pd.Timestamp(_anchor_utc2)
                _sc_ref_ann = scen_d.get("short_cluster_ref", current_price * 1.07 if current_price else 0)
                # Find peak of D pts within W
                _W2 = _W_lay
                _pts_d = [(d, p) for d, p in scen_d.get("points", []) if d <= _W2]
                if _pts_d:
                    _peak_day, _peak_price = max(_pts_d, key=lambda x: x[1])
                    _peak_x = _at2 + pd.Timedelta(days=_peak_day)
                    annotations.append(dict(
                        x=_peak_x, y=_peak_price,
                        text=f"  ⚡ Fake Reject<br>  Quét liq ${_sc_ref_ann:,.0f}",
                        showarrow=True,
                        arrowhead=3, arrowcolor="#ffd700",
                        arrowwidth=2, ax=45, ay=-40,
                        font=dict(color="#ffd700", size=10, family="monospace"),
                        xanchor="left", yanchor="bottom",
                        bgcolor="rgba(30,25,0,0.90)",
                        bordercolor="#ffd700",
                        borderwidth=1,
                        borderpad=3,
                    ))
                # Sweep annotation at title area
                annotations.append(dict(
                    xref="paper", x=0.01, yref="paper", y=1.06,
                    text=f"<b style='color:#ffd700'>{sweep_txt}</b>",
                    showarrow=False,
                    font=dict(color="#ffd700", size=9, family="monospace"),
                    xanchor="left", bgcolor="rgba(14,17,23,0.85)", borderpad=2,
                ))

    # ── Reversal signal arrows ────────────────────────────────────────────────
    if not df.empty and current_price:
        _last_x       = df["open_time"].iloc[-1]
        net_short_pct = 100.0 - net_long_pct
        near_long  = (long_cluster  and
                      abs(current_price - long_cluster)  / current_price < 0.01)
        near_short = (short_cluster and
                      abs(current_price - short_cluster) / current_price < 0.01)
        if net_short_pct > 58 and near_long and current_fr < 0:
            annotations.append(dict(
                x=_last_x, y=current_price,
                text="⚠️ Reversal UP",
                showarrow=True,
                arrowhead=6, arrowcolor="#3fb950",
                arrowwidth=2.5, ax=0, ay=45,
                font=dict(color="#3fb950", size=12, family="monospace"),
                bgcolor="rgba(10,45,18,0.92)", borderpad=4,
            ))
        if net_long_pct > 58 and near_short and current_fr > 0:
            annotations.append(dict(
                x=_last_x, y=current_price,
                text="⚠️ Reversal DOWN",
                showarrow=True,
                arrowhead=6, arrowcolor="#f85149",
                arrowwidth=2.5, ax=0, ay=-45,
                font=dict(color="#f85149", size=12, family="monospace"),
                bgcolor="rgba(45,10,10,0.92)", borderpad=4,
            ))

    has_scenarios = bool(scenarios)
    is_15m_layout = timeframe == "15m"

    # x-axis range: candles window + projection extension (always show future for liq lines)
    xaxis_cfg = dict(
        gridcolor="#21262d", showgrid=True,
        rangeslider=dict(visible=False), color="#8b949e",
    )

    if not df.empty:
        _last_t_ax = df["open_time"].iloc[-1]
        _anchor_utc_ax, _, _, _ = get_weekly_anchor_vn()
        _anchor_t_ax = pd.Timestamp(_anchor_utc_ax)
        if _anchor_t_ax > _last_t_ax:
            _anchor_t_ax = df["open_time"].iloc[0]

        if is_15m_layout:
            xaxis_cfg["range"] = [
                str(_anchor_t_ax - pd.Timedelta(hours=2)),
                str(_anchor_t_ax + pd.Timedelta(days=7.5)),
            ]
        else:  # 1H
            xaxis_cfg["range"] = [
                str(df["open_time"].iloc[0]),
                str(_anchor_t_ax + pd.Timedelta(days=_W_lay + 1.0)),
            ]

    # ── Options expiry vertical markers (non-15m) ─────────────────────────────
    if not is_15m_layout and not df.empty:
        _anchor_utc_ex, _, _, _ = get_weekly_anchor_vn()
        _anchor_t_ex = pd.Timestamp(_anchor_utc_ex)
        last_t_ex = df["open_time"].iloc[-1]
        expiry_marks = []

        if timeframe == "1H":
            fri_t = _anchor_t_ex + pd.Timedelta(days=7)   # thứ 6 kế tiếp từ anchor VN
            expiry_marks.append((fri_t, "#bc8cff", "📅 Weekly Expiry", weekly_max_pain))

        elif timeframe == "4H":
            fri_t   = _anchor_t_ex + pd.Timedelta(days=7)
            month_t = last_t_ex + pd.Timedelta(days=_M_lay)
            expiry_marks.append((fri_t,   "#bc8cff", "📅 Weekly",  weekly_max_pain))
            expiry_marks.append((month_t, "#58a6ff", "🗓 Monthly", None))

        else:  # 1D
            fri_t   = _anchor_t_ex + pd.Timedelta(days=7)
            month_t = last_t_ex + pd.Timedelta(days=_M_lay)
            qtr_t   = last_t_ex + pd.Timedelta(days=_Q_lay)
            expiry_marks.append((fri_t,   "#bc8cff", "📅 Weekly",   weekly_max_pain))
            expiry_marks.append((month_t, "#58a6ff", "🗓 Monthly",  None))
            expiry_marks.append((qtr_t,   "#ffd700", "📊 Quarterly", None))

        for exp_t, exp_color, exp_label, exp_mp in expiry_marks:
            shapes.append(dict(
                type="line", xref="x", x0=exp_t, x1=exp_t,
                yref="paper", y0=0, y1=1,
                line=dict(color=exp_color, width=1.5, dash="dot"),
            ))
            ann_text = f"  {exp_label}"
            if exp_mp:
                ann_text += f"<br>  MP ${exp_mp:,.0f}"
            annotations.append(dict(
                x=exp_t, xref="x",
                y=1.02, yref="paper",
                text=f"<b>{ann_text}</b>",
                showarrow=False,
                font=dict(color=exp_color, size=11, family="monospace"),
                xanchor="center",
                bgcolor="rgba(14,17,23,0.85)",
                borderpad=3,
            ))

    # ── y-axis: clip to visible scenario range (prevent wild autorange) ───────
    yaxis_dict = dict(
        gridcolor="#21262d", showgrid=True,
        color="#8b949e", tickprefix="$",
    )

    if current_price and not df.empty:
        _all_y: list[float] = [float(current_price)]
        # Collect candle prices
        for col in ["high", "low", "close", "open"]:
            if col in df.columns:
                _all_y.extend(df[col].astype(float).tolist())
        # Collect scenario prices within the timeframe horizon
        for key, scen in (scenarios or {}).items():
            for d_pt, p_pt in scen.get("points", []):
                if d_pt <= _max_D_day:
                    _all_y.append(float(p_pt))
        # Include cluster + max pain levels
        for lvl in [long_cluster, short_cluster, max_pain, weekly_max_pain, monthly_max_pain]:
            if lvl and float(lvl) > 0:
                _all_y.append(float(lvl))
        # Include AI liq cluster levels
        if ai_liq_clusters and isinstance(ai_liq_clusters, dict):
            for c in ai_liq_clusters.get("long_liquidation_clusters", []):
                if c.get("price_level"):
                    _all_y.append(float(c["price_level"]))
            for c in ai_liq_clusters.get("short_liquidation_clusters", []):
                if c.get("price_level"):
                    _all_y.append(float(c["price_level"]))

        _y_min_raw = min(_all_y)
        _y_max_raw = max(_all_y)
        _cp = float(current_price)
        # Clip: never more than ±22% from current price
        _y_min = max(_y_min_raw * 0.987, _cp * 0.78)
        _y_max = min(_y_max_raw * 1.013, _cp * 1.22)
        # Ensure current price is always visible
        _y_min = min(_y_min, _cp * 0.985)
        _y_max = max(_y_max, _cp * 1.015)
        yaxis_dict["range"] = [_y_min, _y_max]
    else:
        yaxis_dict["autorange"] = True

    # chart title
    chart_title = title_text
    if is_15m_layout:
        chart_title += "  ·  Weekly Entry Plan (15m view)"
    elif timeframe == "1H":
        chart_title += "  ·  Weekly View  ·  next expiry Friday"

    fig.update_layout(
        title=dict(text=chart_title, font=dict(color="#e6edf3", size=14)),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        xaxis=xaxis_cfg,
        yaxis=yaxis_dict,
        margin=dict(l=10, r=200, t=54, b=10),
        height=780 if is_15m_layout else 640,
        # uirevision: giữ nguyên zoom/pan khi Streamlit re-render Python
        # Chỉ reset khi timeframe thay đổi
        uirevision=f"chart-{timeframe}",
        showlegend=False,
        shapes=shapes,
        annotations=annotations,
    )
    return fig


def make_oi_chart(df):
    fig = go.Figure(go.Scatter(
        x=df["timestamp"], y=df["sumOpenInterestValue"],
        mode="lines", fill="tozeroy",
        line=dict(color="#f7931a", width=2),
        fillcolor="rgba(247,147,26,0.12)",
    ))
    fig.update_layout(
        title=dict(text="Open Interest (USDT)", font=dict(color="#e6edf3", size=14)),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        xaxis=dict(gridcolor="#21262d", color="#8b949e"),
        yaxis=dict(gridcolor="#21262d", color="#8b949e", tickformat=".2s"),
        margin=dict(l=10, r=10, t=40, b=10), height=280,
        showlegend=False,
    )
    return fig


def make_funding_chart(df):
    colors = ["#3fb950" if v >= 0 else "#f85149" for v in df["fundingRate"]]
    fig = go.Figure(go.Bar(
        x=df["fundingTime"], y=df["fundingRate"],
        marker_color=colors,
    ))
    fig.add_hline(y=0, line_dash="dot", line_color="#30363d")
    fig.update_layout(
        title=dict(text="Funding Rate History (%)", font=dict(color="#e6edf3", size=14)),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        xaxis=dict(gridcolor="#21262d", color="#8b949e"),
        yaxis=dict(gridcolor="#21262d", color="#8b949e", ticksuffix="%"),
        margin=dict(l=10, r=10, t=40, b=10), height=280,
        showlegend=False,
    )
    return fig


def calculate_counterflow_ls(price_df: "pd.DataFrame", oi_df: "pd.DataFrame") -> "pd.DataFrame":
    """
    CounterFlow Net Long / Net Short — thuật toán ΔP × ΔOI.

    Nguyên liệu:
      price_df : cột "ts" + "price"  — giá đóng BTC futures 1h
      oi_df    : cột "ts" + "oi"     — sumOpenInterest tính bằng BTC (từ fetch_oi_1h)

    Bước 1 — ΔP tuyệt đối (USD), ΔΟΙBTC tuyệt đối (BTC)
    Bước 2 — usd_mag = |ΔΟΙBTC| × Price[i]   → giá trị USD thực của dòng tiền dịch
    Bước 3 — Phân loại 4 trường hợp:
      ΔP>0, ΔOI>0  → long_flow  = +usd_mag   (tiền mới mở LONG)
      ΔP<0, ΔOI<0  → long_flow  = -usd_mag   (LONG đang đóng)
      ΔP<0, ΔOI>0  → short_flow = +usd_mag   (tiền mới mở SHORT)
      ΔP>0, ΔOI<0  → short_flow = -usd_mag   (SHORT bị squeeze)
    Bước 4 — Tích lũy cumsum
    Bước 5 — Chuẩn hóa theo avg_OI_USD = mean(oi_btc × price) → ra đơn vị %
    Bước 6 — Spread = Net_Long_Pct - Net_Short_Pct
              Ngưỡng: >10% = đảo chiều, 5-10% = FOMO, <0% = phe đó bị diệt
    """
    if price_df is None or price_df.empty or oi_df is None or oi_df.empty:
        return pd.DataFrame()

    try:
        # price_df: ts + price   (fetch_futures_klines_1h)
        # oi_df   : ts + oi      (fetch_oi_1h → sumOpenInterest BTC)
        p = price_df[["ts", "price"]].copy()
        o = oi_df[["ts", "oi"]].copy()
        p["ts"] = pd.to_datetime(p["ts"])
        o["ts"] = pd.to_datetime(o["ts"])

        merged = pd.merge_asof(
            p.sort_values("ts"), o.sort_values("ts"),
            on="ts", direction="nearest", tolerance=pd.Timedelta("30min"),
        )
        merged = merged.dropna(subset=["price", "oi"]).reset_index(drop=True)
        if len(merged) < 4:
            return pd.DataFrame()

        prices = merged["price"].astype(float).values   # USD
        ois    = merged["oi"].astype(float).values       # BTC

        # Bước 5 normalizer — avg OI in USD across full window
        avg_oi_usd = float(np.mean(ois * prices))
        if avg_oi_usd <= 0:
            avg_oi_usd = 1.0

        long_flow_usd  = [0.0]
        short_flow_usd = [0.0]
        for i in range(1, len(prices)):
            dP      = prices[i] - prices[i - 1]          # tuyệt đối USD
            dOI_btc = ois[i]   - ois[i - 1]              # tuyệt đối BTC
            usd_mag = abs(dOI_btc) * prices[i]           # USD value của dịch chuyển OI

            # Long flow
            if   dP > 0 and dOI_btc > 0:
                long_flow_usd.append(+usd_mag)
                short_flow_usd.append(0.0)
            elif dP < 0 and dOI_btc < 0:
                long_flow_usd.append(-usd_mag)
                short_flow_usd.append(0.0)
            # Short flow
            elif dP < 0 and dOI_btc > 0:
                long_flow_usd.append(0.0)
                short_flow_usd.append(+usd_mag)
            elif dP > 0 and dOI_btc < 0:
                long_flow_usd.append(0.0)
                short_flow_usd.append(-usd_mag)
            else:
                long_flow_usd.append(0.0)
                short_flow_usd.append(0.0)

        lf = np.array(long_flow_usd)
        sf = np.array(short_flow_usd)

        # Tích lũy, reset gốc = 0
        nl_cum = np.cumsum(lf)
        ns_cum = np.cumsum(sf)

        # Chuẩn hóa về % tổng OI
        net_long_pct_arr  = nl_cum / avg_oi_usd * 100.0
        net_short_pct_arr = ns_cum / avg_oi_usd * 100.0
        spread_arr        = net_long_pct_arr - net_short_pct_arr

        result = pd.DataFrame({
            "ts":           merged["ts"].values,
            "price":        prices,
            "net_long":     net_long_pct_arr,    # % of avg OI
            "net_short":    net_short_pct_arr,   # % of avg OI
            "spread":       spread_arr,           # %
            "long_flow":    lf,
            "short_flow":   sf,
        })
        return result
    except Exception:
        return pd.DataFrame()


def _detect_cf_divergence(
    prices: "np.ndarray",
    nl: "np.ndarray",
    ns: "np.ndarray",
    window: int = 6,
):
    """Phát hiện bearish / bullish divergence giữa giá và counterflow."""
    bearish: list[int] = []
    bullish: list[int] = []
    for i in range(window, len(prices)):
        p_win  = prices[i - window : i]
        nl_win = nl[i - window : i]
        ns_win = ns[i - window : i]
        # Bearish divergence: giá đỉnh mới nhưng Net Long thấp hơn + trong FOMO zone
        if (prices[i] > float(np.max(p_win))
                and nl[i] < float(np.max(nl_win))
                and nl[i] > 3.0):
            bearish.append(i)
        # Bullish divergence: giá đáy mới nhưng Net Short thấp hơn + trong FOMO zone
        if (prices[i] < float(np.min(p_win))
                and ns[i] < float(np.max(ns_win))
                and ns[i] > 3.0):
            bullish.append(i)
    return bearish, bullish


def _get_cf_zone(nl_val: float, ns_val: float) -> str:
    if nl_val > 10:  return "LONG_EXTREME"
    if nl_val > 5:   return "LONG_FOMO"
    if ns_val > 10:  return "SHORT_EXTREME"
    if ns_val > 5:   return "SHORT_FOMO"
    return "NEUTRAL"


_ZONE_FILL = {
    "LONG_EXTREME": "rgba(248,81,73,0.18)",
    "LONG_FOMO":    "rgba(248,81,73,0.07)",
    "SHORT_EXTREME":"rgba(63,185,80,0.18)",
    "SHORT_FOMO":   "rgba(63,185,80,0.07)",
}


def make_ls_panel(
    cf_df: "pd.DataFrame",
    fut_klines_df: "pd.DataFrame | None" = None,
    long_cluster: float = 0.0,
    short_cluster: float = 0.0,
) -> "Optional[go.Figure]":
    """
    CounterFlow 2-panel combined chart (shared X-axis):
      Panel 1 — Price Candlestick + background zone shading + liq bubbles + divergence arrows
      Panel 2 — Net Long/Short % + Spread fill + threshold lines + crossover arrows
    """
    if cf_df is None or cf_df.empty:
        return None

    nl      = cf_df["net_long"].values.astype(float)
    ns      = cf_df["net_short"].values.astype(float)
    sp      = cf_df["spread"].values.astype(float)
    prices  = cf_df["price"].values.astype(float)
    ts      = pd.to_datetime(cf_df["ts"])
    lf      = cf_df["long_flow"].values.astype(float)
    sf      = cf_df["short_flow"].values.astype(float)

    latest_nl = float(nl[-1]);  latest_ns = float(ns[-1]);  latest_sp = float(sp[-1])

    # ── Tín hiệu title ────────────────────────────────────────────────────
    if abs(latest_sp) > 10:
        signal_label = "⚠ ĐẢO CHIỀU — quá FOMO";  dom_color = "#ffd700"
    elif latest_sp > 5:
        signal_label = "LONG FOMO >5%";  dom_color = "#f85149"
    elif latest_sp < -5:
        signal_label = "SHORT FOMO <-5%";  dom_color = "#3fb950"
    elif latest_sp > 0:
        signal_label = "Long Dominant";  dom_color = "#3fb950"
    else:
        signal_label = "Short Dominant";  dom_color = "#f85149"

    # ── Auto-signals per bar ──────────────────────────────────────────────
    sig_sell_idx: list[int] = []
    sig_buy_idx:  list[int] = []
    sig_ext_idx:  list[int] = []
    for i in range(1, len(ts)):
        sp_i = sp[i]; sp_prev = sp[i - 1]; p_i = prices[i]
        if (nl[i] > 5 and short_cluster > 0
                and p_i >= short_cluster * 0.995
                and sp_i > 0 and sp_i < sp_prev):
            sig_sell_idx.append(i)
        elif (ns[i] > 5 and long_cluster > 0
                and p_i <= long_cluster * 1.005
                and sp_i < 0 and sp_i > sp_prev):
            sig_buy_idx.append(i)
        if abs(sp_i) > 10:
            sig_ext_idx.append(i)

    # ── Divergence ────────────────────────────────────────────────────────
    bearish_div, bullish_div = _detect_cf_divergence(prices, nl, ns)

    # ── Crossovers (spread đổi dấu) ───────────────────────────────────────
    crossovers = [i for i in range(1, len(sp)) if sp[i - 1] * sp[i] < 0]

    # ── Build subplots ────────────────────────────────────────────────────
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.45],
        vertical_spacing=0.03,
        subplot_titles=["Price · Background = FOMO Zone · Bubbles = Liquidation Events",
                        "CounterFlow Net Long/Short %"],
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PANEL 1 — Price Candlestick
    # ═══════════════════════════════════════════════════════════════════════
    has_ohlcv = (fut_klines_df is not None
                 and not fut_klines_df.empty
                 and "open" in fut_klines_df.columns
                 and "high" in fut_klines_df.columns
                 and "low"  in fut_klines_df.columns)

    if has_ohlcv:
        ohlcv = fut_klines_df.copy()
        ohlcv["ts"] = pd.to_datetime(ohlcv["ts"])
        fig.add_trace(go.Candlestick(
            x=ohlcv["ts"],
            open=ohlcv["open"], high=ohlcv["high"],
            low=ohlcv["low"],  close=ohlcv["price"],
            increasing_line_color="#3fb950", decreasing_line_color="#f85149",
            name="BTC/USDT",
            showlegend=True,
        ), row=1, col=1)
    else:
        # fallback: đường giá
        fig.add_trace(go.Scatter(
            x=ts, y=prices, mode="lines",
            name="Price", line=dict(color="#f7931a", width=1.5),
        ), row=1, col=1)

    # ── Background zone shading (vrect per contiguous zone group) ──────────
    from itertools import groupby as _grpby
    zones = [_get_cf_zone(float(nl[i]), float(ns[i])) for i in range(len(ts))]
    ts_list = list(ts)
    # group consecutive identical zones
    idx = 0
    for zone, grp in _grpby(range(len(zones)), key=lambda i: zones[i]):
        grp_list = list(grp)
        if zone == "NEUTRAL":
            idx += len(grp_list)
            continue
        t0 = ts_list[grp_list[0]]
        t1 = ts_list[grp_list[-1]]
        fig.add_vrect(
            x0=t0, x1=t1,
            fillcolor=_ZONE_FILL[zone],
            layer="below", line_width=0,
            row=1, col=1,   # type: ignore[call-arg]
        )
        idx += len(grp_list)

    # ── Liquidation bubbles from flow data ─────────────────────────────────
    # Long liq: long_flow < 0 (dP<0, dOI<0) → red bubbles
    ll_mask = lf < 0
    if ll_mask.any():
        ll_mag = np.abs(lf[ll_mask])
        ll_sz  = np.clip(ll_mag / max(float(ll_mag.max()), 1) * 22 + 5, 5, 28)
        fig.add_trace(go.Scatter(
            x=ts[ll_mask], y=prices[ll_mask],
            mode="markers",
            name="Long Liq",
            marker=dict(color="rgba(248,81,73,0.70)", size=ll_sz,
                        line=dict(color="#f85149", width=1)),
            hovertemplate="Long Liq<br>$%{y:,.0f}<extra></extra>",
        ), row=1, col=1)

    # Short liq: short_flow < 0 (dP>0, dOI<0) → blue bubbles
    sl_mask = sf < 0
    if sl_mask.any():
        sl_mag = np.abs(sf[sl_mask])
        sl_sz  = np.clip(sl_mag / max(float(sl_mag.max()), 1) * 22 + 5, 5, 28)
        fig.add_trace(go.Scatter(
            x=ts[sl_mask], y=prices[sl_mask],
            mode="markers",
            name="Short Liq",
            marker=dict(color="rgba(56,139,253,0.70)", size=sl_sz,
                        line=dict(color="#388bfd", width=1)),
            hovertemplate="Short Liq<br>$%{y:,.0f}<extra></extra>",
        ), row=1, col=1)

    # ── Divergence arrows ──────────────────────────────────────────────────
    if bearish_div:
        bd_idx = bearish_div[-8:]   # tối đa 8 arrows gần nhất
        fig.add_trace(go.Scatter(
            x=ts.iloc[bd_idx], y=prices[bd_idx],
            mode="markers+text",
            name="Bearish Div",
            marker=dict(symbol="triangle-down", color="#f85149", size=12),
            text=["↘"] * len(bd_idx),
            textposition="top center",
            textfont=dict(color="#f85149", size=10),
            hovertemplate="Bearish Div $%{y:,.0f}<extra></extra>",
        ), row=1, col=1)
    if bullish_div:
        bd_idx2 = bullish_div[-8:]
        fig.add_trace(go.Scatter(
            x=ts.iloc[bd_idx2], y=prices[bd_idx2],
            mode="markers+text",
            name="Bullish Div",
            marker=dict(symbol="triangle-up", color="#3fb950", size=12),
            text=["↗"] * len(bd_idx2),
            textposition="bottom center",
            textfont=dict(color="#3fb950", size=10),
            hovertemplate="Bullish Div $%{y:,.0f}<extra></extra>",
        ), row=1, col=1)

    # ── Auto-signal markers on price panel ────────────────────────────────
    if sig_sell_idx:
        fig.add_trace(go.Scatter(
            x=ts.iloc[sig_sell_idx], y=prices[sig_sell_idx],
            mode="markers+text",
            name="SELL Alert",
            marker=dict(symbol="x", color="#ff6b6b", size=14),
            text=["SELL"] * len(sig_sell_idx),
            textposition="top center",
            textfont=dict(color="#ff6b6b", size=9),
            hovertemplate="SELL_ALERT $%{y:,.0f}<extra></extra>",
        ), row=1, col=1)
    if sig_buy_idx:
        fig.add_trace(go.Scatter(
            x=ts.iloc[sig_buy_idx], y=prices[sig_buy_idx],
            mode="markers+text",
            name="BUY Alert",
            marker=dict(symbol="x", color="#79c0ff", size=14),
            text=["BUY"] * len(sig_buy_idx),
            textposition="bottom center",
            textfont=dict(color="#79c0ff", size=9),
            hovertemplate="BUY_ALERT $%{y:,.0f}<extra></extra>",
        ), row=1, col=1)
    if sig_ext_idx:
        # Chỉ show markers tại điểm EXTREME REVERSAL đầu tiên của mỗi cụm
        _prev = -99
        for si in sig_ext_idx:
            if si - _prev > 3:   # tránh spam quá nhiều
                fig.add_annotation(
                    x=ts.iloc[si], y=float(prices[si]),
                    text="⚠", showarrow=False,
                    font=dict(color="#ffd700", size=16),
                    xref="x", yref="y",
                    row=1, col=1,
                )
                _prev = si

    # ═══════════════════════════════════════════════════════════════════════
    # PANEL 2 — Net Long/Short % + Spread
    # ═══════════════════════════════════════════════════════════════════════
    sp_max = max(float(np.max(np.abs(sp))) * 1.1, 12.0)

    # Zone fills on LS panel
    fig.add_hrect(y0=10, y1=sp_max,
                  fillcolor="rgba(248,81,73,0.07)", line_width=0, layer="below",
                  row=2, col=1)   # type: ignore[call-arg]
    fig.add_hrect(y0=5, y1=10,
                  fillcolor="rgba(248,81,73,0.04)", line_width=0, layer="below",
                  row=2, col=1)   # type: ignore[call-arg]
    fig.add_hrect(y0=-10, y1=-5,
                  fillcolor="rgba(63,185,80,0.04)", line_width=0, layer="below",
                  row=2, col=1)   # type: ignore[call-arg]
    fig.add_hrect(y0=-sp_max, y1=-10,
                  fillcolor="rgba(63,185,80,0.07)", line_width=0, layer="below",
                  row=2, col=1)   # type: ignore[call-arg]

    # Spread fill
    fig.add_trace(go.Scatter(
        x=ts, y=sp,
        mode="lines",
        name=f"Spread {latest_sp:+.2f}%",
        line=dict(color=dom_color, width=1.5),
        fill="tozeroy",
        fillcolor="rgba(63,185,80,0.13)" if latest_sp > 0 else "rgba(248,81,73,0.13)",
        hovertemplate="Spread: %{y:.2f}%<extra></extra>",
    ), row=2, col=1)

    # Net Long %
    fig.add_trace(go.Scatter(
        x=ts, y=nl,
        mode="lines",
        name=f"Net Long {latest_nl:+.2f}%",
        line=dict(color="#ffd700", width=2),
        hovertemplate="Net Long: %{y:.2f}%<extra></extra>",
    ), row=2, col=1)

    # Net Short %
    fig.add_trace(go.Scatter(
        x=ts, y=ns,
        mode="lines",
        name=f"Net Short {latest_ns:+.2f}%",
        line=dict(color="#388bfd", width=2),
        hovertemplate="Net Short: %{y:.2f}%<extra></extra>",
    ), row=2, col=1)

    # Threshold lines
    for yv, col_th, lbl in [
        ( 10, "#f85149", "+10%"), ( 5, "#f85149", "+5%"),
        (  0, "#484f58", "0"),
        ( -5, "#3fb950", "-5%"), (-10, "#3fb950", "-10%"),
    ]:
        fig.add_hline(y=yv, line_color=col_th, line_dash="dot", line_width=1.0,
                      annotation_text=lbl, annotation_position="right",
                      annotation_font=dict(color=col_th, size=8),
                      row=2, col=1)

    # Crossover arrows ↑↓ on LS panel
    for ci in crossovers[-10:]:
        arrow_sym = "triangle-up" if sp[ci] > 0 else "triangle-down"
        arrow_col = "#3fb950"     if sp[ci] > 0 else "#f85149"
        fig.add_trace(go.Scatter(
            x=[ts.iloc[ci]], y=[float(sp[ci])],
            mode="markers",
            showlegend=False,
            marker=dict(symbol=arrow_sym, color=arrow_col, size=10),
            hovertemplate="Crossover %{y:.2f}%<extra></extra>",
        ), row=2, col=1)

    # ── Layout ────────────────────────────────────────────────────────────
    fig.update_layout(
        title=dict(
            text=(
                f"CounterFlow Net Long/Short · USD Flow/avgOI · "
                f"<span style='color:{dom_color}'>{signal_label}</span> · "
                f"Spread {latest_sp:+.2f}%"
            ),
            font=dict(color="#e6edf3", size=14),
        ),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        hovermode="x unified",
        xaxis=dict(gridcolor="#21262d", color="#8b949e", showticklabels=False),
        yaxis=dict(gridcolor="#21262d", color="#8b949e",
                   tickprefix="$", tickformat=",.0f"),
        xaxis2=dict(gridcolor="#21262d", color="#8b949e"),
        yaxis2=dict(gridcolor="#21262d", color="#8b949e", ticksuffix="%"),
        legend=dict(bgcolor="rgba(22,27,34,0.90)", bordercolor="#30363d",
                    borderwidth=1, font=dict(color="#e6edf3", size=10),
                    orientation="h", yanchor="top", y=-0.06,
                    traceorder="normal"),
        margin=dict(l=10, r=10, t=50, b=10),
        height=560,
        xaxis_rangeslider_visible=False,
    )
    return fig


# ---------------------------------------------------------------------------
# Chart builders — Max Pain
# ---------------------------------------------------------------------------

def make_max_pain_chart(opts_df, max_pain, current_price, window=0.30):
    """
    OI distribution chart: call OI (green) + put OI (red) by strike,
    zoomed to ±window% around current price, with max pain + current price lines.
    """
    if opts_df.empty or max_pain is None:
        return None

    lo = current_price * (1 - window)
    hi = current_price * (1 + window)
    sub = opts_df[(opts_df["strike"] >= lo) & (opts_df["strike"] <= hi)]
    if sub.empty:
        return None

    calls = sub[sub["type"] == "C"].groupby("strike")["oi"].sum().reset_index()
    calls.columns = ["strike", "oi"]
    puts  = sub[sub["type"] == "P"].groupby("strike")["oi"].sum().reset_index()
    puts.columns = ["strike", "oi"]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=calls["strike"], y=calls["oi"],
        name="Call OI", marker_color="rgba(63,185,80,0.75)",
        hovertemplate="Strike $%{x:,.0f}<br>Call OI: %{y:,.2f} BTC<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=puts["strike"], y=puts["oi"],
        name="Put OI", marker_color="rgba(248,81,73,0.75)",
        hovertemplate="Strike $%{x:,.0f}<br>Put OI: %{y:,.2f} BTC<extra></extra>",
    ))

    # Max pain vertical line
    fig.add_vline(
        x=max_pain, line_color="#a371f7", line_width=2, line_dash="dash",
        annotation_text=f"Max Pain ${max_pain:,.0f}",
        annotation_position="top",
        annotation_font=dict(color="#a371f7", size=11),
    )
    # Current price vertical line
    fig.add_vline(
        x=current_price, line_color="#f7931a", line_width=1.5, line_dash="dot",
        annotation_text=f"Current ${current_price:,.0f}",
        annotation_position="top right",
        annotation_font=dict(color="#f7931a", size=11),
    )

    fig.update_layout(
        title=dict(text="BTC Options OI by Strike (Deribit) · ±30% range",
                   font=dict(color="#e6edf3", size=14)),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        barmode="group",
        bargap=0.1,
        xaxis=dict(gridcolor="#21262d", color="#8b949e",
                   tickprefix="$", title="Strike Price"),
        yaxis=dict(gridcolor="#21262d", color="#8b949e",
                   title="Open Interest (BTC)"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d",
                    borderwidth=1, font=dict(color="#e6edf3")),
        margin=dict(l=10, r=10, t=60, b=10), height=340,
    )
    return fig

# ---------------------------------------------------------------------------
# Chart builders — liquidations
# ---------------------------------------------------------------------------

PRICE_BUCKET = 200   # $ per price band

def make_liq_heatmap(df):
    df = df.copy()
    df["price_bucket"] = (df["price"] // PRICE_BUCKET) * PRICE_BUCKET
    df["minute"] = df["time"].dt.floor("1min")
    df["label"] = df["side"].map({"SELL": "Long Liq", "BUY": "Short Liq"})
    df["color"] = df["side"].map({"SELL": "#f85149", "BUY": "#388bfd"})

    longs = df[df["side"] == "SELL"]
    shorts = df[df["side"] == "BUY"]

    fig = go.Figure()

    for subset, name, color in [
        (longs, "Long Liq (red)", "#f85149"),
        (shorts, "Short Liq (blue)", "#388bfd"),
    ]:
        if subset.empty:
            continue
        size_scaled = (subset["usd_value"] / subset["usd_value"].max() * 40 + 6).clip(6, 46)
        hover = [
            f"${v:,.0f} | {p:,.0f} | {t.strftime('%H:%M:%S')}"
            for v, p, t in zip(subset["usd_value"], subset["price"], subset["time"])
        ]
        fig.add_trace(go.Scatter(
            x=subset["time"],
            y=subset["price"],
            mode="markers",
            name=name,
            marker=dict(
                size=size_scaled,
                color=color,
                opacity=0.7,
                line=dict(width=0),
            ),
            hovertext=hover,
            hoverinfo="text",
        ))

    fig.update_layout(
        title=dict(text="Liquidation Heatmap  ·  bubble size = USD value",
                   font=dict(color="#e6edf3", size=14)),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        xaxis=dict(gridcolor="#21262d", color="#8b949e", title="Time"),
        yaxis=dict(gridcolor="#21262d", color="#8b949e",
                   tickprefix="$", title="Price"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d",
                    borderwidth=1, font=dict(color="#e6edf3")),
        margin=dict(l=10, r=10, t=50, b=10), height=360,
    )
    return fig


def make_liq_volume_bar(df):
    df = df.copy()
    df["minute"] = df["time"].dt.floor("1min")

    long_liq = (df[df["side"] == "SELL"]
                .groupby("minute")["usd_value"].sum()
                .rename("long_liq"))
    short_liq = (df[df["side"] == "BUY"]
                 .groupby("minute")["usd_value"].sum()
                 .rename("short_liq"))

    vol = pd.concat([long_liq, short_liq], axis=1).fillna(0).reset_index()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=vol["minute"], y=vol["long_liq"],
        name="Long Liqs", marker_color="#f85149",
    ))
    fig.add_trace(go.Bar(
        x=vol["minute"], y=vol["short_liq"],
        name="Short Liqs", marker_color="#388bfd",
    ))
    fig.update_layout(
        title=dict(text="Liquidation Volume by Minute (USDT)",
                   font=dict(color="#e6edf3", size=14)),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"),
        barmode="stack",
        xaxis=dict(gridcolor="#21262d", color="#8b949e"),
        yaxis=dict(gridcolor="#21262d", color="#8b949e", tickformat=".2s"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d",
                    borderwidth=1, font=dict(color="#e6edf3")),
        margin=dict(l=10, r=10, t=50, b=10), height=280,
    )
    return fig

# ---------------------------------------------------------------------------
# Chart builders — liquidation clusters
# ---------------------------------------------------------------------------

def make_liq_cluster_chart(cluster_df, current_price, ai_analysis: dict = None):
    """
    Bidirectional horizontal bar chart.
    Left (negative X) = long liquidations  → red
    Right (positive X) = short liquidations → green
    ai_analysis: optional dict from Liq Image AI tab — overlays detected clusters.
    """
    if cluster_df.empty and (ai_analysis is None or "error" in (ai_analysis or {})):
        return None

    fig = go.Figure()
    x_range = 500  # default, updated below

    if not cluster_df.empty:
        df = cluster_df.copy()
        df = df.sort_values("price_level").reset_index(drop=True)
        df["long_m"]  = df["long_liq_usd"]  / 1e6
        df["short_m"] = df["short_liq_usd"] / 1e6
        below = df[df["price_level"] <= current_price]
        above = df[df["price_level"] >  current_price]

        if not below.empty:
            fig.add_trace(go.Bar(
                y=below["price_level"], x=-below["long_m"], orientation="h",
                name="Long Liqs (OI-based)",
                marker=dict(color="rgba(248,81,73,0.75)", line=dict(width=0)),
                hovertemplate="<b>$%{y:,.0f}</b><br>Est. long liqs: $%{customdata:.2f}M<extra></extra>",
                customdata=below["long_m"],
            ))
        if not above.empty:
            fig.add_trace(go.Bar(
                y=above["price_level"], x=above["short_m"], orientation="h",
                name="Short Liqs (OI-based)",
                marker=dict(color="rgba(63,185,80,0.75)", line=dict(width=0)),
                hovertemplate="<b>$%{y:,.0f}</b><br>Est. short liqs: $%{x:.2f}M<extra></extra>",
            ))
        max_val = max(df["long_m"].max() if not df.empty else 0,
                      df["short_m"].max() if not df.empty else 0)
        x_range = max_val * 1.15

    # ── AI overlay ─────────────────────────────────────────────────────────
    ai_title_suffix = ""
    if ai_analysis and isinstance(ai_analysis, dict) and "error" not in ai_analysis:
        _sz  = {"low": 9, "medium": 18, "high": 30, "extreme": 44}
        _sym = {"low": "circle", "medium": "diamond", "high": "star", "extreme": "star-triangle-up"}

        long_cl  = ai_analysis.get("long_liquidation_clusters",  [])
        short_cl = ai_analysis.get("short_liquidation_clusters", [])

        if long_cl:
            lp   = [c.get("price_level", 0) for c in long_cl]
            lx   = [-(c.get("size_usd_millions", 0)) for c in long_cl]
            lsz  = [_sz.get(c.get("strength", "low"), 9) for c in long_cl]
            lsym = [_sym.get(c.get("strength", "low"), "circle") for c in long_cl]
            ltxt = [f"AI Long ${c.get('price_level',0):,.0f} · {c.get('size_usd_millions',0):.0f}M · {c.get('strength','').upper()}"
                    for c in long_cl]
            fig.add_trace(go.Scatter(
                x=lx, y=lp, mode="markers",
                name="🤖 AI Long Clusters",
                marker=dict(color="#ff6b6b", size=lsz, symbol=lsym,
                            line=dict(color="#fff", width=1.5), opacity=0.95),
                hovertext=ltxt, hoverinfo="text",
            ))

        if short_cl:
            sp   = [c.get("price_level", 0) for c in short_cl]
            sx   = [c.get("size_usd_millions", 0) for c in short_cl]
            ssz  = [_sz.get(c.get("strength", "low"), 9) for c in short_cl]
            ssym = [_sym.get(c.get("strength", "low"), "circle") for c in short_cl]
            stxt = [f"AI Short ${c.get('price_level',0):,.0f} · {c.get('size_usd_millions',0):.0f}M · {c.get('strength','').upper()}"
                    for c in short_cl]
            fig.add_trace(go.Scatter(
                x=sx, y=sp, mode="markers",
                name="🤖 AI Short Clusters",
                marker=dict(color="#00e5a0", size=ssz, symbol=ssym,
                            line=dict(color="#fff", width=1.5), opacity=0.95),
                hovertext=stxt, hoverinfo="text",
            ))

        for lvl in ai_analysis.get("key_support_levels", [])[:4]:
            fig.add_hline(y=lvl, line_color="#f87171", line_width=1, line_dash="longdash",
                          annotation_text=f"  AI Sup ${lvl:,.0f}",
                          annotation_font=dict(color="#f87171", size=9))
        for lvl in ai_analysis.get("key_resistance_levels", [])[:4]:
            fig.add_hline(y=lvl, line_color="#4ade80", line_width=1, line_dash="longdash",
                          annotation_text=f"  AI Res ${lvl:,.0f}",
                          annotation_font=dict(color="#4ade80", size=9))

        all_ai = [abs(c.get("size_usd_millions", 0)) for c in long_cl + short_cl]
        if all_ai:
            x_range = max(x_range, max(all_ai) * 1.3)

        src  = ai_analysis.get("data_source_hint", "image")
        conf = ai_analysis.get("analysis_confidence", "?")
        ai_title_suffix = f"  +  🤖 AI overlay ({src} · {conf})"

    # ── Current price line ──────────────────────────────────────────────────
    if current_price > 0:
        fig.add_hline(
            y=current_price, line_color="#f7931a", line_width=2, line_dash="dot",
            annotation_text=f"  Current ${current_price:,.0f}",
            annotation_position="right",
            annotation_font=dict(color="#f7931a", size=11),
        )

    fig.update_layout(
        title=dict(
            text=("Estimated Liquidation Clusters  ·  red = long  ·  green = short  ·  "
                  f"7-day OI + leverage dist.{ai_title_suffix}"),
            font=dict(color="#e6edf3", size=13),
        ),
        paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
        font=dict(color="#8b949e"), barmode="overlay",
        xaxis=dict(gridcolor="#21262d", color="#8b949e", range=[-x_range, x_range],
                   tickformat=".1f", ticksuffix="M", title="Est. Liquidation (USD)",
                   zeroline=True, zerolinecolor="#30363d", zerolinewidth=1),
        yaxis=dict(gridcolor="#21262d", color="#8b949e", tickprefix="$", title="Price Level"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1,
                    font=dict(color="#e6edf3"), orientation="h",
                    yanchor="bottom", y=1.01, xanchor="right", x=1),
        margin=dict(l=10, r=120, t=60, b=10),
        height=560,
    )
    return fig

# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

TIMEFRAME_CONFIG = {
    "15m": ("15m", 192,  "15-min candles · last 48h · Weekly plan"),
    "1H":  ("1h",  168,  "1-hour candles · last 7d  ·  Weekly options cycle"),
}

PROXIMITY_THRESHOLD = 0.005   # 0.5%

# ---------------------------------------------------------------------------
# Signal alert banner
# ---------------------------------------------------------------------------

def render_signal_banner(current_price, long_cluster, short_cluster,
                          max_pain, weekly_max_pain):
    """
    Render colored alert banners when price is within PROXIMITY_THRESHOLD
    of any key level.
      RED    — near long liquidation cluster  (support / liq magnet below)
      GREEN  — near short liquidation cluster (resistance / liq magnet above)
      PURPLE — near max pain (options gravity)
    Multiple alerts can fire simultaneously; all are shown stacked.
    """
    if not current_price:
        return

    alerts = []

    def _pct(lvl):
        return abs(current_price - lvl) / current_price

    # Long liq cluster (below price) — RED
    if long_cluster and _pct(long_cluster) <= PROXIMITY_THRESHOLD:
        dist = (current_price - long_cluster) / current_price * 100
        alerts.append({
            "bg":     "#2d0a0a",
            "border": "#f85149",
            "icon":   "🔴",
            "label":  "LONG LIQ ZONE",
            "label_color": "#f85149",
            "price":  f"${long_cluster:,.0f}",
            "dist":   f"{dist:.2f}% below",
            "action": (
                f"Price is {dist:.2f}% from the long liquidation cluster "
                f"at ${long_cluster:,.0f}. "
                f"A wick to this level may cascade long liquidations — "
                f"watch for a sharp bounce or acceleration lower."
            ),
        })

    # Short liq cluster (above price) — GREEN
    if short_cluster and _pct(short_cluster) <= PROXIMITY_THRESHOLD:
        dist = (short_cluster - current_price) / current_price * 100
        alerts.append({
            "bg":     "#0a2d12",
            "border": "#3fb950",
            "icon":   "🟢",
            "label":  "SHORT LIQ ZONE",
            "label_color": "#3fb950",
            "price":  f"${short_cluster:,.0f}",
            "dist":   f"{dist:.2f}% above",
            "action": (
                f"Price is {dist:.2f}% from the short liquidation cluster "
                f"at ${short_cluster:,.0f}. "
                f"A wick to this level may cascade short liquidations — "
                f"watch for a sharp rejection or continuation higher."
            ),
        })

    # Weekly max pain — PURPLE (takes priority over aggregate)
    mp_level = weekly_max_pain or max_pain
    mp_label = "WEEKLY MAX PAIN" if weekly_max_pain else "MAX PAIN"
    if mp_level and _pct(mp_level) <= PROXIMITY_THRESHOLD:
        dist_raw = current_price - mp_level
        dist_dir = f"{abs(dist_raw)/current_price*100:.2f}% {'above' if dist_raw > 0 else 'below'}"
        alerts.append({
            "bg":     "#18082e",
            "border": "#bc8cff",
            "icon":   "🟣",
            "label":  mp_label,
            "label_color": "#bc8cff",
            "price":  f"${mp_level:,.0f}",
            "dist":   dist_dir,
            "action": (
                f"Price is {dist_dir} from {mp_label} at ${mp_level:,.0f}. "
                f"Options writers' pain is maximised near this level — "
                f"expect pinning pressure and elevated volatility around expiry."
            ),
        })

    for a in alerts:
        st.markdown(
            f'<div style="background:{a["bg"]};border:1.5px solid {a["border"]};'
            f'border-radius:10px;padding:.75rem 1.4rem;margin-bottom:.5rem;'
            f'display:flex;align-items:flex-start;gap:1rem;">'

            f'<div style="font-size:1.5rem;line-height:1;padding-top:.1rem;">{a["icon"]}</div>'

            f'<div style="flex:1;">'
            f'<div style="display:flex;align-items:baseline;gap:.6rem;margin-bottom:.3rem;'
            f'flex-wrap:wrap;">'
            f'<span style="font-size:0.72rem;font-weight:700;color:{a["label_color"]};'
            f'text-transform:uppercase;letter-spacing:.12em;">⚡ SIGNAL: {a["label"]}</span>'
            f'<span style="font-size:1rem;font-weight:700;color:{a["label_color"]};">'
            f'{a["price"]}</span>'
            f'<span style="font-size:0.78rem;color:#8b949e;">{a["dist"]}</span>'
            f'</div>'
            f'<div style="font-size:0.82rem;color:#c9d1d9;line-height:1.55;">'
            f'{a["action"]}'
            f'</div>'
            f'</div>'

            f'</div>',
            unsafe_allow_html=True,
        )



# ── SNAKE AR + MM COST ANALYSIS ─────────────────────────────────────────────

def get_top5_liq_clusters(
    clusters_df: "pd.DataFrame",
    current_price: float,
    n_total: int = 5,
) -> "pd.DataFrame":
    """
    Gom các price-level $500 lân cận thành N cụm thanh khoản lớn nhất.

    Thuật toán:
      1. Gộp các bin $500 thành băng $2500 (coarse_bin).
      2. Tính weighted-average price mỗi băng theo USD liq tương ứng.
      3. Lấy top (n_total-2) băng BÊN DƯỚI theo long_liq_usd  (liq target DOWN).
         Lấy top 2 băng BÊN TRÊN  theo short_liq_usd (liq target UP).
      4. Trả về DataFrame với cột: price_level, long_liq_usd, short_liq_usd.
    """
    if clusters_df is None or clusters_df.empty or not current_price:
        return pd.DataFrame()

    BIN_W = 2500  # gom các bin $500 → vùng $2500
    df = clusters_df.copy()

    below = df[df["price_level"] <  current_price].copy()
    above = df[df["price_level"] >= current_price].copy()

    def _top_bands(side_df: "pd.DataFrame", key_col: str, n: int) -> "pd.DataFrame":
        if side_df.empty or n <= 0:
            return pd.DataFrame()
        side_df = side_df.copy()
        side_df["band"] = (side_df["price_level"] // BIN_W) * BIN_W

        agg = side_df.groupby("band", as_index=False).agg(
            long_liq_usd=("long_liq_usd",  "sum"),
            short_liq_usd=("short_liq_usd", "sum"),
        )

        # Weighted-average price trong mỗi band (weight = key_col)
        def _wavg(g: "pd.DataFrame") -> float:
            w = g[key_col]
            return float((g["price_level"] * w).sum() / w.sum()) if w.sum() > 0 else float(g["price_level"].mean())

        price_map = side_df.groupby("band").apply(_wavg)
        agg["price_level"] = agg["band"].map(price_map)
        return agg.nlargest(n, key_col)[["price_level", "long_liq_usd", "short_liq_usd"]]

    n_above = min(2, n_total)
    n_below = n_total - n_above

    bot = _top_bands(below, "long_liq_usd",  n_below)
    top = _top_bands(above, "short_liq_usd", n_above)

    result = pd.concat([bot, top], ignore_index=True)
    return result


def calculate_snake_ar(current_price, clusters_df, max_pain, weekly_max_pain,
                       funding_rate_pct, pc_ratio, oi_total_usd):
    """
    Snake Attractive Ratio framework — sử dụng top-5 liquidation clusters.
    Snake_AR   = Cluster_Price / |Distance|
    MP_align   = 1 - |Cluster - MaxPain| / MaxPain
    Time_grav  = 1 / sqrt(DaysToExpiry + 1)
    AR_tw      = Snake_AR × MP_align × Time_grav

    MM Cost & Revenue estimates per cluster.
    ROI_score  = AR_tw × (Revenue / Cost)
    """
    if clusters_df is None or clusters_df.empty or not current_price:
        return pd.DataFrame()

    # ── Chỉ dùng top-5 clusters có revenue lớn nhất ──────────────────────────
    clusters_df = get_top5_liq_clusters(clusters_df, current_price, n_total=5)
    if clusters_df.empty:
        return pd.DataFrame()

    mp = max_pain or current_price
    wmp = weekly_max_pain or mp

    # Days to next Friday
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    days_to_expiry = days_ahead
    time_grav = 1.0 / np.sqrt(days_to_expiry + 1)

    rows = []
    for _, row in clusters_df.iterrows():
        lvl       = float(row["price_level"])
        long_usd  = float(row.get("long_liq_usd", 0))
        short_usd = float(row.get("short_liq_usd", 0))

        # Skip trivial levels — hạ ngưỡng xuống 100K để fallback cluster cũng qua được
        if long_usd + short_usd < 100_000:
            continue

        direction = "DOWN" if lvl < current_price else "UP"
        distance  = abs(lvl - current_price)
        if distance < 1:
            continue

        # Core Snake AR
        snake_ar = lvl / distance

        # MP alignment — how close is this cluster to max pain?
        mp_align = max(0.0, 1.0 - abs(lvl - wmp) / max(wmp, 1))

        # AR with time weight
        ar_tw = snake_ar * mp_align * time_grav

        # ── MM Cost to push price to this level ─────────────────────────────
        # Simplified: cost ≈ orderbook_depth_estimate × distance_pct
        dist_pct   = distance / current_price
        # Rough cost formula: OI × dist_pct × friction_factor
        # Downside cheaper (0.6x) — ROI asymmetry principle from Snake math
        friction   = 0.6 if direction == "DOWN" else 1.0
        push_cost  = oi_total_usd * dist_pct * 0.012 * friction   # ~1.2% of OI per 1% move

        # ── MM Revenue from sweeping this cluster ────────────────────────────
        # MM captures counter-party spread on forced liquidations (~4-6%)
        liq_value  = long_usd if direction == "DOWN" else short_usd
        liq_rev    = liq_value * 0.05   # 5% capture rate

        # ── Options revenue if price settles near this level then Max Pain ───
        # Simplified: proportional to distance from max pain × PC ratio signal
        mp_dist_pct = abs(lvl - wmp) / max(wmp, 1)
        opt_rev     = oi_total_usd * 0.001 * (1 - mp_dist_pct) * (1.5 - pc_ratio)

        # ── Funding cost to hold position 7 days ────────────────────────────
        daily_funding = abs(funding_rate_pct / 100) * oi_total_usd * 3
        total_funding_cost = daily_funding * 1   # 1 ngày — short-term analysis

        # ── Net economics ────────────────────────────────────────────────────
        total_revenue = liq_rev + max(opt_rev, 0)
        net_profit    = total_revenue - push_cost - total_funding_cost
        roi_pct       = (net_profit / max(push_cost, 1)) * 100

        # ── ROI Score (Snake AR weighted by economics) ───────────────────────
        rev_cost_ratio = total_revenue / max(push_cost, 1)
        roi_score      = ar_tw * rev_cost_ratio
        # MM action label
        if liq_value >= 3e6:
            mm_action = "🔴 SWEEP"
        elif liq_value >= 1e6:
            mm_action = "👁 WATCH"
        else:
            mm_action = "🔵 TEST"

        rows.append({
            "mm_action": mm_action,
            "price_level":     lvl,
            "direction":       direction,
            "distance":        distance,
            "dist_pct":        dist_pct * 100,
            "liq_value":       liq_value,
            "snake_ar":        snake_ar,
            "mp_align":        mp_align,
            "time_grav":       time_grav,
            "ar_tw":           ar_tw,
            "push_cost":       push_cost,
            "liq_revenue":     liq_rev,
            "options_revenue": max(opt_rev, 0),
            "funding_cost":    total_funding_cost,
            "total_revenue":   total_revenue,
            "net_profit":      net_profit,
            "roi_pct":         roi_pct,
            "roi_score":       roi_score,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.sort_values("roi_score", ascending=False).reset_index(drop=True)
    return df


def calculate_scenario_costs(scenarios, snake_df, current_price,
                              oi_total_usd, funding_rate_pct):
    """
    For each scenario A-D, compute total MM cost, revenue, net profit, ROI.
    Uses Snake AR data to price each leg of the journey.
    """
    if not scenarios or snake_df is None or snake_df.empty:
        return {}

    results = {}
    for key, sc in scenarios.items():
        pts = sc.get("points", [])
        if len(pts) < 2:
            continue

        total_cost    = 0.0
        total_rev     = 0.0
        total_funding = 0.0
        legs          = []

        for i in range(len(pts) - 1):
            _, p_start = pts[i]
            _, p_end   = pts[i + 1]
            leg_dist   = abs(p_end - p_start)
            leg_pct    = leg_dist / max(current_price, 1)
            direction  = "DOWN" if p_end < p_start else "UP"
            friction   = 0.6 if direction == "DOWN" else 1.0

            # Cost for this leg
            leg_cost   = oi_total_usd * leg_pct * 0.012 * friction

            # Revenue: check if any cluster is swept in this leg
            leg_rev    = 0.0
            if not snake_df.empty:
                for _, sr in snake_df.iterrows():
                    lvl = sr["price_level"]
                    if direction == "DOWN" and p_end <= lvl <= p_start:
                        leg_rev += sr["liq_revenue"]
                    elif direction == "UP" and p_start <= lvl <= p_end:
                        leg_rev += sr["liq_revenue"]

            # Funding cost per leg (proportional to time)
            daily_f   = abs(funding_rate_pct / 100) * oi_total_usd * 3
            leg_days  = (pts[i + 1][0] - pts[i][0])   # day_offset difference
            leg_fund  = daily_f * leg_days

            total_cost    += leg_cost
            total_rev     += leg_rev
            total_funding += leg_fund
            legs.append({
                "from": p_start, "to": p_end,
                "direction": direction,
                "cost": leg_cost, "revenue": leg_rev,
            })

        net  = total_rev - total_cost - total_funding
        roi  = (net / max(total_cost, 1)) * 100
        results[key] = {
            "label":          sc["label"],
            "prob":           sc["prob"],
            "color":          sc["color"],
            "total_cost":     total_cost,
            "total_revenue":  total_rev,
            "funding_cost":   total_funding,
            "net_profit":     net,
            "roi_pct":        roi,
            "legs":           legs,
        }

    return results

## ════════════════════════════════════════════════════════════════

def render_three_max_pain_card(weekly_mp, monthly_mp, quarterly_mp,
                                monthly_expiry, quarterly_expiry,
                                current_price):
    """Hiển thị 3 Max Pain levels + khoảng cách từ giá hiện tại."""
    st.markdown(
        '<div class="section-title">🎯 3-Layer Max Pain · Weekly / Monthly / Quarterly</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)

    def mp_card(col, label, price, expiry_date, color, sub=""):
        dist = ""
        arrow = ""
        if current_price and price:
            pct  = (price - current_price) / current_price * 100
            arrow = "⬆" if pct > 0 else "⬇"
            dist  = f"{arrow} {abs(pct):.1f}% from current"
        exp_str = expiry_date.strftime("%d %b %Y") if expiry_date else "—"
        col.markdown(f"""
        <div class="metric-card" style="border-color:{color}">
            <div class="metric-label">{label}</div>
            <div class="metric-value" style="color:{color};font-size:1.5rem">
                ${price:,.0f}
            </div>
            <div class="metric-sub" style="color:#8b949e">
                Expire: {exp_str}<br>{dist}<br>{sub}
            </div>
        </div>""", unsafe_allow_html=True)

    with c1:
        if weekly_mp:
            mp_card(c1, "📅 Weekly Max Pain",
                    weekly_mp, None, "#bc8cff",
                    "Next Friday")
    with c2:
        if monthly_mp:
            mp_card(c2, "🗓 Monthly Max Pain",
                    monthly_mp, monthly_expiry, "#58a6ff",
                    "Last Friday of month")
    with c3:
        if quarterly_mp:
            mp_card(c3, "📊 Quarterly Max Pain",
                    quarterly_mp, quarterly_expiry, "#ffd700",
                    "Last Friday of quarter")
    with c4:
        # Hướng tổng hợp
        if weekly_mp and monthly_mp:
            trend = "⬇ BEARISH" if monthly_mp < current_price else "⬆ BULLISH"
            color = "#f85149" if monthly_mp < current_price else "#3fb950"
            gravity = abs(monthly_mp - weekly_mp)
            st.markdown(f"""
            <div class="metric-card" style="border-color:{color}">
                <div class="metric-label">🧲 MP Gravity Direction</div>
                <div class="metric-value" style="color:{color};font-size:1.3rem">
                    {trend}
                </div>
                <div class="metric-sub" style="color:#8b949e">
                    Weekly→Monthly gap: ${gravity:,.0f}<br>
                    Monthly MP is the true gravity
                </div>
            </div>""", unsafe_allow_html=True)

    # ── Timeline visual ──────────────────────────────────────────────────────
    if weekly_mp and monthly_mp:
        st.markdown("<br>", unsafe_allow_html=True)

        fig_mp = go.Figure()

        levels = []
        if weekly_mp:
            levels.append(("Weekly MP", weekly_mp, "#bc8cff", "dash"))
        if monthly_mp:
            levels.append(("Monthly MP", monthly_mp, "#58a6ff", "dashdot"))
        if quarterly_mp:
            levels.append(("Quarterly MP", quarterly_mp, "#ffd700", "dot"))
        levels.append(("Current Price", current_price, "#f7931a", "solid"))

        for name, lvl, color, dash in levels:
            fig_mp.add_hline(
                y=lvl,
                line_color=color,
                line_dash=dash,
                line_width=2,
                annotation_text=f"  {name} ${lvl:,.0f}",
                annotation_position="right",
                annotation_font_color=color,
            )

        # Vẽ "gravity arrows" từ current price đến mỗi MP
        for name, lvl, color, _ in levels[:-1]:
            fig_mp.add_annotation(
                x=0.5, xref="paper",
                y=(current_price + lvl) / 2,
                text=f"${abs(lvl - current_price):,.0f} ({abs((lvl-current_price)/current_price*100):.1f}%)",
                showarrow=True,
                arrowhead=2,
                arrowcolor=color,
                arrowsize=1.5,
                ax=0,
                ay=-40 if lvl < current_price else 40,
                font=dict(color=color, size=10),
            )

        fig_mp.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0d1117",
            plot_bgcolor="#0d1117",
            height=280,
            title="Max Pain Gravity Map · 3 Timeframes",
            yaxis=dict(
                title="Price ($)",
                gridcolor="#21262d",
                tickformat="$,.0f",
                range=[
                    min(quarterly_mp or monthly_mp, current_price) * 0.95,
                    max(quarterly_mp or monthly_mp, current_price) * 1.05,
                ],
            ),
            xaxis=dict(visible=False),
            margin=dict(l=0, r=120, t=40, b=0),
        )
        st.plotly_chart(fig_mp, use_container_width=True,
                        config={"displayModeBar": False})

        # ── Scenario D summary dựa trên 3 Max Pain ──────────────────────────
        st.markdown("**🐍 Scenario D — MM Master Plan Path (3 Max Pain Targets)**")

        weekly_dir  = "⬇ Xuống" if weekly_mp  < current_price else "⬆ Lên"
        monthly_dir = "⬇ Xuống" if monthly_mp < current_price else "⬆ Lên"
        qdir        = "⬇ Xuống" if (quarterly_mp or monthly_mp) < current_price else "⬆ Lên"

        st.markdown(f"""
        <div style="background:#161b22;border:1px solid #ffd700;border-radius:8px;padding:1rem;margin-top:0.5rem">
            <div style="color:#ffd700;font-weight:700;margin-bottom:0.5rem">
                🗺 MM Optimal Route — 4H Timeframe
            </div>
            <div style="color:#e6edf3;font-size:0.9rem;line-height:1.8">
                <b style="color:#f7931a">Hiện tại:</b> ${current_price:,.0f}<br>
                <b style="color:#bc8cff">Tuần này [1H]:</b>
                    Sweep clusters → {weekly_dir} về <b>${weekly_mp:,.0f}</b>
                    (Weekly Max Pain)<br>
                <b style="color:#58a6ff">Tháng này [4H]:</b>
                    {monthly_dir} về <b>${monthly_mp:,.0f}</b>
                    (Monthly Max Pain — thứ 6 cuối tháng)<br>
                <b style="color:#ffd700">Quý này [4H+]:</b>
                    {qdir} về <b>${quarterly_mp or monthly_mp:,.0f}</b>
                    (Quarterly Max Pain)<br>
                <br>
                <b style="color:#3fb950">Logic MM:</b>
                    Mỗi kỳ hạn là 1 "gravity well" — MM điều hướng giá
                    qua từng mức để maximize options premium thu được.
                    Monthly MP là mục tiêu quan trọng nhất vì notional lớn nhất.
            </div>
        </div>
        """, unsafe_allow_html=True)
def render_snake_ar_section(snake_df, scenario_costs, current_price,
                             long_cluster, short_cluster, max_pain):
    """Render the full Snake AR + MM Cost Analysis section."""

    st.markdown(
        '<div class="section-title">🐍 Snake AR · MM Cost & ROI Analysis</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Snake Attractive Ratio quantifies how appealing each liquidation cluster is "
        "to Market Makers. Higher AR_tw = MM more likely to target. "
        "Downside always cheaper (ROI asymmetry). All figures are model estimates."
    )

    if snake_df is None or snake_df.empty:
        st.markdown("""
        <div style="background:#161b22;border:1px solid #f7931a44;border-radius:8px;
                    padding:14px 18px;color:#c9d1d9;font-size:.84rem;line-height:1.7;">
          <b style="color:#f7931a">🐍 Snake AR</b> — đang chờ dữ liệu cluster thanh lý…<br>
          <span style="color:#8b949e">Dashboard cần tích lũy ít nhất <b>2 price bucket</b>
          OI lịch sử để tính Snake AR. Thường mất 1–2 phút sau khi load xong.
          Nếu lỗi kéo dài, thử bấm <b>Refresh</b> trình duyệt.</span>
        </div>
        """, unsafe_allow_html=True)
        return

    # ── Top-level Snake AR score cards ─────────────────────────────────────────
    top = snake_df.iloc[0]
    second = snake_df.iloc[1] if len(snake_df) > 1 else None

    s1, s2, s3, s4 = st.columns(4)
    arrow = "⬇" if top["direction"] == "DOWN" else "⬆"
    with s1:
        st.markdown(f"""
        <div class="metric-card" style="border-color:#ffd700">
            <div class="metric-label">🎯 Top MM Target</div>
            <div class="metric-value orange">${top['price_level']:,.0f}</div>
            <div class="metric-sub" style="color:#ffd700">{arrow} {top['direction']} · AR_tw {top['ar_tw']:.2f}</div>
        </div>""", unsafe_allow_html=True)
    with s2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">ROI Score (top)</div>
            <div class="metric-value {'green' if top['roi_score'] > 0 else 'red'}">{top['roi_score']:.1f}</div>
            <div class="metric-sub" style="color:#8b949e">Net ROI {top['roi_pct']:.0f}%</div>
        </div>""", unsafe_allow_html=True)
    with s3:
        priority = "DOWNSIDE" if top["direction"] == "DOWN" else "UPSIDE"
        p_color  = "red" if priority == "DOWNSIDE" else "green"
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">MM Priority</div>
            <div class="metric-value {p_color}">{priority}</div>
            <div class="metric-sub" style="color:#8b949e">Cheaper path for MM</div>
        </div>""", unsafe_allow_html=True)
    with s4:
        if second is not None:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">2nd Target</div>
                <div class="metric-value">{'⬇' if second['direction']=='DOWN' else '⬆'} ${second['price_level']:,.0f}</div>
                <div class="metric-sub" style="color:#8b949e">AR_tw {second['ar_tw']:.2f}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Snake AR Heatmap Table ──────────────────────────────────────────────────
    st.markdown("**🗂 Snake AR Cluster Rankings** · sorted by ROI Score")

    display_cols = {
        "mm_action":      "MM Action",
        "price_level":    "Price Level",
        "direction":      "Dir",
        "dist_pct":       "Dist %",
        "ar_tw":          "AR_tw",
        "push_cost":      "Push Cost",
        "liq_revenue":    "Liq Revenue",
        "net_profit":     "Net Profit",
        "roi_pct":        "ROI %",
        "roi_score":      "ROI Score",
    }
    tbl = snake_df[list(display_cols.keys())].head(12).copy()
    tbl.columns = list(display_cols.values())

    # Format
    tbl["Price Level"] = tbl["Price Level"].apply(lambda x: f"${x:,.0f}")
    tbl["Dist %"]      = tbl["Dist %"].apply(lambda x: f"{x:.1f}%")
    tbl["AR_tw"]       = tbl["AR_tw"].apply(lambda x: f"{x:.2f}")
    tbl["Push Cost"]   = tbl["Push Cost"].apply(lambda x: f"${x/1e6:.1f}M")
    tbl["Liq Revenue"] = tbl["Liq Revenue"].apply(lambda x: f"${x/1e6:.1f}M")
    def _fmt_net(x):
        if x < -999e6:
            return f"-${abs(x)/1e9:.2f}B"
        return f"${x/1e6:.1f}M"
    tbl["Net Profit"] = tbl["Net Profit"].apply(_fmt_net)
    def _fmt_roi(x):
        if x < -9999:
            return f">{int(x/1000)}K%"
        return f"{x:.0f}%"
    tbl["ROI %"] = tbl["ROI %"].apply(_fmt_roi)
    tbl["ROI Score"]   = tbl["ROI Score"].apply(lambda x: f"{x:.1f}")

    st.dataframe(tbl, hide_index=True, use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Scenario Cost & ROI Comparison ─────────────────────────────────────────
    if scenario_costs:
        st.markdown("**💰 MM Scenario Cost & ROI Comparison**")

        cols = st.columns(4)
        best_roi   = max(scenario_costs.values(), key=lambda x: x["roi_pct"])
        best_label = best_roi["label"]

        for i, (key, sc) in enumerate(scenario_costs.items()):
            is_best  = sc["label"] == best_label
            border   = sc["color"]
            star     = " ★ HIGHEST ROI" if is_best else ""
            roi_col  = "green" if sc["roi_pct"] > 0 else "red"
            net_col  = "green" if sc["net_profit"] > 0 else "red"
            with cols[i]:
                st.markdown(f"""
                <div class="metric-card" style="border-color:{border};border-width:{'2px' if is_best else '1px'}">
                    <div class="metric-label" style="color:{border}">{sc['label']}{star}</div>
                    <div style="font-size:0.8rem;margin-top:0.4rem;color:#e6edf3">
                        <b>Cost:</b> ${sc['total_cost']/1e6:.0f}M<br>
                        <b>Revenue:</b> ${sc['total_revenue']/1e6:.0f}M<br>
                        <b>Funding:</b> -${sc['funding_cost']/1e6:.0f}M<br>
                        <b>Net:</b> <span class="{net_col}">${sc['net_profit']/1e6:.0f}M</span><br>
                        <b>ROI:</b> <span class="{roi_col}">{sc['roi_pct']:.0f}%</span><br>
                        <b>Probability:</b> {sc['prob']*100:.0f}%
                    </div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── ROI Bar Chart comparing scenarios ──────────────────────────────────
        fig_roi = go.Figure()
        labels  = [sc["label"] for sc in scenario_costs.values()]
        rois    = [sc["roi_pct"] for sc in scenario_costs.values()]
        colors  = [sc["color"] for sc in scenario_costs.values()]
        costs   = [sc["total_cost"] / 1e6 for sc in scenario_costs.values()]
        revs    = [sc["total_revenue"] / 1e6 for sc in scenario_costs.values()]

        fig_roi.add_trace(go.Bar(
            name="Push Cost ($M)", x=labels, y=costs,
            marker_color="rgba(248,81,73,0.7)", text=[f"${c:.0f}M" for c in costs],
            textposition="auto",
        ))
        fig_roi.add_trace(go.Bar(
            name="Revenue ($M)", x=labels, y=revs,
            marker_color="rgba(63,185,80,0.7)", text=[f"${r:.0f}M" for r in revs],
            textposition="auto",
        ))
        fig_roi.add_trace(go.Scatter(
            name="ROI %", x=labels, y=rois,
            mode="lines+markers+text",
            line=dict(color="#ffd700", width=2),
            marker=dict(size=10, color=colors),
            text=[f"{r:.0f}%" for r in rois],
            textposition="top center",
            yaxis="y2",
        ))
        fig_roi.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0d1117",
            plot_bgcolor="#0d1117",
            barmode="group",
            height=320,
            title="MM Scenario: Cost vs Revenue vs ROI",
            yaxis=dict(title="USD ($M)", gridcolor="#21262d"),
            yaxis2=dict(title="ROI %", overlaying="y", side="right",
                        gridcolor="#21262d"),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
            margin=dict(l=0, r=60, t=40, b=0),
        )
        st.plotly_chart(fig_roi, use_container_width=True,
                        config={"displayModeBar": False})

        # ── Snake AR visualization on price axis ───────────────────────────────
        st.markdown("**🐍 Snake AR by Price Level** · bubble size = ROI Score")

        fig_snake = go.Figure()

        # Reference lines
        for lvl, label, color in [
            (current_price, f"Current ${current_price:,.0f}", "#f7931a"),
            (long_cluster,  f"Long Cluster ${long_cluster:,.0f}", "#f85149"),
            (short_cluster, f"Short Cluster ${short_cluster:,.0f}", "#3fb950"),
            (max_pain,      f"Max Pain ${max_pain:,.0f}", "#a371f7"),
        ]:
            if lvl and lvl > 0:
                fig_snake.add_hline(y=lvl, line_color=color, line_dash="dot",
                                    line_width=1,
                                    annotation_text=label,
                                    annotation_position="right",
                                    annotation_font_color=color)

        # Down clusters
        dn = snake_df[snake_df["direction"] == "DOWN"]
        if not dn.empty:
            fig_snake.add_trace(go.Scatter(
                x=dn["ar_tw"],
                y=dn["price_level"],
                mode="markers+text",
                name="DOWN clusters (long liq)",
                marker=dict(
                    color="#f85149",
                    size=np.clip(dn["roi_score"].abs() * 3, 8, 40),
                    opacity=0.85,
                    line=dict(color="#c0392b", width=1),
                ),
                text=dn["price_level"].apply(lambda x: f"${x:,.0f}"),
                textposition="middle right",
                textfont=dict(size=9, color="#f85149"),
                hovertemplate=(
                    "<b>$%{y:,.0f}</b><br>"
                    "AR_tw: %{x:.2f}<br>"
                    "Cost: $%{customdata[0]:.0f}M<br>"
                    "Revenue: $%{customdata[1]:.0f}M<br>"
                    "ROI: %{customdata[2]:.0f}%<br>"
                    "<extra></extra>"
                ),
                customdata=np.stack([
                    dn["push_cost"] / 1e6,
                    dn["liq_revenue"] / 1e6,
                    dn["roi_pct"],
                ], axis=-1),
            ))

        # Up clusters
        up = snake_df[snake_df["direction"] == "UP"]
        if not up.empty:
            fig_snake.add_trace(go.Scatter(
                x=up["ar_tw"],
                y=up["price_level"],
                mode="markers+text",
                name="UP clusters (short liq)",
                marker=dict(
                    color="#3fb950",
                    size=np.clip(up["roi_score"].abs() * 3, 8, 40),
                    opacity=0.85,
                    line=dict(color="#2ea043", width=1),
                ),
                text=up["price_level"].apply(lambda x: f"${x:,.0f}"),
                textposition="middle right",
                textfont=dict(size=9, color="#3fb950"),
                hovertemplate=(
                    "<b>$%{y:,.0f}</b><br>"
                    "AR_tw: %{x:.2f}<br>"
                    "Cost: $%{customdata[0]:.0f}M<br>"
                    "Revenue: $%{customdata[1]:.0f}M<br>"
                    "ROI: %{customdata[2]:.0f}%<br>"
                    "<extra></extra>"
                ),
                customdata=np.stack([
                    up["push_cost"] / 1e6,
                    up["liq_revenue"] / 1e6,
                    up["roi_pct"],
                ], axis=-1),
            ))

        fig_snake.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0d1117",
            plot_bgcolor="#0d1117",
            height=500,
            title="Snake AR Map · x-axis = AR_tw (attractiveness) · y-axis = price level",
            xaxis=dict(title="Snake AR_tw (higher = more attractive to MM)",
                       gridcolor="#21262d", zeroline=True,
                       zerolinecolor="#30363d"),
            yaxis=dict(title="Price Level ($)", gridcolor="#21262d",
                       tickformat="$,.0f"),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
            margin=dict(l=0, r=120, t=40, b=0),
        )
        st.plotly_chart(fig_snake, use_container_width=True,
                        config={"displayModeBar": False})

    st.caption(
        "⚠️ Snake AR and MM Cost figures are mathematical estimates based on public "
        "Binance/Deribit data. Actual MM positions and costs are private. "
        "Use as a probabilistic framework, not as financial advice."
    )



def render_multi_exchange_liq(df_all: pd.DataFrame):
    """Hiển thị liquidation tổng hợp 5 sàn."""
    st.markdown(
        '<div class="section-title">⚡ Multi-Exchange Liquidations · Binance + Bybit + OKX + Hyperliquid + Coinbase</div>',
        unsafe_allow_html=True,
    )

    if df_all.empty:
        st.info("Đang thu thập data liquidation từ các sàn… Vui lòng chờ 1-2 phút.")
        return

    stats = calc_exchange_stats(df_all)
    exchanges = ["Binance", "Bybit", "OKX", "Hyperliquid", "Coinbase"]
    exchange_colors = {
        "Binance":     "#f3ba2f",
        "Bybit":       "#ff6b35",
        "OKX":         "#00d4aa",
        "Hyperliquid": "#9b59b6",
        "Coinbase":    "#0052ff",
    }

    # ── Tổng overview cards ──────────────────────────────────────────────────
    total_long  = sum(s.get("long_liq",  0) for s in stats.values())
    total_short = sum(s.get("short_liq", 0) for s in stats.values())
    total_all   = total_long + total_short
    dom_side    = "LONG liq dominates ⬇" if total_long > total_short else "SHORT liq dominates ⬆"
    dom_color   = "#f85149" if total_long > total_short else "#3fb950"

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"""<div class="metric-card">
        <div class="metric-label">Total Liquidated</div>
        <div class="metric-value orange">${total_all/1e6:.1f}M</div>
        <div class="metric-sub" style="color:#8b949e">{len(df_all)} events</div>
    </div>""", unsafe_allow_html=True)
    c2.markdown(f"""<div class="metric-card" style="border-color:#f85149">
        <div class="metric-label">Long Liquidated</div>
        <div class="metric-value red">${total_long/1e6:.1f}M</div>
        <div class="metric-sub" style="color:#8b949e">Buyers wiped</div>
    </div>""", unsafe_allow_html=True)
    c3.markdown(f"""<div class="metric-card" style="border-color:#3fb950">
        <div class="metric-label">Short Liquidated</div>
        <div class="metric-value green">${total_short/1e6:.1f}M</div>
        <div class="metric-sub" style="color:#8b949e">Sellers wiped</div>
    </div>""", unsafe_allow_html=True)
    c4.markdown(f"""<div class="metric-card" style="border-color:{dom_color}">
        <div class="metric-label">Dominance Signal</div>
        <div class="metric-value" style="color:{dom_color};font-size:1rem">{dom_side}</div>
        <div class="metric-sub" style="color:#8b949e">
            L/S ratio: {total_long/max(total_short,1):.2f}
        </div>
    </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Per-exchange breakdown ───────────────────────────────────────────────
    cols = st.columns(len(exchanges))
    for i, exch in enumerate(exchanges):
        s = stats.get(exch, {})
        color = exchange_colors.get(exch, "#8b949e")
        total_e = s.get("total", 0)
        ll = s.get("long_liq", 0)
        sl = s.get("short_liq", 0)
        pct = total_e / max(total_all, 1) * 100
        cols[i].markdown(f"""
        <div class="metric-card" style="border-color:{color}">
            <div class="metric-label" style="color:{color}">{exch}</div>
            <div class="metric-value" style="color:{color};font-size:1.1rem">
                ${total_e/1e6:.1f}M
            </div>
            <div style="font-size:0.75rem;color:#e6edf3;margin-top:.3rem">
                🔴 ${ll/1e6:.1f}M<br>
                🟢 ${sl/1e6:.1f}M<br>
                <span style="color:#8b949e">{pct:.1f}% share · {s.get('count',0)} events</span>
            </div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Chart 1: Timeline liquidation events ────────────────────────────────
    fig_tl = go.Figure()
    # Last 4 hours
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    df_recent = df_all[df_all["time"] >= cutoff].copy()

    if not df_recent.empty:
        for exch in exchanges:
            sub = df_recent[df_recent["exchange"] == exch]
            if sub.empty:
                continue
            longs  = sub[sub["side"] == "SELL"]
            shorts = sub[sub["side"] == "BUY"]
            color  = exchange_colors.get(exch, "#8b949e")
            if not longs.empty:
                fig_tl.add_trace(go.Scatter(
                    x=longs["time"], y=longs["price"],
                    mode="markers",
                    name=f"{exch} Long Liq",
                    marker=dict(size=np.clip(longs["usd_value"]/1e5, 4, 20),
                                color="#f85149", opacity=0.7,
                                line=dict(color=color, width=1)),
                    hovertemplate=(f"<b>{exch} LONG LIQ</b><br>"
                                   "$%{y:,.0f}<br>$%{customdata:.0f}<extra></extra>"),
                    customdata=longs["usd_value"],
                ))
            if not shorts.empty:
                fig_tl.add_trace(go.Scatter(
                    x=shorts["time"], y=shorts["price"],
                    mode="markers",
                    name=f"{exch} Short Liq",
                    marker=dict(size=np.clip(shorts["usd_value"]/1e5, 4, 20),
                                color="#3fb950", opacity=0.7,
                                line=dict(color=color, width=1)),
                    hovertemplate=(f"<b>{exch} SHORT LIQ</b><br>"
                                   "$%{y:,.0f}<br>$%{customdata:.0f}<extra></extra>"),
                    customdata=shorts["usd_value"],
                ))

    fig_tl.update_layout(
        template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        height=320, title="Liquidation Events · Last 4H · All Exchanges · bubble = USD size",
        xaxis=dict(gridcolor="#21262d"),
        yaxis=dict(gridcolor="#21262d", tickprefix="$"),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=9)),
        margin=dict(l=0, r=0, t=40, b=0),
    )
    st.plotly_chart(fig_tl, use_container_width=True, config={"displayModeBar": False})

    # ── Chart 2: Exchange dominance over time (stacked bar 30-min bins) ──────
    if not df_all.empty and len(df_all) > 10:
        df_all2 = df_all.copy()
        df_all2["bin"] = df_all2["time"].dt.floor("30min")
        grouped = (df_all2.groupby(["bin", "exchange"])["usd_value"]
                          .sum().reset_index())

        fig_dom = go.Figure()
        for exch in exchanges:
            sub = grouped[grouped["exchange"] == exch]
            if sub.empty:
                continue
            fig_dom.add_trace(go.Bar(
                x=sub["bin"], y=sub["usd_value"] / 1e6,
                name=exch,
                marker_color=exchange_colors.get(exch, "#8b949e"),
                hovertemplate=f"<b>{exch}</b><br>$%{{y:.2f}}M<extra></extra>",
            ))

        fig_dom.update_layout(
            template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            barmode="stack", height=280,
            title="Exchange Liquidation Share · 30-min bins (USD)",
            xaxis=dict(gridcolor="#21262d"),
            yaxis=dict(gridcolor="#21262d", ticksuffix="M"),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=9),
                        orientation="h", y=-0.15),
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig_dom, use_container_width=True,
                        config={"displayModeBar": False})

    # ── Accumulated history stats ────────────────────────────────────────────
    if not df_all.empty:
        oldest = df_all["time"].min()
        newest = df_all["time"].max()
        duration = newest - oldest
        hours = duration.total_seconds() / 3600
        st.caption(
            f"📦 Tổng data tích luỹ: {len(df_all):,} events "
            f"· Từ {oldest.strftime('%H:%M %d/%m')} "
            f"→ {newest.strftime('%H:%M %d/%m')} "
            f"· ({hours:.1f}h)"
        )



def main():
    # ── Timeframe selector (persisted in session state) ──────────────────────
    if "timeframe" not in st.session_state:
        st.session_state.timeframe = "1H"
    if "_liq_clusters_applied" not in st.session_state:
        st.session_state._liq_clusters_applied = False

    # ── Header row: title | timeframe | UPDATE button ────────────────────────
    header_col, tf_col, btn_col = st.columns([3, 1, 1])
    with header_col:
        st.markdown("# ₿ BTC Live Dashboard")
    with tf_col:
        st.markdown("<div style='padding-top:1.1rem'></div>", unsafe_allow_html=True)
        chosen_tf = st.radio(
            "Timeframe",
            options=list(TIMEFRAME_CONFIG.keys()),
            index=list(TIMEFRAME_CONFIG.keys()).index(st.session_state.timeframe),
            horizontal=True,
            label_visibility="collapsed",
        )
        st.session_state.timeframe = chosen_tf
    with btn_col:
        st.markdown("<div style='padding-top:1.1rem'></div>", unsafe_allow_html=True)
        # ── UPDATE button: xoá cache + fetch lại giá live + cập nhật AI clusters ──
        _ai_available = bool(
            st.session_state.get("_inline_liq_analysis")
            or st.session_state.get("_liq_ai_analysis")
        )
        _btn_label = "🔄 UPDATE" if not _ai_available else "🔄 UPDATE  ✅ AI Liq"
        _btn_color = "#1a4020" if _ai_available else "#1a2030"
        _btn_border = "#4ade80" if _ai_available else "#58a6ff"
        st.markdown(
            f'<style>'
            f'div[data-testid="stButton"] button[kind="primary"] {{'
            f'background:{_btn_color}!important;border:1.5px solid {_btn_border}!important;'
            f'color:{_btn_border}!important;font-weight:700;font-size:.82rem;padding:.35rem .8rem;'
            f'}}</style>',
            unsafe_allow_html=True,
        )
        if st.button(_btn_label, type="primary", key="btn_update_chart"):
            # Xoá cache price + ticker để fetch lại ngay
            fetch_price_klines.clear()
            fetch_ticker_24h.clear()
            fetch_current_funding.clear()
            fetch_open_interest_hist.clear()
            st.session_state._liq_clusters_applied = True
            st.rerun()

    interval, limit, tf_label = TIMEFRAME_CONFIG[st.session_state.timeframe]

    # ── Thời gian hiển thị theo giờ Việt Nam UTC+7 ───────────────────────────
    now_vn      = _now_vn()
    now_str_vn  = now_vn.strftime("%H:%M:%S (UTC+7)")
    anchor_utc, anchor_vn, next_fri_vn, _ = get_weekly_anchor_vn()
    anchor_label = anchor_vn.strftime("thứ 6 %d/%m %H:%M VN")
    days_left    = max(0.0, (next_fri_vn - now_vn).total_seconds() / 3600)

    # Đếm AI clusters đang active
    _ai_now = (st.session_state.get("_inline_liq_analysis")
               or st.session_state.get("_liq_ai_analysis"))
    _ai_cluster_info = ""
    if _ai_now and isinstance(_ai_now, dict) and "error" not in _ai_now:
        _n_long  = len(_ai_now.get("long_liquidation_clusters",  []))
        _n_short = len(_ai_now.get("short_liquidation_clusters", []))
        if _n_long + _n_short > 0:
            _ai_cluster_info = (
                f' &nbsp;·&nbsp; <span style="color:#4ade80;font-weight:600;">'
                f'✅ AI Liq: {_n_long}L / {_n_short}S clusters on chart</span>'
            )

    st.markdown(
        f'<div class="last-updated">'
        f'⏰ Giờ VN: <b>{now_str_vn}</b>'
        f' &nbsp;·&nbsp; Neo: <b>{anchor_label}</b>'
        f' &nbsp;·&nbsp; Còn <b>{days_left:.1f}h</b> đến expire {next_fri_vn.strftime("%d/%m")}'
        f' &nbsp;·&nbsp; <span style="color:#58a6ff;">Live price · auto 15s</span>'
        f'{_ai_cluster_info}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Fetch REST data ──────────────────────────────────────────────────────
    with st.spinner("Fetching market data from Binance & Deribit..."):
        ticker = fetch_ticker_24h()
        price_df = fetch_price_klines(interval=interval, limit=limit)
        oi_df = fetch_open_interest_hist(period="5m", limit=60)
        funding_df = fetch_funding_rate(limit=30)
        current_funding = fetch_current_funding()
        opts_df = fetch_deribit_options()
        daily_walls = get_daily_walls(opts_df)
        max_pain, mp_strikes, mp_values = calculate_max_pain(opts_df)
        weekly_max_pain = fetch_weekly_max_pain(opts_df)
        monthly_max_pain,  monthly_expiry   = fetch_monthly_max_pain(opts_df)
        quarterly_max_pain, quarterly_expiry = fetch_quarterly_max_pain(opts_df)

        # Klines 1h cho anchor price
        fut_klines = fetch_futures_klines_1h()

    current_price = fetch_live_price()   # ← live, no cache
    if current_price <= 0:
        current_price = float(ticker.get("lastPrice", 0)) if ticker else 0
    price_change = float(ticker.get("priceChangePercent", 0)) if ticker else 0
    price_change_abs = float(ticker.get("priceChange", 0)) if ticker else 0
    volume_24h = float(ticker.get("quoteVolume", 0)) if ticker else 0
    high_24h = float(ticker.get("highPrice", 0)) if ticker else 0
    low_24h = float(ticker.get("lowPrice", 0)) if ticker else 0
    current_fr = float(current_funding.get("lastFundingRate", 0)) * 100 if current_funding else 0
    current_oi_usd = oi_df["sumOpenInterestValue"].iloc[-1] if not oi_df.empty else 0
    current_oi_btc = oi_df["sumOpenInterest"].iloc[-1] if not oi_df.empty else 0
    # Định nghĩa sớm để dùng được ở mọi nơi trong hàm main()
    oi_total_usd_val = float(oi_df["sumOpenInterestValue"].iloc[-1]) if not oi_df.empty else 0.0

    # ── Cluster data — removed. Use fixed estimates ────────────────────────
    cluster_df            = pd.DataFrame()
    cluster_calc_ts       = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%H:%M UTC")
    long_cluster_liq_usd  = 0.0
    short_cluster_liq_usd = 0.0
    long_cluster          = current_price * 0.92
    short_cluster         = current_price * 1.08
    net_long_pct          = 50.0

    # ── Anchor price: giá BTC tại 15:05 thứ 6 VN gần nhất ───────────────────
    # Dùng để neo điểm bắt đầu scenarios, tránh scale bóp méo
    anchor_price = None
    if not fut_klines.empty:
        _anchor_ts = pd.Timestamp(anchor_utc)
        _price_ts  = fut_klines["ts"].copy()
        _diff      = (_price_ts - _anchor_ts).abs()
        _closest   = _diff.idxmin()
        anchor_price = float(fut_klines.loc[_closest, "price"])
        # Nếu anchor quá xa (>3h) so với data → dùng current_price
        if _diff[_closest] > pd.Timedelta(hours=3):
            anchor_price = current_price

    total_call_oi = opts_df[opts_df["type"] == "C"]["oi"].sum() if not opts_df.empty else 1
    total_put_oi  = opts_df[opts_df["type"] == "P"]["oi"].sum() if not opts_df.empty else 0
    pc_ratio_val  = total_put_oi / total_call_oi if total_call_oi else 0.69

    scenarios = calculate_scenarios(
        current_price, weekly_max_pain, long_cluster, short_cluster,
        pc_ratio_val, current_fr, oi_df,
        monthly_max_pain=monthly_max_pain,
        quarterly_max_pain=quarterly_max_pain,
        anchor_price=anchor_price,
    ) if current_price > 0 else {}

    price_color = "green" if price_change >= 0 else "red"
    fr_color = "green" if current_fr >= 0 else "red"
    change_sign = "+" if price_change >= 0 else ""

    # ── Signal alert banners (proximity alerts for key levels) ────────────────
    render_signal_banner(current_price, long_cluster, short_cluster,
                         max_pain, weekly_max_pain)

    # ── Prominent price banner — JS live update (no page flash) ─────────────
    abs_sign = "+" if price_change_abs >= 0 else ""
    _live_ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    _chg_color = "#3fb950" if price_change >= 0 else "#f85149"
    st.markdown(f"""
    <div id="btc-price-banner"
         style="background:#161b22;border:1px solid #30363d;border-radius:10px;
                padding:1rem 1.8rem;margin-bottom:1rem;display:flex;
                align-items:center;gap:2rem;flex-wrap:wrap;">
        <div>
            <div style="font-size:0.7rem;color:#8b949e;text-transform:uppercase;
                        letter-spacing:.08em;margin-bottom:.2rem;">
              BTC / USDT &nbsp;
              <span style="background:#0a2d12;color:#3fb950;padding:1px 6px;
                border-radius:6px;font-size:.65rem;font-weight:700;">● LIVE</span>
              &nbsp;<span id="btc-live-ts" style="color:#484f58;font-size:.65rem;">{_live_ts}</span>
            </div>
            <div id="btc-live-price"
                 style="font-size:2.6rem;font-weight:800;color:#f7931a;
                        letter-spacing:-.02em;line-height:1;">${current_price:,.2f}</div>
        </div>
        <div>
            <div id="btc-live-change"
                 style="font-size:1.1rem;font-weight:600;color:{_chg_color};">
                {abs_sign}{price_change_abs:,.2f} &nbsp; ({("+" if price_change>=0 else "")}{price_change:.2f}%)
            </div>
            <div style="font-size:0.75rem;color:#8b949e;margin-top:.2rem;">24h change</div>
        </div>
        <div style="margin-left:auto;text-align:right;">
            <div style="font-size:0.75rem;color:#8b949e;">24h High</div>
            <div style="font-size:1rem;font-weight:600;color:#3fb950;">${high_24h:,.2f}</div>
        </div>
        <div style="text-align:right;">
            <div style="font-size:0.75rem;color:#8b949e;">24h Low</div>
            <div style="font-size:1rem;font-weight:600;color:#f85149;">${low_24h:,.2f}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── JS Live Price Ticker — fetch Binance từ browser mỗi 5 giây ───────────
    # Không gọi st.rerun() → không flash tối, không reset zoom chart
    import streamlit.components.v1 as _stc
    _stc.html("""
    <script>
    (function() {
      // Tìm element trong parent document (Streamlit iframe → parent)
      function findEl(id) {
        try { return window.parent.document.getElementById(id); } catch(e) { return null; }
      }

      function fmt(n, decimals) {
        return n.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
      }

      async function updatePrice() {
        try {
          const r  = await fetch('https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT');
          const d  = await r.json();
          const price   = parseFloat(d.lastPrice);
          const change  = parseFloat(d.priceChangePercent);
          const changeA = parseFloat(d.priceChange);

          const elP  = findEl('btc-live-price');
          const elC  = findEl('btc-live-change');
          const elTs = findEl('btc-live-ts');

          if (elP)  elP.textContent  = '$' + fmt(price, 2);
          if (elC) {
            const sign = changeA >= 0 ? '+' : '';
            elC.textContent = sign + fmt(changeA,2) + '  (' + sign + fmt(change,2) + '%)';
            elC.style.color = changeA >= 0 ? '#3fb950' : '#f85149';
          }
          if (elTs) {
            const now = new Date();
            elTs.textContent = now.toISOString().replace('T',' ').substring(0,19) + ' UTC';
          }
        } catch(e) { /* silent fail */ }
      }

      updatePrice();
      setInterval(updatePrice, 5000);  // cập nhật mỗi 5 giây, không re-render Python
    })();
    </script>
    """, height=0, scrolling=False)

    # ── Metric cards ─────────────────────────────────────────────────────────
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">BTC Price</div>
            <div class="metric-value orange">${current_price:,.2f}</div>
            <div class="metric-sub {price_color}">{change_sign}{price_change:.2f}% (24h)</div>
        </div>""", unsafe_allow_html=True)

    with col2:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">24h High / Low</div>
            <div class="metric-value" style="font-size:1.2rem">
                <span class="green">${high_24h:,.0f}</span> /
                <span class="red">${low_24h:,.0f}</span>
            </div>
            <div class="metric-sub" style="color:#8b949e">Range: ${high_24h - low_24h:,.0f}</div>
        </div>""", unsafe_allow_html=True)

    with col3:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">24h Volume</div>
            <div class="metric-value" style="font-size:1.4rem">${volume_24h/1e9:.2f}B</div>
            <div class="metric-sub" style="color:#8b949e">USDT</div>
        </div>""", unsafe_allow_html=True)

    with col4:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Open Interest</div>
            <div class="metric-value" style="font-size:1.4rem">${current_oi_usd/1e9:.2f}B</div>
            <div class="metric-sub" style="color:#8b949e">{current_oi_btc:,.0f} BTC</div>
        </div>""", unsafe_allow_html=True)

    with col5:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">Funding Rate</div>
            <div class="metric-value {fr_color}">{current_fr:+.4f}%</div>
            <div class="metric-sub" style="color:#8b949e">per 8h interval</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Price chart ──────────────────────────────────────────────────────────
    # ── Load AI liq clusters from session state (nếu đã phân tích ảnh) ────────
    _ai_liq = (st.session_state.get("_inline_liq_analysis")
               or st.session_state.get("_liq_ai_analysis"))

    if not price_df.empty:
        st.plotly_chart(
            make_price_chart_patched(
                price_df, tf_label=tf_label,
                current_price=current_price, max_pain=max_pain,
                weekly_max_pain=weekly_max_pain, scenarios=scenarios,
                ls_ratio_val=net_long_pct / 100.0,
                net_long_pct=net_long_pct,
                current_fr=current_fr,
                long_cluster=long_cluster,
                short_cluster=short_cluster,
                long_cluster_liq_usd=long_cluster_liq_usd,
                short_cluster_liq_usd=short_cluster_liq_usd,
                timeframe=st.session_state.timeframe,
                daily_walls=daily_walls,
                monthly_max_pain=monthly_max_pain,
                ai_liq_clusters=_ai_liq,
            ),
            width="stretch",
            config={
                "displayModeBar": True,
                "modeBarButtonsToAdd": [
                    "drawline",
                    "drawopenpath",
                    "drawclosedpath",
                    "drawcircle",
                    "drawrect",
                    "eraseshape",
                ],
                "modeBarButtonsToRemove": ["toImage", "sendDataToCloud"],
                "scrollZoom": True,
            },
        )

        # ── AI cluster status hint ────────────────────────────────────────────
        if _ai_liq and isinstance(_ai_liq, dict) and "error" not in _ai_liq:
            _nl = len(_ai_liq.get("long_liquidation_clusters", []))
            _ns = len(_ai_liq.get("short_liquidation_clusters", []))
            _src = _ai_liq.get("data_source_hint", "AI")
            st.markdown(
                f'<div style="font-size:.73rem;color:#484f58;text-align:right;'
                f'margin-top:-.4rem;margin-bottom:.4rem;">'
                f'📊 AI Liq clusters on chart: '
                f'<span style="color:#f85149">{_nl} Long</span> · '
                f'<span style="color:#3fb950">{_ns} Short</span> '
                f'<span style="color:#484f58">(nguồn: {_src})</span> · '
                f'Bấm <b style="color:#58a6ff;">🔄 UPDATE</b> để cập nhật sau khi upload ảnh mới'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<div style="font-size:.73rem;color:#484f58;text-align:right;'
                'margin-top:-.4rem;margin-bottom:.4rem;">'
                '💡 Upload ảnh liquidation → AI phân tích → bấm '
                '<b style="color:#58a6ff;">🔄 UPDATE</b> để vẽ clusters lên chart'
                '</div>',
                unsafe_allow_html=True,
            )

        # Options wall card + mini strike chart (chỉ trong 15m)
        if st.session_state.timeframe == "15m" and current_price > 0:
            render_options_wall_card(daily_walls, current_price)
            strike_fig = make_daily_strike_chart(daily_walls, current_price)
            if strike_fig:
                st.plotly_chart(strike_fig, width="stretch", config={"displayModeBar": False})

        # ── Liquidation Image AI Analyzer (inline, phía dưới chart) ──────────
        st.markdown("<br>", unsafe_allow_html=True)
        _render_liq_image_inline(current_price=current_price)

        # ── Text Strategy Widget (ngay bên dưới, cùng khu vực chart giá) ────
        try:
            render_text_strategy_widget(
                current_price=current_price,
                daily_walls=daily_walls,
                weekly_max_pain=weekly_max_pain,
                oi_total_usd=oi_total_usd_val,
            )
        except Exception as _tsw_err:
            st.error(f"Text strategy widget error: {_tsw_err}")

        # ── MM Weekly Tactics Widget — 4 chiến thuật MM đến thứ 6 expire ─────
        try:
            render_mm_weekly_tactics_widget(
                current_price    = current_price,
                weekly_max_pain  = weekly_max_pain,
                monthly_max_pain = monthly_max_pain,
                daily_walls      = daily_walls,
                funding_rate_pct = current_fr,
            )
        except Exception as _mmtac_err:
            st.error(f"MM Tactics widget error: {_mmtac_err}")

        # ── CounterFlow Net Long/Short — ngay dưới MM Weekly Tactics ─────────
        try:
            render_counterflow_widget(current_price=current_price)
        except Exception as _cf_err:
            st.error(f"CounterFlow widget error: {_cf_err}")

    else:
        st.warning("Price data unavailable.")

    # ── CounterFlow đã được chuyển vào trong block price_df ─────────────────

    # ── 15m Weekly Entry Plan card (nâng cấp) ───────────────────────────────────────
    if st.session_state.timeframe == "15m" and scenarios and current_price > 0:
        wmp_target   = weekly_max_pain or max_pain or current_price
        _now         = datetime.now(timezone.utc).replace(tzinfo=None)
        today_name   = _now.strftime("%A")
        days_to_fri  = (4 - _now.weekday()) % 7
        if days_to_fri == 0: days_to_fri = 7
        fri_date     = (_now + timedelta(days=days_to_fri)).strftime("%d/%m")
        best_key     = max(scenarios, key=lambda k: scenarios[k]["prob"] if k != "D" else 0)
        best_sc      = scenarios[best_key]

        if best_key == "A":
            e1_dir, e1_entry = "SHORT", short_cluster
            e1_sl, e1_tp = short_cluster * 1.015, wmp_target
            e1_note, e1_timing = "Chờ reject tại Short Cluster", "Mon-Wed"
            e2_dir, e2_entry = "LONG", long_cluster
            e2_sl, e2_tp = long_cluster * 0.985, wmp_target
            e2_note, e2_timing = "Sau khi dump bounce Long Cluster", "Wed-Thu"
        elif best_key == "B":
            e1_dir, e1_entry = "LONG", long_cluster
            e1_sl, e1_tp = long_cluster * 0.985, wmp_target
            e1_note, e1_timing = "Chờ wick tại Long Cluster", "Mon-Wed"
            e2_dir, e2_entry = "SHORT", short_cluster
            e2_sl, e2_tp = short_cluster * 1.015, wmp_target
            e2_note, e2_timing = "Sau khi pump reject Short Cluster", "Wed-Thu"
        else:
            e1_dir, e1_entry = "WATCH", wmp_target
            e1_sl, e1_tp = long_cluster * 0.985, short_cluster
            e1_note, e1_timing = "Sideway ve Max Pain", "Ca tuan"
            e2_dir, e2_entry = "READY", wmp_target
            e2_sl, e2_tp = long_cluster * 0.985, short_cluster
            e2_note, e2_timing = "Cho breakout sau expire", "Thu 6"

        dc = {"LONG":"#3fb950","SHORT":"#f85149","WATCH":"#e3b341","READY":"#58a6ff"}
        rr1 = abs(e1_tp - e1_entry) / max(abs(e1_entry - e1_sl), 1)
        rr2 = abs(e2_tp - e2_entry) / max(abs(e2_entry - e2_sl), 1)
        gap1 = abs(current_price - e1_entry) / current_price * 100
        gap2 = abs(current_price - e2_entry) / current_price * 100
        mm_priority = "DOWNSIDE" if (long_cluster and current_price - long_cluster < short_cluster - current_price) else "UPSIDE"
        bias_txt = "Bullish bias" if pc_ratio_val < 0.7 else "Bearish bias"
        fr_col = "#3fb950" if current_fr >= 0 else "#f85149"

        html_card = (
            '<div style="background:#0d1117;border:1.5px solid #3fb950;border-radius:12px;padding:1.1rem 1.4rem;margin-bottom:.7rem;">'
            f'<div style="color:#3fb950;font-weight:700;font-size:0.95rem;margin-bottom:.7rem;border-bottom:1px solid #21262d;padding-bottom:.4rem;">'
            f'&#128203; WEEKLY ENTRY PLAN &middot; {today_name} &rarr; &#128197; Expire Thu 6 {fri_date}'
            f' &nbsp;&middot;&nbsp; <span style="color:#ffd700">Con {days_to_fri} ngay</span>'
            f' &nbsp;&middot;&nbsp; <span style="color:#bc8cff">MP ${wmp_target:,.0f}</span>'
            f' &nbsp;&middot;&nbsp; <span style="color:#8b949e">Best: {best_sc["label"]}</span></div>'

            '<div style="display:grid;grid-template-columns:1fr 1fr;gap:1.2rem;font-size:0.82rem;">'

            f'<div style="background:#161b22;border-radius:8px;padding:.8rem;border-left:3px solid {dc.get(e1_dir,"#fff")}">'
            f'<div style="color:#8b949e;font-size:0.70rem;margin-bottom:.3rem;">&#127919; ENTRY #1 &middot; {e1_timing}</div>'
            f'<div style="color:{dc.get(e1_dir,"#fff")};font-weight:700;font-size:1rem">{e1_dir} @ ${e1_entry:,.0f}</div>'
            f'<div style="color:#8b949e;font-size:0.75rem;margin:.2rem 0">{e1_note}</div>'
            f'<div style="font-size:0.78rem;line-height:1.8">'
            f'<span style="color:#3fb950">TP: ${e1_tp:,.0f}</span>&nbsp;&middot;&nbsp;'
            f'<span style="color:#f85149">SL: ${e1_sl:,.0f}</span><br>'
            f'<span style="color:#ffd700">R:R = 1:{rr1:.1f}</span>&nbsp;&middot;&nbsp;'
            f'<span style="color:#8b949e">Gap: {gap1:.1f}%</span></div></div>'

            f'<div style="background:#161b22;border-radius:8px;padding:.8rem;border-left:3px solid {dc.get(e2_dir,"#fff")}">'
            f'<div style="color:#8b949e;font-size:0.70rem;margin-bottom:.3rem;">&#127919; ENTRY #2 &middot; {e2_timing}</div>'
            f'<div style="color:{dc.get(e2_dir,"#fff")};font-weight:700;font-size:1rem">{e2_dir} @ ${e2_entry:,.0f}</div>'
            f'<div style="color:#8b949e;font-size:0.75rem;margin:.2rem 0">{e2_note}</div>'
            f'<div style="font-size:0.78rem;line-height:1.8">'
            f'<span style="color:#3fb950">TP: ${e2_tp:,.0f}</span>&nbsp;&middot;&nbsp;'
            f'<span style="color:#f85149">SL: ${e2_sl:,.0f}</span><br>'
            f'<span style="color:#ffd700">R:R = 1:{rr2:.1f}</span>&nbsp;&middot;&nbsp;'
            f'<span style="color:#8b949e">Gap: {gap2:.1f}%</span></div></div>'

            '</div>'
            '<div style="margin-top:.8rem;border-top:1px solid #21262d;padding-top:.6rem;font-size:0.75rem;color:#8b949e;display:flex;gap:1.5rem;flex-wrap:wrap;">'
            f'<span>P/C: <b style="color:#e6edf3">{pc_ratio_val:.2f}</b> {bias_txt}</span>'
            f'<span>FR: <b style="color:{fr_col}">{current_fr*100:+.4f}%</b></span>'
            f'<span>MM: <b style="color:#ffd700">{mm_priority}</b></span>'
            '</div></div>'
        )
        st.markdown(html_card, unsafe_allow_html=True)


    # ── Scenario Summary Card ─────────────────────────────────────────────────
    if scenarios and current_price > 0:
        top_key = max(scenarios, key=lambda k: scenarios[k]["prob"])
        top     = scenarios[top_key]
        top_col = top["color"]

        dist_s_pct = (short_cluster - current_price) / current_price * 100
        dist_l_pct = (current_price - long_cluster)  / current_price * 100
        mp_target  = weekly_max_pain or max_pain or current_price

        # Suggested action text (plain, no inline HTML needed)
        if top_key == "A":
            action_txt = (f"Watch for reversal at ${short_cluster:,.0f} "
                          f"(short liq cluster). If price taps here, "
                          f"expect sharp rejection back toward ${mp_target:,.0f}.")
        elif top_key == "B":
            action_txt = (f"Watch for support at ${long_cluster:,.0f} "
                          f"(long liq cluster). If price wicks here, "
                          f"expect a bounce back toward ${mp_target:,.0f}.")
        elif top_key == "D":
            scen_d     = scenarios["D"]
            action_txt = (f"MM Master Plan in play. "
                          f"{scen_d.get('sweep_first', '')} first, "
                          f"then {scen_d.get('sweep_second', '').lower()}, "
                          f"final target ${mp_target:,.0f} Max Pain.")
        else:
            action_txt = (f"Price grinding toward ${mp_target:,.0f} Max Pain. "
                          f"Watch for breakout if ${long_cluster:,.0f} or "
                          f"${short_cluster:,.0f} is tested.")

        # ── Card header
        st.markdown(
            f'<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;'
            f'padding:1.2rem 1.6rem 0.6rem 1.6rem;margin-bottom:0.4rem;">'
            f'<span style="font-size:0.85rem;color:#8b949e;">Most Likely Scenario &nbsp;·&nbsp; '
            f'Clusters recalculated {cluster_calc_ts}</span><br>'
            f'<span style="font-size:1.3rem;font-weight:700;color:{top_col};">'
            f'{top["label"]}</span>'
            f'<span style="font-size:1rem;font-weight:600;color:{top_col};margin-left:.5rem;">'
            f'({top["prob"]*100:.0f}% probability)</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── Two-column body using st.columns
        col_left, col_right = st.columns(2)

        with col_left:
            fr_sign   = "+" if current_fr >= 0 else ""
            pc_bias_w = "bullish" if pc_ratio_val < 0.85 else "bearish"
            fr_bias_w = "bearish pressure" if current_fr >= 0 else "bullish relief"
            dist_near = ("short cluster closer — pump is path of least resistance"
                         if dist_s_pct < dist_l_pct
                         else "long cluster closer — dump is path of least resistance")

            st.markdown(
                f'<div style="background:#161b22;border:1px solid #30363d;'
                f'border-radius:0 0 0 10px;padding:1rem 1.2rem;">'
                f'<div style="font-size:0.72rem;color:#8b949e;text-transform:uppercase;'
                f'letter-spacing:.08em;margin-bottom:.5rem;">Reasoning</div>'
                f'<div style="font-size:0.82rem;color:#c9d1d9;line-height:1.75;">'
                f'&#9679; P/C ratio <b>{pc_ratio_val:.2f}</b> — more '
                f'{"calls" if pc_ratio_val < 0.85 else "puts"} outstanding, '
                f'mild <b>{pc_bias_w}</b> bias<br>'
                f'&#9679; Funding <b>{fr_sign}{current_fr:.4f}%</b> — longs '
                f'{"paying" if current_fr >= 0 else "receiving"}, '
                f'slight <b>{fr_bias_w}</b><br>'
                f'&#9679; Short cluster {dist_s_pct:.1f}% away, '
                f'long cluster {dist_l_pct:.1f}% away — {dist_near}'
                f'</div>'
                f'<div style="font-size:0.72rem;color:#8b949e;text-transform:uppercase;'
                f'letter-spacing:.08em;margin:.75rem 0 .35rem;">Suggested Action</div>'
                f'<div style="font-size:0.84rem;color:#c9d1d9;line-height:1.55;'
                f'background:#0e1117;border-left:3px solid {top_col};'
                f'padding:.5rem .75rem;border-radius:0 4px 4px 0;">'
                f'{action_txt}'
                f'</div></div>',
                unsafe_allow_html=True,
            )

        with col_right:
            wmp_row = (
                f'<tr><td>🟣</td>'
                f'<td style="color:#c9d1d9;padding:3px 6px;">Max Pain Friday</td>'
                f'<td style="text-align:right;color:#bc8cff;font-weight:600;">'
                f'${weekly_max_pain:,.0f}</td>'
                f'<td style="color:#8b949e;font-size:0.78rem;padding-left:8px;">'
                f'weekly gravity</td></tr>'
            ) if weekly_max_pain else ""

            mp_row = (
                f'<tr><td>🟣</td>'
                f'<td style="color:#c9d1d9;padding:3px 6px;">Max Pain (all)</td>'
                f'<td style="text-align:right;color:#a371f7;font-weight:600;">'
                f'${max_pain:,.0f}</td>'
                f'<td style="color:#8b949e;font-size:0.78rem;padding-left:8px;">'
                f'aggregate gravity</td></tr>'
            ) if max_pain else ""

            st.markdown(
                f'<div style="background:#161b22;border:1px solid #30363d;'
                f'border-radius:0 0 10px 0;padding:1rem 1.2rem;">'
                f'<div style="font-size:0.72rem;color:#8b949e;text-transform:uppercase;'
                f'letter-spacing:.08em;margin-bottom:.5rem;">'
                f'Key Price Levels &nbsp;·&nbsp; '
                f'<span style="color:#484f58;text-transform:none;font-size:0.70rem;">'
                f'live from Binance OI · {cluster_calc_ts}</span></div>'
                f'<table style="width:100%;border-collapse:collapse;font-size:0.82rem;">'
                f'<tr><td>🔴</td>'
                f'<td style="color:#c9d1d9;padding:3px 6px;">Long liq cluster</td>'
                f'<td style="text-align:right;color:#f85149;font-weight:600;">'
                f'${long_cluster:,.0f}</td>'
                f'<td style="color:#8b949e;font-size:0.78rem;padding-left:8px;">'
                f'support / liq magnet</td></tr>'
                f'<tr><td>🟢</td>'
                f'<td style="color:#c9d1d9;padding:3px 6px;">Short liq cluster</td>'
                f'<td style="text-align:right;color:#3fb950;font-weight:600;">'
                f'${short_cluster:,.0f}</td>'
                f'<td style="color:#8b949e;font-size:0.78rem;padding-left:8px;">'
                f'resistance / liq magnet</td></tr>'
                f'{wmp_row}{mp_row}'
                f'<tr><td>🟠</td>'
                f'<td style="color:#c9d1d9;padding:3px 6px;">Current price</td>'
                f'<td style="text-align:right;color:#f7931a;font-weight:600;">'
                f'${current_price:,.2f}</td>'
                f'<td style="color:#8b949e;font-size:0.78rem;padding-left:8px;">'
                f'live</td></tr>'
                f'</table>'
                f'<div style="margin-top:.6rem;font-size:0.70rem;color:#484f58;'
                f'font-style:italic;">Not financial advice. Model estimates only.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ── Scenario D callout (chỉ show ở 1H/4H/1D, không show 15m)
        if "D" in scenarios and st.session_state.timeframe != "15m":
            scen_d   = scenarios["D"]
            d_prob   = scen_d["prob"] * 100
            d_sweep  = scen_d.get("sweep", "")
            st.markdown(
                f'<div style="background:#1a1600;border:1px solid #ffd700;'
                f'border-radius:8px;padding:.75rem 1.2rem;margin-top:.4rem;">'
                f'<span style="font-size:0.72rem;color:#8b949e;text-transform:uppercase;'
                f'letter-spacing:.08em;">D: MM Master Plan (Optimal Path)</span>'
                f'<span style="font-size:0.82rem;font-weight:700;color:#ffd700;'
                f'margin-left:.6rem;">{d_prob:.0f}% probability</span>'
                f'<div style="font-size:0.84rem;color:#e3c96e;margin-top:.3rem;'
                f'line-height:1.6;">{d_sweep}</div>'
                f'<div style="font-size:0.76rem;color:#8b949e;margin-top:.25rem;">'
                f'MM sweeps both liquidation clusters to maximise extraction before '
                f'settling price at Max Pain where options writers retain most premium.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── Pre-compute cluster summary values (used across tabs) ────────────────
    _multi_liq_for_cluster = get_persisted_liq()
    _exchange_cluster_note = ""
    if not _multi_liq_for_cluster.empty:
        _n_ex = _multi_liq_for_cluster["exchange"].nunique()
        _n_ev = len(_multi_liq_for_cluster)
        _exchange_cluster_note = f" · +{_n_ev:,} real liqs ({_n_ex} sàn)"

    biggest_long_lvl  = current_price * 0.92
    biggest_short_lvl = current_price * 1.08
    if not cluster_df.empty:
        biggest_long_lvl  = cluster_df.loc[cluster_df["long_liq_usd"].idxmax(),  "price_level"]
        biggest_short_lvl = cluster_df.loc[cluster_df["short_liq_usd"].idxmax(), "price_level"]

    # oi_total_usd_val đã được định nghĩa sớm ở trên (sau khi fetch oi_df)

    # ── Snake AR — dùng cluster_df nếu có, fallback về long/short cluster ──────
    # Nếu cluster_df rỗng (OI data chưa load xong), tạo cluster tối thiểu từ
    # long_cluster và short_cluster đã tính được để Snake AR không bị "Insufficient data"
    _snake_cluster_df = cluster_df.copy() if not cluster_df.empty else pd.DataFrame()

    if _snake_cluster_df.empty and long_cluster > 0 and short_cluster > 0:
        # Tạo 2 cluster tối thiểu — ước tính liq size từ OI
        _oi_est = oi_total_usd_val if oi_total_usd_val > 0 else float(current_price) * 5000
        _snake_cluster_df = pd.DataFrame([
            {
                "price_level":  long_cluster,
                "long_liq_usd": _oi_est * 0.025,   # ước 2.5% OI
                "short_liq_usd": 0.0,
            },
            {
                "price_level":  short_cluster,
                "long_liq_usd": 0.0,
                "short_liq_usd": _oi_est * 0.025,
            },
        ])

    snake_df_result = calculate_snake_ar(
        current_price    = current_price,
        clusters_df      = _snake_cluster_df,
        max_pain         = max_pain,
        weekly_max_pain  = weekly_max_pain,
        funding_rate_pct = current_fr * 100,
        pc_ratio         = pc_ratio_val,
        oi_total_usd     = oi_total_usd_val,
    )

    sc_costs = calculate_scenario_costs(
        scenarios        = scenarios,
        snake_df         = snake_df_result,
        current_price    = current_price,
        oi_total_usd     = oi_total_usd_val,
        funding_rate_pct = current_fr * 100,
    )

    liq_df = get_liq_df()

    # ── Dashboard Tabs ────────────────────────────────────────────────────────
    tab_snake, tab_options, tab_liq, tab_img_ai, tab_liq_bar = st.tabs([
        "🐍 Snake AR & Max Pain",
        "🎯 Options & OI",
        "⚡ Liquidations",
        "🖼 Liq Image AI",
        "📊 Liq Bar Chart",
    ])

    # ═════════════════════════════════════════════════════════════════════════
    # TAB 1: Snake AR & 3-Layer Max Pain
    # ═════════════════════════════════════════════════════════════════════════
    with tab_snake:
        render_three_max_pain_card(
            weekly_mp        = weekly_max_pain,
            monthly_mp       = monthly_max_pain,
            quarterly_mp     = quarterly_max_pain,
            monthly_expiry   = monthly_expiry,
            quarterly_expiry = quarterly_expiry,
            current_price    = current_price,
        )

        st.markdown("<br>", unsafe_allow_html=True)

        render_snake_ar_section(
            snake_df       = snake_df_result,
            scenario_costs = sc_costs,
            current_price  = current_price,
            long_cluster   = biggest_long_lvl,
            short_cluster  = biggest_short_lvl,
            max_pain       = max_pain,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # TAB 2: Options Max Pain + Clusters + OI/Funding
    # ═════════════════════════════════════════════════════════════════════════
    with tab_options:
        # ── OI + Funding ─────────────────────────────────────────────────────
        with st.expander("📊 Open Interest & Funding Rate", expanded=False):
            col_oi, col_fr = st.columns(2)
            with col_oi:
                if not oi_df.empty:
                    st.plotly_chart(make_oi_chart(oi_df), width="stretch",
                                    config={"displayModeBar": False})
                else:
                    st.warning("Open Interest data unavailable.")
            with col_fr:
                if not funding_df.empty:
                    st.plotly_chart(make_funding_chart(funding_df), width="stretch",
                                    config={"displayModeBar": False})
                else:
                    st.warning("Funding Rate data unavailable.")

        # ── Max Pain ─────────────────────────────────────────────────────────
        st.markdown('<div class="section-title">🎯 Options Max Pain  (Deribit · refreshes every 15 min)</div>',
                    unsafe_allow_html=True)

        if max_pain is None or opts_df.empty:
            st.info("Could not load options data from Deribit. Will retry on next refresh.")
        else:
            dist_pct = ((current_price - max_pain) / max_pain * 100) if max_pain else 0
            above_below = "above" if dist_pct >= 0 else "below"
            total_call_oi = opts_df[opts_df["type"] == "C"]["oi"].sum()
            total_put_oi  = opts_df[opts_df["type"] == "P"]["oi"].sum()
            pc_ratio = total_put_oi / total_call_oi if total_call_oi else 0
            num_strikes = opts_df["strike"].nunique()

            mp1, mp2, mp3, mp4 = st.columns(4)
            with mp1:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Max Pain Price</div>
                    <div class="metric-value" style="color:#a371f7;font-size:1.6rem">${max_pain:,.0f}</div>
                    <div class="metric-sub" style="color:#8b949e">options expire worthless here</div>
                </div>""", unsafe_allow_html=True)
            with mp2:
                color = "red" if dist_pct >= 0 else "green"
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Distance to Max Pain</div>
                    <div class="metric-value {color}" style="font-size:1.6rem">{abs(dist_pct):.1f}%</div>
                    <div class="metric-sub" style="color:#8b949e">price is {above_below} max pain</div>
                </div>""", unsafe_allow_html=True)
            with mp3:
                pc_color = "red" if pc_ratio > 1 else "green"
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Put / Call Ratio</div>
                    <div class="metric-value {pc_color}" style="font-size:1.6rem">{pc_ratio:.2f}</div>
                    <div class="metric-sub" style="color:#8b949e">&gt;1 bearish · &lt;1 bullish</div>
                </div>""", unsafe_allow_html=True)
            with mp4:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Active Strikes</div>
                    <div class="metric-value" style="font-size:1.6rem">{num_strikes:,}</div>
                    <div class="metric-sub" style="color:#8b949e">{len(opts_df):,} contracts total</div>
                </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            mp_fig = make_max_pain_chart(opts_df, max_pain, current_price)
            if mp_fig:
                st.plotly_chart(mp_fig, width="stretch", config={"displayModeBar": False})

        # ── Liquidation Cluster Heatmap ───────────────────────────────────────
        _ai_overlay = st.session_state.get("_liq_ai_analysis")
        _has_ai = (
            _ai_overlay and isinstance(_ai_overlay, dict)
            and "error" not in _ai_overlay
            and (_ai_overlay.get("long_liquidation_clusters")
                 or _ai_overlay.get("short_liquidation_clusters"))
        )
        _ai_badge = (
            ' <span style="background:#0d1f1a;color:#4ade80;font-size:.62rem;'
            'padding:2px 8px;border-radius:10px;font-weight:600;vertical-align:middle;">'
            '🤖 AI Overlay ON</span>' if _has_ai else ""
        )
        st.markdown(
            f'<div class="section-title">🔥 Estimated Liquidation Clusters'
            f'  (7-day OI · leverage distribution 5x–100x · refreshes every 5 min'
            f'{_exchange_cluster_note}){_ai_badge}</div>',
            unsafe_allow_html=True,
        )

        if current_price > 0:
            if cluster_df.empty and not _has_ai:
                st.info("Building cluster data — will appear once 1h OI history is loaded.")
            else:
                if not cluster_df.empty:
                    total_long_risk  = cluster_df["long_liq_usd"].sum()
                    total_short_risk = cluster_df["short_liq_usd"].sum()
                    biggest_long_val  = cluster_df["long_liq_usd"].max()
                    biggest_short_val = cluster_df["short_liq_usd"].max()

                    cc1, cc2, cc3, cc4 = st.columns(4)
                    with cc1:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Est. Long Liq Risk (±25%)</div>
                            <div class="metric-value red" style="font-size:1.5rem">${total_long_risk/1e9:.2f}B</div>
                            <div class="metric-sub" style="color:#8b949e">below current price</div>
                        </div>""", unsafe_allow_html=True)
                    with cc2:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Est. Short Liq Risk (±25%)</div>
                            <div class="metric-value green" style="font-size:1.5rem">${total_short_risk/1e9:.2f}B</div>
                            <div class="metric-sub" style="color:#8b949e">above current price</div>
                        </div>""", unsafe_allow_html=True)
                    with cc3:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Biggest Long Cluster</div>
                            <div class="metric-value red" style="font-size:1.5rem">${biggest_long_lvl:,.0f}</div>
                            <div class="metric-sub" style="color:#8b949e">${biggest_long_val/1e6:.1f}M est.</div>
                        </div>""", unsafe_allow_html=True)
                    with cc4:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Biggest Short Cluster</div>
                            <div class="metric-value green" style="font-size:1.5rem">${biggest_short_lvl:,.0f}</div>
                            <div class="metric-sub" style="color:#8b949e">${biggest_short_val/1e6:.1f}M est.</div>
                        </div>""", unsafe_allow_html=True)

                # AI summary bar khi có overlay
                if _has_ai:
                    _a = _ai_overlay
                    _dom = _a.get("dominant_side", "?").upper()
                    _dc  = {"LONG": "#f87171", "SHORT": "#4ade80", "BALANCED": "#fbbf24"}.get(_dom, "#8b949e")
                    st.markdown(
                        f'<div style="background:#0d1f12;border:1px solid #27ae6044;'
                        f'border-radius:8px;padding:9px 14px;margin:8px 0;'
                        f'display:flex;gap:20px;flex-wrap:wrap;font-size:.77rem;">'
                        f'<span style="color:#4ade80;font-weight:600;">🤖 AI Image: '
                        f'{_a.get("data_source_hint","?")}</span>'
                        f'<span style="color:#8b949e;">Long: <b style="color:#f87171">'
                        f'${_a.get("total_long_liq_usd_millions",0):.0f}M</b></span>'
                        f'<span style="color:#8b949e;">Short: <b style="color:#4ade80">'
                        f'${_a.get("total_short_liq_usd_millions",0):.0f}M</b></span>'
                        f'<span style="color:#8b949e;">Dominant: <b style="color:{_dc}">{_dom}</b></span>'
                        f'<span style="color:#8b949e;">Conf: <b style="color:#fbbf24">'
                        f'{_a.get("analysis_confidence","?").upper()}</b></span>'
                        f'<span style="color:#6e7681;font-size:.7rem;">'
                        f'← Upload ảnh mới ở tab 🖼 Liq Image AI</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                st.markdown("<br>", unsafe_allow_html=True)
                cl_fig = make_liq_cluster_chart(
                    cluster_df, current_price,
                    ai_analysis=_ai_overlay if _has_ai else None,
                )
                if cl_fig:
                    st.plotly_chart(cl_fig, width="stretch", config={"displayModeBar": False})

                _cap = ("⚠️ Estimates only — based on historical OI changes at each price level "
                        "projected through a weighted leverage distribution (5x–100x). "
                        "Actual liquidations depend on individual position entry prices and margin levels.")
                if _has_ai:
                    _cap += ("  |  🤖 AI markers = clusters từ ảnh liquidation đã upload. "
                             "Kích thước marker = độ lớn cluster (low/medium/high/extreme).")
                st.caption(_cap)

    # ═════════════════════════════════════════════════════════════════════════
    # TAB 3: Liquidations (Real-time + Multi-exchange)
    # ═════════════════════════════════════════════════════════════════════════
    with tab_liq:
        st.markdown('<div class="section-title">⚡ Real-Time Liquidations  (BTC/USDT Perpetual)</div>',
                    unsafe_allow_html=True)

        if liq_df.empty:
            st.info("Waiting for liquidation events from Binance stream… "
                    "Large liquidations are infrequent — data will appear here as they occur.")
        else:
            total_events = len(liq_df)
            total_usd = liq_df["usd_value"].sum()
            long_usd = liq_df[liq_df["side"] == "SELL"]["usd_value"].sum()
            short_usd = liq_df[liq_df["side"] == "BUY"]["usd_value"].sum()
            largest = liq_df["usd_value"].max()

            lc1, lc2, lc3, lc4 = st.columns(4)
            with lc1:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Total Events</div>
                    <div class="metric-value" style="font-size:1.5rem">{total_events}</div>
                    <div class="metric-sub" style="color:#8b949e">since session start</div>
                </div>""", unsafe_allow_html=True)
            with lc2:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Total Liquidated</div>
                    <div class="metric-value" style="font-size:1.5rem">${total_usd/1e6:.2f}M</div>
                    <div class="metric-sub" style="color:#8b949e">USDT</div>
                </div>""", unsafe_allow_html=True)
            with lc3:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Longs / Shorts</div>
                    <div class="metric-value" style="font-size:1.1rem">
                        <span class="red">${long_usd/1e3:.0f}K</span>
                        &nbsp;/&nbsp;
                        <span class="green">${short_usd/1e3:.0f}K</span>
                    </div>
                    <div class="metric-sub" style="color:#8b949e">liquidated</div>
                </div>""", unsafe_allow_html=True)
            with lc4:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Largest Single</div>
                    <div class="metric-value" style="font-size:1.5rem">${largest/1e3:.1f}K</div>
                    <div class="metric-sub" style="color:#8b949e">USDT</div>
                </div>""", unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.plotly_chart(make_liq_heatmap(liq_df), width="stretch",
                            config={"displayModeBar": False})
            st.plotly_chart(make_liq_volume_bar(liq_df), width="stretch",
                            config={"displayModeBar": False})

        # ── Real Liquidation Heatmap — 5 sàn ─────────────────────────────────
        if not _multi_liq_for_cluster.empty and current_price > 0:
            with st.expander("🗺 Real Liquidation Heatmap · 5 Exchanges · click để xem", expanded=True):
                _ml = _multi_liq_for_cluster.copy()
                _lo, _hi = current_price * 0.70, current_price * 1.30
                _ml = _ml[(_ml["price"] >= _lo) & (_ml["price"] <= _hi)]
                if not _ml.empty:
                    _ml["bucket"] = (_ml["price"] // 500) * 500
                    _ex_colors = {"Binance":"#f3ba2f","Bybit":"#ff6b35","OKX":"#00d4aa","Hyperliquid":"#9b59b6","Coinbase":"#0052ff"}
                    fig_rl = go.Figure()
                    for exch, grp in _ml.groupby("exchange"):
                        color = _ex_colors.get(exch, "#8b949e")
                        lg = grp[grp["side"] == "SELL"]
                        sg = grp[grp["side"] == "BUY"]
                        if not lg.empty:
                            lb = lg.groupby("bucket")["usd_value"].sum().reset_index()
                            fig_rl.add_trace(go.Bar(x=-(lb["usd_value"]/1e6), y=lb["bucket"], orientation="h", name=f"{exch} Long Liq", marker_color=color, opacity=0.75))
                        if not sg.empty:
                            sb = sg.groupby("bucket")["usd_value"].sum().reset_index()
                            fig_rl.add_trace(go.Bar(x=sb["usd_value"]/1e6, y=sb["bucket"], orientation="h", name=f"{exch} Short Liq", marker_color=color, opacity=0.40))
                    fig_rl.add_hline(y=current_price, line_color="#f7931a", line_dash="dot", line_width=2, annotation_text=f"  ${current_price:,.0f}", annotation_font_color="#f7931a")
                    fig_rl.update_layout(template="plotly_dark", paper_bgcolor="#0d1117", plot_bgcolor="#0d1117", barmode="overlay", height=420,
                        title="Real Liquidation by Price Level (5 sàn) · ← Long Liq | Short Liq → · USD millions",
                        xaxis=dict(title="USD Millions", gridcolor="#21262d"),
                        yaxis=dict(title="Price ($)", gridcolor="#21262d", tickprefix="$", tickformat=",.0f"),
                        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=9), orientation="h", y=-0.15),
                        margin=dict(l=0, r=0, t=40, b=0))
                    st.plotly_chart(fig_rl, use_container_width=True, config={"displayModeBar": False})
                    oldest_liq = _multi_liq_for_cluster["time"].min()
                    st.caption(f"📦 {len(_multi_liq_for_cluster):,} events tích luỹ từ {oldest_liq.strftime('%H:%M %d/%m')}")
                else:
                    st.info("Chưa có real liquidation trong vùng giá ±30%. Đang thu thập…")

        # Raw log — chỉ show nếu có data
        if not liq_df.empty:
            _n_events = len(liq_df)
            with st.expander(f"Raw liquidation log ({_n_events} events)", expanded=False):
                display = liq_df[["time", "side", "price", "qty", "usd_value"]].copy()
                display["side"] = display["side"].map({"SELL": "🔴 Long", "BUY": "🔵 Short"})
                display["price"] = display["price"].map("${:,.2f}".format)
                display["usd_value"] = display["usd_value"].map("${:,.0f}".format)
                display = display.sort_values("time", ascending=False).head(100)
                display.columns = ["Time (UTC)", "Type", "Price", "Qty (BTC)", "USD Value"]
                st.dataframe(display, hide_index=True)

    # ═════════════════════════════════════════════════════════════════════════
    # TAB 4: Liq Image AI — Gemini + Claude
    # ═════════════════════════════════════════════════════════════════════════
    with tab_img_ai:
        render_liq_image_tab(
            current_price=current_price,
            estimated_clusters=None,
        )

    # ═════════════════════════════════════════════════════════════════════════
    # TAB 5: Liq Bar Chart — Upload PNG → AI đọc số liệu → vẽ bar chart
    # ═════════════════════════════════════════════════════════════════════════
    with tab_liq_bar:
        render_liq_bar_chart_tab(current_price=current_price)

    # ── Footer ───────────────────────────────────────────────────────────────
    st.markdown("---")

    st.markdown(
        '<div style="text-align:center;color:#8b949e;font-size:0.75rem;">'
        'Data sourced from Binance public API &amp; Deribit · '
        'Liq Image AI powered by Google Gemini FREE / Claude'
        '</div>',
        unsafe_allow_html=True,
    )

    # ── Auto-refresh: bỏ qua nếu đang có AI analysis ─────────────────────────
    _has_ai_work = bool(
        st.session_state.get("_liq_ai_analysis") or
        st.session_state.get("_liq_ai_strategy")
    )
    if not _has_ai_work:
        time.sleep(60)
        st.rerun()
    else:
        time.sleep(300)
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# LIQ BAR CHART TAB — Upload PNG heatmap → AI đọc số liệu → vẽ bar chart
# ═════════════════════════════════════════════════════════════════════════════

_LIQ_BAR_SYSTEM_PROMPT = """Bạn là chuyên gia đọc biểu đồ liquidation BTC từ ảnh.
Ảnh người dùng cung cấp là một liquidation bar chart (dạng cột/bar) từ Hyblock, Coinglass hoặc tương tự.
Nhiệm vụ: Đọc chính xác các số liệu từ ảnh và trả về JSON.

Quy tắc:
1. Đọc trục X (price levels, VD: $74100, $75000, $83100...) 
2. Đọc trục Y (giá trị USD million, VD: 8M, 16M, 32M, 40M, 64M)
3. Mỗi cột bar có thể có nhiều màu sắc xếp chồng (leverage levels khác nhau: xanh dương=low, xanh lá=medium, vàng=high, cam=very high, đỏ=extreme)
4. Xác định đường giá hiện tại (dotted vertical line) nếu có
5. Phân biệt long liquidation (bên trái/dưới giá hiện tại) và short liquidation (bên phải/trên giá hiện tại)

Trả về DUY NHẤT JSON (không có markdown, không có text thêm):
{
  "current_price": 76800,
  "price_range": {"low": 74100, "high": 84100},
  "bars": [
    {
      "price": 74100,
      "total_usd_millions": 28,
      "side": "long",
      "leverage_breakdown": {
        "low": 16,
        "medium": 8,
        "high": 4,
        "very_high": 0,
        "extreme": 0
      }
    }
  ],
  "y_axis_max": 64,
  "source_note": "Hyblock 1 week BTCUSDT"
}

Ghi chú:
- "side": "long" nếu price < current_price (long bị liquidate khi giá giảm xuống đó)
- "side": "short" nếu price > current_price
- Nếu không đọc được leverage breakdown, chỉ cần total_usd_millions, để leverage_breakdown = {}
- Đọc càng nhiều price level càng tốt (tối thiểu 10, tối đa 50)
- Nếu bar rất nhỏ hoặc gần 0, vẫn ghi vào với giá trị nhỏ
"""


def _call_ai_read_liq_bar(img_pil, api_key_claude: str = "", api_key_gemini: str = "") -> dict:
    """Gọi AI (Claude hoặc Gemini) để đọc số liệu từ ảnh liquidation bar chart."""
    import base64, io, json, re

    # Convert PIL to base64
    buf = io.BytesIO()
    img_pil.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    # Try Claude first
    if api_key_claude:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key_claude)
            resp = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                        {"type": "text", "text": _LIQ_BAR_SYSTEM_PROMPT + "\n\nĐọc ảnh này và trả về JSON:"},
                    ]
                }]
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            return json.loads(raw)
        except Exception as e:
            if not api_key_gemini:
                return {"error": f"Claude error: {e}"}

    # Try Gemini
    if api_key_gemini:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key_gemini)
            model = genai.GenerativeModel("gemini-1.5-pro-latest")
            import PIL.Image
            response = model.generate_content([
                _LIQ_BAR_SYSTEM_PROMPT + "\n\nĐọc ảnh này và trả về JSON:",
                img_pil,
            ])
            raw = response.text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            return json.loads(raw)
        except Exception as e:
            return {"error": f"Gemini error: {e}"}

    return {"error": "Cần nhập API key Claude hoặc Gemini để AI đọc ảnh."}


def _make_liq_bar_plotly(data: dict) -> "go.Figure | None":
    """Vẽ bar chart liquidation từ data đã được AI parse."""
    bars = data.get("bars", [])
    if not bars:
        return None

    current_price = data.get("current_price", 0)

    # Sort by price
    bars_sorted = sorted(bars, key=lambda x: x.get("price", 0))

    prices = [b["price"] for b in bars_sorted]
    totals = [b.get("total_usd_millions", 0) for b in bars_sorted]
    sides  = [b.get("side", "long") for b in bars_sorted]

    # Check if we have leverage breakdown
    has_breakdown = any(b.get("leverage_breakdown") for b in bars_sorted)

    fig = go.Figure()

    if has_breakdown:
        # Stacked bars by leverage level
        leverage_levels = ["low", "medium", "high", "very_high", "extreme"]
        colors = {
            "low":       "#3b82f6",   # blue
            "medium":    "#22c55e",   # green
            "high":      "#eab308",   # yellow
            "very_high": "#f97316",   # orange
            "extreme":   "#ef4444",   # red
        }
        labels = {
            "low": "Low Lev", "medium": "Med Lev", "high": "High Lev",
            "very_high": "Very High", "extreme": "Extreme",
        }
        for lev in leverage_levels:
            vals = []
            for b in bars_sorted:
                bd = b.get("leverage_breakdown", {})
                vals.append(bd.get(lev, 0) if bd else 0)
            if any(v > 0 for v in vals):
                fig.add_trace(go.Bar(
                    name=labels[lev],
                    x=[f"${p:,}" for p in prices],
                    y=vals,
                    marker_color=colors[lev],
                    marker_line_width=0,
                ))
        fig.update_layout(barmode="stack")
    else:
        # Simple colored bars: red = long side, green = short side
        bar_colors = []
        for b in bars_sorted:
            side = b.get("side", "long")
            val  = b.get("total_usd_millions", 0)
            # Color intensity based on value
            if side == "long":
                bar_colors.append("#ef4444" if val > 30 else "#f97316" if val > 20 else "#eab308" if val > 10 else "#22c55e")
            else:
                bar_colors.append("#ef4444" if val > 30 else "#f97316" if val > 20 else "#eab308" if val > 10 else "#3b82f6")

        fig.add_trace(go.Bar(
            x=[f"${p:,}" for p in prices],
            y=totals,
            marker_color=bar_colors,
            marker_line_width=0,
            name="Liquidation",
            hovertemplate="Price: %{x}<br>Liq: $%{y:.1f}M<extra></extra>",
        ))

    # Current price vertical line
    if current_price:
        price_label = f"${current_price:,}"
        if price_label in [f"${p:,}" for p in prices]:
            fig.add_vline(
                x=price_label,
                line_dash="dot",
                line_color="#ffffff",
                line_width=1.5,
                annotation_text=f"Current: ${current_price:,}",
                annotation_font_color="#ffffff",
                annotation_font_size=11,
            )

    fig.update_layout(
        title=dict(
            text=f"📊 Liquidation Bar Chart  |  Current: ${current_price:,}  |  {data.get('source_note', '')}",
            font=dict(color="#e6edf3", size=14),
            x=0,
        ),
        plot_bgcolor="#0d1117",
        paper_bgcolor="#0d1117",
        font=dict(color="#c9d1d9", size=11),
        xaxis=dict(
            title="Price Level",
            gridcolor="#21262d",
            tickfont=dict(size=9),
            tickangle=-45,
        ),
        yaxis=dict(
            title="Liquidation (USD Millions)",
            gridcolor="#21262d",
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.01,
            xanchor="left", x=0,
            font=dict(size=10),
        ),
        height=480,
        margin=dict(l=60, r=20, t=80, b=80),
        hoverlabel=dict(bgcolor="#161b22", font_size=12),
    )
    return fig


def render_liq_bar_chart_tab(current_price: float = 0.0):
    """
    Tab 📊 Liq Bar Chart:
    - Upload 1 ảnh PNG liquidation bar chart (dạng từ Hyblock, Coinglass...)
    - AI đọc số liệu từ ảnh
    - Vẽ lại biểu đồ bar chart bằng Plotly
    """
    st.markdown(
        '<div class="section-title">📊 Liquidation Bar Chart — Upload ảnh PNG → AI đọc số liệu → Vẽ lại</div>',
        unsafe_allow_html=True,
    )

    # ── Hướng dẫn ─────────────────────────────────────────────────────────────
    st.markdown("""
    <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin-bottom:16px;font-size:.84rem;color:#c9d1d9;line-height:1.7;">
    <b style="color:#f7931a;">Cách dùng:</b><br>
    ① Chụp màn hình / lưu ảnh PNG biểu đồ liquidation dạng bar chart (từ Hyblock, Coinglass, Velo...)<br>
    ② Upload ảnh bên dưới<br>
    ③ Nhập API key Claude hoặc Gemini → AI sẽ đọc các số liệu từ ảnh<br>
    ④ Biểu đồ Plotly sẽ được vẽ lại từ dữ liệu AI trích xuất<br>
    <b style="color:#8b949e;">Lưu ý:</b> AI đọc số liệu bằng mắt — độ chính xác ~85-95% tuỳ chất lượng ảnh.
    </div>
    """, unsafe_allow_html=True)

    # ── API key inputs ─────────────────────────────────────────────────────────
    col_k1, col_k2 = st.columns(2)
    with col_k1:
        api_claude = st.text_input(
            "🔑 Claude API Key (Anthropic)",
            type="password",
            key="liq_bar_claude_key",
            placeholder="sk-ant-...",
        )
    with col_k2:
        api_gemini = st.text_input(
            "🔑 Gemini API Key (Google)",
            type="password",
            key="liq_bar_gemini_key",
            placeholder="AIza...",
        )

    # ── Image upload ───────────────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "📁 Upload ảnh PNG/JPG liquidation bar chart",
        type=["png", "jpg", "jpeg", "webp"],
        key="liq_bar_upload",
    )

    # ── Manual data entry fallback ─────────────────────────────────────────────
    with st.expander("✏️ Hoặc nhập thủ công dữ liệu (nếu không có API key)", expanded=False):
        st.markdown("""
        <div style="font-size:.8rem;color:#8b949e;margin-bottom:10px;">
        Nhập dữ liệu theo định dạng: <code>price,usd_millions,side</code> — mỗi dòng 1 bar<br>
        Ví dụ:<br>
        <code>74100,28,long</code><br>
        <code>75100,42,long</code><br>
        <code>77100,8,short</code><br>
        <code>83100,41,short</code>
        </div>
        """, unsafe_allow_html=True)
        manual_price = st.number_input(
            "Giá hiện tại (current price)",
            value=int(current_price) if current_price > 0 else 76800,
            step=100,
            key="liq_bar_manual_price",
        )
        manual_text = st.text_area(
            "Dữ liệu bars (price,usd_millions,side)",
            height=200,
            key="liq_bar_manual_text",
            placeholder="74100,28,long\n75100,42,long\n77100,8,short\n83100,41,short",
        )
        if st.button("📊 Vẽ từ dữ liệu thủ công", key="liq_bar_manual_btn"):
            try:
                bars_manual = []
                for line in manual_text.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2:
                        price_m = float(parts[0].replace("$", "").replace(",", ""))
                        usd_m   = float(parts[1])
                        side_m  = parts[2].strip().lower() if len(parts) >= 3 else (
                            "long" if price_m < manual_price else "short"
                        )
                        bars_manual.append({
                            "price": price_m,
                            "total_usd_millions": usd_m,
                            "side": side_m,
                            "leverage_breakdown": {},
                        })
                if bars_manual:
                    data_manual = {
                        "current_price": manual_price,
                        "bars": bars_manual,
                        "source_note": "Manual entry",
                        "price_range": {
                            "low": min(b["price"] for b in bars_manual),
                            "high": max(b["price"] for b in bars_manual),
                        },
                    }
                    st.session_state["liq_bar_data"] = data_manual
                    st.success(f"✅ Đã nhập {len(bars_manual)} bars!")
                else:
                    st.warning("Không parse được dữ liệu. Kiểm tra format.")
            except Exception as ex:
                st.error(f"Lỗi parse: {ex}")

    # ── Run AI analysis ────────────────────────────────────────────────────────
    if uploaded is not None:
        from PIL import Image as _PILImage
        import io as _io

        img_pil = _PILImage.open(_io.BytesIO(uploaded.read()))
        img_pil = img_pil.convert("RGB")

        col_img, col_info = st.columns([2, 1])
        with col_img:
            st.image(img_pil, caption="Ảnh đã upload", use_column_width=True)
        with col_info:
            w, h = img_pil.size
            st.markdown(f"""
            <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;font-size:.82rem;color:#c9d1d9;">
            <b>Thông tin ảnh:</b><br>
            📐 {w} × {h} px<br>
            🎨 Mode: {img_pil.mode}<br><br>
            <b style="color:#f7931a;">Bước tiếp theo:</b><br>
            Nhập API key → nhấn nút bên dưới để AI đọc số liệu.
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        if st.button("🤖 AI Đọc Số Liệu & Vẽ Biểu Đồ", type="primary", key="liq_bar_ai_btn"):
            if not api_claude and not api_gemini:
                st.error("⚠️ Cần nhập ít nhất 1 API key (Claude hoặc Gemini) để AI đọc ảnh.")
            else:
                engine = "Claude" if api_claude else "Gemini"
                with st.spinner(f"🤖 {engine} đang đọc số liệu từ ảnh…"):
                    result = _call_ai_read_liq_bar(img_pil, api_claude, api_gemini)
                if "error" in result:
                    st.error(f"❌ {result['error']}")
                else:
                    st.session_state["liq_bar_data"] = result
                    n_bars = len(result.get("bars", []))
                    cp     = result.get("current_price", 0)
                    st.success(f"✅ AI đọc được {n_bars} price levels | Current price: ${cp:,}")

    # ── Render chart ───────────────────────────────────────────────────────────
    bar_data = st.session_state.get("liq_bar_data")
    if bar_data and "bars" in bar_data and bar_data["bars"]:
        st.markdown("---")
        st.markdown('<div class="section-title">📊 Biểu Đồ Thanh Khoản</div>', unsafe_allow_html=True)

        # Metrics row
        bars    = bar_data.get("bars", [])
        cp_data = bar_data.get("current_price", current_price or 0)
        long_bars  = [b for b in bars if b.get("side") == "long"]
        short_bars = [b for b in bars if b.get("side") == "short"]
        total_long  = sum(b.get("total_usd_millions", 0) for b in long_bars)
        total_short = sum(b.get("total_usd_millions", 0) for b in short_bars)
        biggest_long  = max((b for b in long_bars),  key=lambda x: x.get("total_usd_millions", 0), default={})
        biggest_short = max((b for b in short_bars), key=lambda x: x.get("total_usd_millions", 0), default={})

        m1, m2, m3, m4, m5 = st.columns(5)
        for col, lbl, val, color in [
            (m1, "Price Levels",     f"{len(bars)}",          "#e6edf3"),
            (m2, "Total Long Liq",   f"${total_long:.0f}M",   "#f87171"),
            (m3, "Total Short Liq",  f"${total_short:.0f}M",  "#4ade80"),
            (m4, "Biggest Long @",   f"${biggest_long.get('price', 0):,}"  if biggest_long  else "—", "#f87171"),
            (m5, "Biggest Short @",  f"${biggest_short.get('price', 0):,}" if biggest_short else "—", "#4ade80"),
        ]:
            with col:
                st.markdown(f"""
                <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
                            padding:10px;text-align:center;margin-bottom:10px;">
                  <div style="color:#8b949e;font-size:.65rem;">{lbl}</div>
                  <div style="color:{color};font-weight:700;font-size:.95rem;">{val}</div>
                </div>""", unsafe_allow_html=True)

        # Main chart
        fig = _make_liq_bar_plotly(bar_data)
        if fig:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        # Raw data expander
        with st.expander("🔍 Xem raw data từ AI", expanded=False):
            import json as _json
            st.code(_json.dumps(bar_data, indent=2, ensure_ascii=False), language="json")

        # Clear button
        if st.button("🗑 Xoá & upload ảnh mới", key="liq_bar_clear"):
            st.session_state.pop("liq_bar_data", None)
            st.rerun()
    elif not uploaded:
        # Show example placeholder
        st.markdown("""
        <div style="background:#0d1117;border:2px dashed #30363d;border-radius:12px;
                    padding:40px;text-align:center;color:#8b949e;margin-top:20px;">
          <div style="font-size:2.5rem;margin-bottom:10px;">📊</div>
          <div style="font-size:1rem;font-weight:600;color:#c9d1d9;margin-bottom:6px;">
            Upload ảnh PNG để bắt đầu
          </div>
          <div style="font-size:.82rem;line-height:1.6;">
            Hỗ trợ biểu đồ từ: <b style="color:#f7931a;">Hyblock</b> · 
            <b style="color:#f7931a;">Coinglass</b> · 
            <b style="color:#f7931a;">Velo</b> · 
            <b style="color:#f7931a;">CryptoQuant</b><br>
            AI sẽ đọc từng cột bar và trị số USD millions từ ảnh của bạn.
          </div>
        </div>
        """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
