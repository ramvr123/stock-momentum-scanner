"""
📈 Daily Post-Market Momentum Scanner
=====================================
A Streamlit dashboard that scans NYSE & NASDAQ for momentum stocks meeting
strict price gain, volume surge, and asset-class criteria.

Run: python -m streamlit run app.py
"""

import io
import json
import time
import ftplib
import datetime
import concurrent.futures
import warnings
from pathlib import Path

import pandas as pd
import yfinance as yf
import streamlit as st

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# PASSWORD PROTECTION
# ─────────────────────────────────────────────────────────────────────────────
def check_password() -> bool:
    """Returns True if the user has entered the correct password."""
    if st.session_state.get("authenticated"):
        return True

    st.markdown("## 🔐 Post-Market Momentum Scanner")
    st.markdown(
        "<p style='color:#5b7fa6; font-size:0.9rem;'>Enter your password to access the dashboard.</p>",
        unsafe_allow_html=True,
    )

    password = st.text_input("Password", type="password", placeholder="Enter password …")

    if st.button("Login"):
        if password == st.secrets["APP_PASSWORD"]:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("❌ Incorrect password. Please try again.")

    return False


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
CACHE_FILE             = Path("cached_data.json")
NASDAQ_FTP_HOST        = "ftp.nasdaqtrader.com"
NASDAQ_FTP_PATH        = "/SymbolDirectory/"
PRICE_MIN              = 2.00
PRICE_MAX              = 20.00
MIN_PRICE_GAIN_PCT     = 3.0
MIN_VOLUME_SURGE_RATIO = 1.5
MAX_WORKERS            = 3
FETCH_PERIOD           = "7d"
CARDS_PER_ROW          = 5


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 – TICKER UNIVERSE  (with exchange tagging)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_ftp(filename: str, retries: int = 3, timeout: int = 60) -> str:
    """Download a file from NASDAQ FTP with retry logic on timeout."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            buf = io.BytesIO()
            with ftplib.FTP(NASDAQ_FTP_HOST, timeout=timeout) as ftp:
                ftp.login()
                ftp.retrbinary(f"RETR {NASDAQ_FTP_PATH}{filename}", buf.write)
            return buf.getvalue().decode("utf-8", errors="ignore")
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(3 * attempt)
    raise last_exc


def load_ticker_universe() -> dict[str, str]:
    """
    Returns a dict of {symbol: exchange} for all common stocks on
    NASDAQ and NYSE.
    """
    ticker_map: dict[str, str] = {}

    # ── NASDAQ ───────────────────────────────────────────────────────────────
    try:
        text = _fetch_ftp("nasdaqlisted.txt")
        df = pd.read_csv(io.StringIO(text), sep="|")
        df.columns = df.columns.str.strip()
        if "ETF" in df.columns:
            df = df[df["ETF"].astype(str).str.upper() != "Y"]
        if "Symbol" in df.columns:
            mask = df["Symbol"].astype(str).str.match(r"^[A-Z]{1,5}$")
            for sym in df.loc[mask, "Symbol"]:
                ticker_map[sym] = "NASDAQ"
    except Exception as exc:
        st.warning(f"nasdaqlisted.txt error: {exc}")

    # ── NYSE / Other ─────────────────────────────────────────────────────────
    try:
        text = _fetch_ftp("otherlisted.txt")
        df = pd.read_csv(io.StringIO(text), sep="|")
        df.columns = df.columns.str.strip()
        if "ETF" in df.columns:
            df = df[df["ETF"].astype(str).str.upper() != "Y"]
        sym_col = next((c for c in df.columns if "symbol" in c.lower()), None)
        if sym_col:
            mask = df[sym_col].astype(str).str.match(r"^[A-Z]{1,5}$")
            for sym in df.loc[mask, sym_col]:
                if sym not in ticker_map:          # NASDAQ takes priority
                    ticker_map[sym] = "NYSE"
    except Exception as exc:
        st.warning(f"otherlisted.txt error: {exc}")

    return ticker_map


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 – PER-TICKER SCREENING
# ─────────────────────────────────────────────────────────────────────────────
def screen_ticker(symbol: str, exchange: str) -> dict | None:
    import re
    from yfinance.exceptions import YFRateLimitError

    if not re.fullmatch(r"[A-Z]{1,5}", symbol):
        return None

    for attempt in range(3):                       # up to 3 attempts per ticker
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            quote_type = getattr(info, "quote_type", None)
            if quote_type and str(quote_type).upper() != "EQUITY":
                return None

            hist = ticker.history(period=FETCH_PERIOD, interval="1d", auto_adjust=True)
            if hist is None or len(hist) < 2:
                return None

            hist = hist.dropna(subset=["Close", "Volume"])
            hist = hist[hist["Volume"] > 0]
            if len(hist) < 2:
                return None

            price_today     = float(hist["Close"].iloc[-1])
            price_yesterday = float(hist["Close"].iloc[-2])
            vol_today       = float(hist["Volume"].iloc[-1])
            vol_yesterday   = float(hist["Volume"].iloc[-2])

            if not (PRICE_MIN <= price_today <= PRICE_MAX):
                return None

            price_chg_pct = (price_today - price_yesterday) / price_yesterday * 100
            if price_chg_pct < MIN_PRICE_GAIN_PCT:
                return None

            if vol_today < MIN_VOLUME_SURGE_RATIO * vol_yesterday:
                return None

            return {
                "symbol":           symbol,
                "exchange":         exchange,
                "price_today":      round(price_today, 2),
                "price_chg_dollar": round(price_today - price_yesterday, 2),
                "price_chg_pct":    round(price_chg_pct, 2),
                "vol_today":        int(vol_today),
                "vol_chg_pct":      round((vol_today - vol_yesterday) / vol_yesterday * 100, 1),
            }

        except YFRateLimitError:
            # Yahoo rate limited us — wait before retrying
            wait = 10 * (attempt + 1)              # 10s, 20s, 30s
            time.sleep(wait)
        except Exception:
            return None   # any other error — skip this ticker

    return None            # exhausted all retries


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 – ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────────────
def run_scan(progress_bar, status_text) -> dict:
    status_text.text("🔍 Fetching ticker universe from NASDAQ FTP …")
    ticker_map = load_ticker_universe()
    total      = len(ticker_map)
    status_text.text(f"⚙️  Screening {total:,} tickers …")

    results: list[dict] = []
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(screen_ticker, sym, exch): sym
            for sym, exch in ticker_map.items()
        }
        for future in concurrent.futures.as_completed(futures):
            done += 1
            if done % 50 == 0 or done == total:
                progress_bar.progress(done / total)
                status_text.text(
                    f"⚙️  Scanned {done:,} / {total:,} … "
                    f"({len(results)} qualifying so far)"
                )
            res = future.result()
            if res:
                results.append(res)

    # Sort by price ascending
    results.sort(key=lambda x: x["price_today"])

    payload = {
        "scanned_at":      datetime.datetime.now().isoformat(timespec="seconds"),
        "total_scanned":   total,
        "total_qualified": len(results),
        "results":         results,
    }
    CACHE_FILE.write_text(json.dumps(payload, indent=2))
    return payload


def load_cache() -> dict | None:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_vol(v: int) -> str:
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:,}"


def results_to_df(results: list[dict]) -> pd.DataFrame:
    """Convert results list to a clean display DataFrame."""
    rows = []
    for s in results:
        rows.append({
            "Ticker":           s["symbol"],
            "Exchange":         s["exchange"],
            "Price ($)":        s["price_today"],
            "Price Chg %":      s["price_chg_pct"],
            "Volume":           s["vol_today"],
            "Volume Chg %":     s["vol_chg_pct"],
        })
    return pd.DataFrame(rows)


def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    """Export DataFrame to Excel bytes for download."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Momentum Movers")
        # Auto-size columns
        ws = writer.sheets["Momentum Movers"]
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col) + 4
            ws.column_dimensions[col[0].column_letter].width = min(max_len, 30)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# CARD RENDERING
# ─────────────────────────────────────────────────────────────────────────────
def _card_html(stock: dict) -> str:
    sym      = stock["symbol"]
    price    = stock["price_today"]
    p_dollar = stock["price_chg_dollar"]
    p_pct    = stock["price_chg_pct"]
    vol      = _fmt_vol(stock["vol_today"])
    v_pct    = stock["vol_chg_pct"]

    return f"""
<div style="
    background: #0d1b2e;
    border: 1px solid #1e3a5f;
    border-radius: 14px;
    padding: 14px 10px 12px 10px;
    margin-bottom: 10px;
    font-family: 'Inter', 'Segoe UI', sans-serif;
    text-align: center;
    box-shadow: 0 2px 12px rgba(0,0,0,0.35);
">
  <div style="font-size:1.15rem; font-weight:800; color:#e8f4fd;
              letter-spacing:2px; margin-bottom:10px;">{sym}</div>
  <div style="background:#0a2540; border-radius:8px; padding:6px 8px; margin-bottom:6px;">
    <div style="font-size:0.65rem; color:#5b8db8; text-transform:uppercase;
                letter-spacing:1px; margin-bottom:2px;">Volume</div>
    <div style="font-size:0.85rem; font-weight:700; color:#e8f4fd;">{vol}</div>
    <div style="font-size:0.75rem; font-weight:600; color:#00e5a0;">▲ +{v_pct:.1f}%</div>
  </div>
  <div style="background:#0a2540; border-radius:8px; padding:6px 8px;">
    <div style="font-size:0.65rem; color:#5b8db8; text-transform:uppercase;
                letter-spacing:1px; margin-bottom:2px;">Price</div>
    <div style="font-size:0.85rem; font-weight:700; color:#e8f4fd;">${price:.2f}</div>
    <div style="font-size:0.75rem; font-weight:600; color:#00e5a0;">
      ▲ +{p_dollar:.2f} &nbsp;/&nbsp; +{p_pct:.2f}%
    </div>
  </div>
</div>
"""


def render_grid(stocks: list[dict]) -> None:
    if not stocks:
        st.info("No qualifying stocks found for today's session.")
        return
    for row_start in range(0, len(stocks), CARDS_PER_ROW):
        row_stocks = stocks[row_start: row_start + CARDS_PER_ROW]
        cols = st.columns(CARDS_PER_ROW)
        for col, stock in zip(cols, row_stocks):
            with col:
                st.markdown(_card_html(stock), unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# TABLE RENDERING
# ─────────────────────────────────────────────────────────────────────────────
def render_table(results: list[dict]) -> None:
    if not results:
        return

    df = results_to_df(results)

    # ── Download buttons ─────────────────────────────────────────────────────
    col_csv, col_xlsx, col_spacer = st.columns([1, 1, 6])

    with col_csv:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Download CSV",
            data=csv_bytes,
            file_name=f"momentum_movers_{datetime.date.today()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    with col_xlsx:
        try:
            xlsx_bytes = df_to_excel_bytes(df)
            st.download_button(
                label="⬇️ Download Excel",
                data=xlsx_bytes,
                file_name=f"momentum_movers_{datetime.date.today()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        except Exception:
            st.caption("Install openpyxl for Excel export: `pip install openpyxl`")

    # ── Styled interactive table ──────────────────────────────────────────────
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Ticker": st.column_config.TextColumn("Ticker", width="small"),
            "Exchange": st.column_config.TextColumn("Exchange", width="small"),
            "Price ($)": st.column_config.NumberColumn(
                "Price ($)", format="$%.2f", width="small"
            ),
            "Price Chg %": st.column_config.NumberColumn(
                "Price Chg %", format="+%.2f%%", width="small"
            ),
            "Volume": st.column_config.NumberColumn(
                "Volume", format="%d", width="medium"
            ),
            "Volume Chg %": st.column_config.NumberColumn(
                "Volume Chg %", format="+%.1f%%", width="small"
            ),
        },
        height=min(50 + len(df) * 36, 600),   # cap at 600px, scrollable
    )


# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG & GLOBAL CSS
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Post-Market Momentum Scanner",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Space+Mono:wght@400;700&display=swap');

    html, body, [class*="css"] { background-color: #060e18 !important; color: #c9d8e8; }

    h1 { font-family: 'Syne', sans-serif !important; font-size: 1.7rem !important;
         letter-spacing: 1px !important; margin-bottom: 0 !important; }

    h2 { font-family: 'Syne', sans-serif !important; font-size: 1.0rem !important;
         color: #7ec8e3 !important; border-bottom: 1px solid #1e3a5f;
         padding-bottom: 4px; margin-top: 14px !important; margin-bottom: 8px !important; }

    h3 { font-family: 'Syne', sans-serif !important; font-size: 0.95rem !important;
         color: #7ec8e3 !important; margin-top: 18px !important; margin-bottom: 6px !important; }

    .stButton > button, .stDownloadButton > button {
        background: linear-gradient(90deg, #00e5a0, #00b4d8) !important;
        color: #040d14 !important;
        font-family: 'Space Mono', monospace !important;
        font-weight: 700 !important; font-size: 0.82rem !important;
        border: none !important; border-radius: 8px !important;
        padding: 8px 16px !important; cursor: pointer !important;
        transition: opacity 0.2s;
    }
    .stButton > button:hover, .stDownloadButton > button:hover { opacity: 0.85 !important; }

    [data-testid="column"] { padding-left: 4px !important; padding-right: 4px !important; }

    /* Dataframe dark theme overrides */
    [data-testid="stDataFrame"] { border: 1px solid #1e3a5f; border-radius: 10px; overflow: hidden; }

    #MainMenu, footer, header { visibility: hidden; }
    .block-container {
        padding-top: 0.8rem !important; padding-bottom: 0.5rem !important;
        padding-left: 1.5rem !important; padding-right: 1.5rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    if not check_password():
        st.stop()

    # Logout button in top right corner
    col_logout = st.columns([8, 1])[1]
    with col_logout:
        if st.button("🔒 Logout"):
            st.session_state["authenticated"] = False
            st.rerun()

    st.markdown("## 📈 Daily Post-Market Momentum Scanner")
    st.markdown(
        "<p style='color:#5b7fa6; font-size:0.82rem; margin-top:-6px; margin-bottom:10px;'>"
        "NYSE &amp; NASDAQ common stocks · $2–$20 · ≥3% price gain · ≥50% volume surge vs prior session"
        "</p>",
        unsafe_allow_html=True,
    )

    col_btn, col_meta = st.columns([1, 5])
    with col_btn:
        refresh = st.button("🔄 Refresh & Scan Market")

    data: dict | None = None

    if refresh:
        CACHE_FILE.unlink(missing_ok=True)
        prog   = st.progress(0.0)
        status = st.empty()
        try:
            with st.spinner("Running market scan …"):
                data = run_scan(prog, status)
            prog.empty()
            status.empty()
            st.success("✅ Scan complete — results refreshed.")
        except Exception as exc:
            prog.empty()
            status.empty()
            st.error(f"Scan failed: {exc}")
            return
    else:
        data = load_cache()
        if data is None:
            st.info("No cached data found. Hit **🔄 Refresh & Scan Market** to run the scanner.")
            return

    if data is None:
        return

    with col_meta:
        st.markdown(
            f"<p style='color:#5b7fa6; font-size:0.82rem; margin-top:10px;'>"
            f"Last scan: <b style='color:#7ec8e3'>{data.get('scanned_at','—')}</b>"
            f" &nbsp;|&nbsp; Scanned: <b style='color:#7ec8e3'>{data.get('total_scanned',0):,}</b>"
            f" &nbsp;|&nbsp; Qualified: <b style='color:#00e5a0'>{data.get('total_qualified',0)}</b>"
            f"</p>",
            unsafe_allow_html=True,
        )

    results: list[dict] = data.get("results", [])

    # ── Cards ─────────────────────────────────────────────────────────────────
    st.markdown("## 🔥 Today's Momentum Movers")
    render_grid(results)

    # ── Table + Downloads ─────────────────────────────────────────────────────
    st.markdown("## 📋 Full Results Table")
    render_table(results)

    st.markdown(
        "<p style='color:#1e3a5f; font-size:0.72rem; text-align:center; margin-top:16px;'>"
        "Data: Yahoo Finance · Universe: NASDAQ Trader FTP · Sorted by price ascending · Not financial advice."
        "</p>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()


# =============================================================================
# SETUP & RUN INSTRUCTIONS
# =============================================================================
#
# 1. Install dependencies (one time only):
#       pip install streamlit yfinance pandas openpyxl
#
# 2. Launch:
#       python -m streamlit run app.py
#
# 3. Click "Refresh & Scan Market" — takes 5–15 min for a full scan.
#    Results saved to cached_data.json; reloads instantly on page refresh.
#
# =============================================================================
