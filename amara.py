"""
Amara — Serverless Single-Run Trading Bot
==========================================
策略：大型股＋中型股動能交易，技術面分析
模式：紙上交易（不使用真實資金）
執行：單次執行（適合 Claude Code routines / cron / 任何排程器）

需要安裝：
    pip install alpaca-py anthropic pandas requests python-dotenv pytz

需要設定：
    把您的 API keys 填入同一資料夾的 secrets.env 檔案（不要填在這裡）

Serverless changes vs. original:
  - Removed: while True loop, time.sleep(), HTTP dashboard server,
              ngrok/Cloudflare tunnels, _bot_control state machine.
  - Added:    read_previous_dashboard() — loads amara_dashboard.md for context
              write_amara_dashboard()   — writes a fresh Markdown report each run
              _run_decisions tracking   — collects every buy/sell/skip this run
  - Preserved EXACTLY: all technical indicators, Alpaca/Benzinga/Claude API calls,
              position management, LINE notifications, stop-loss/take-profit logic.
"""

import os
import json
import time
import logging

# Absolute path to the directory containing this script file.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Dashboard is written to Google Drive so the household can view it via a
# shared static link without exposing secrets.env or the script directory.
# The local Google Drive sync client keeps this path always up-to-date.
DASHBOARD_PATH = os.path.join(_SCRIPT_DIR, "amara_dashboard.md")

from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# Load secrets from secrets.env if present (local dev only).
# Claude Code routines inject API keys as environment variables directly —
# secrets.env is not committed to the repo, so this is skipped in remote runs.
_dotenv_path = os.path.join(_SCRIPT_DIR, "secrets.env")
if os.path.exists(_dotenv_path):
    load_dotenv(_dotenv_path)

# ─────────────────────────────────────────────
# CONFIG — non-sensitive settings here; API keys in secrets.env
# ─────────────────────────────────────────────
CONFIG = {
    # Alpaca Paper Trading — read from secrets.env
    "ALPACA_API_KEY":    os.getenv("ALPACA_API_KEY"),
    "ALPACA_SECRET_KEY": os.getenv("ALPACA_SECRET_KEY"),
    "ALPACA_BASE_URL":   "https://paper-api.alpaca.markets",

    # Anthropic Claude — read from secrets.env
    "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),

    # Portfolio limits
    "TOTAL_CAPITAL": 100000,
    "MAX_POSITION_PCT": 0.10,       # used to calculate max simultaneous positions (10 slots)
    "MAX_HOLD_DAYS": 8,
    "DAILY_LOSS_LIMIT_PCT": 0.05,
    "TOTAL_LOSS_LIMIT_PCT": 0.30,

    # ── Dual-tier risk profiles ──────────────────────────────────────────
    # Large-caps: tighter stop, conservative target
    "LARGE_CAP_STOP_LOSS_PCT":    0.035,  # Hard stop-loss    -3.5%
    "LARGE_CAP_TAKE_PROFIT_PCT":  0.070,  # Fixed take profit  +7.0%
    "LARGE_CAP_POSITION_PCT":     0.10,   # 10% of capital per position

    # Mid-caps: wider stop, higher target — accommodates larger swings
    "MID_CAP_STOP_LOSS_PCT":      0.050,  # Hard stop-loss    -5.0%
    "MID_CAP_TAKE_PROFIT_PCT":    0.100,  # Fixed take profit +10.0%
    "MID_CAP_POSITION_PCT":       0.06,   # 6% of capital per position

    # ── 4-criterion scoring thresholds ──────────────────────────────────
    "RSI_BUY_THRESHOLD":    55,     # RSI must be above this (momentum building)
    "RSI_OVERBOUGHT":       75,     # RSI must be below this (not extended)
    "VOLUME_SURGE_FACTOR":  1.5,    # Volume must be >= 1.5× 20-day average

    # Scanning
    "TOP_CANDIDATES": 5,           # only top-N momentum stocks go to Claude
    "LOG_FILE": os.path.join(_SCRIPT_DIR, "amara.log"),
    "TRADES_FILE": os.path.join(_SCRIPT_DIR, "amara_trades.json"),

    # LINE Messaging API — read from secrets.env
    "LINE_CHANNEL_ACCESS_TOKEN": os.getenv("LINE_CHANNEL_ACCESS_TOKEN"),
    "LINE_USER_IDS": [
        uid.strip()
        for uid in os.getenv("LINE_USER_IDS", "").split(",")
        if uid.strip() and not uid.strip().startswith("#")
    ],

    # ── Live trading switch ──────────────────────────────────────────────
    # Set to False only when ready for real money.
    "PAPER_TRADING": True,
}

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"], encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("Amara")

# ─────────────────────────────────────────────
# LINE Messaging helper
# ─────────────────────────────────────────────
# Uses a standard HTTPS connection to api.line.me with no manual DNS or
# socket overrides (those caused SSLEOFError handshake drops on LINE's
# modern gateway).  If HTTP_PROXY / HTTPS_PROXY are defined in secrets.env
# they are picked up automatically by requests — useful for VPN or
# corporate proxy environments without any code changes.
# ─────────────────────────────────────────────
def send_line_message(message: str):
    token    = CONFIG.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_ids = CONFIG.get("LINE_USER_IDS", [])
    if not token or not user_ids:
        return

    import requests

    # Build a session that inherits proxy settings from the environment.
    # dotenv already loaded secrets.env, so HTTP_PROXY / HTTPS_PROXY set
    # there are already present in os.environ at this point.
    session = requests.Session()
    http_proxy  = os.getenv("HTTP_PROXY", "")
    https_proxy = os.getenv("HTTPS_PROXY", "")
    if http_proxy or https_proxy:
        session.proxies.update({
            "http":  http_proxy  or None,
            "https": https_proxy or None,
        })
        log.info(f"📡 LINE: routing via proxy ({https_proxy or http_proxy})")
    else:
        log.info("📡 LINE: using default system network (no proxy configured)")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    try:
        for uid in user_ids:
            if not uid or uid.startswith("#"):
                continue
            payload = {
                "to":       uid,
                "messages": [{"type": "text", "text": message}],
            }
            r = session.post(
                "https://api.line.me/v2/bot/message/push",
                headers=headers,
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                log.info(f"📱 LINE message sent to {uid[:8]}...")
            else:
                log.warning(f"⚠️ LINE send failed ({r.status_code}): {r.text}")
    except Exception as e:
        log.warning(f"⚠️ LINE message failed: {e}")
    finally:
        session.close()

# ─────────────────────────────────────────────
# Dual-tier universe — 50 Large-Caps + 100 Mid-Caps
# ─────────────────────────────────────────────

# ── Tier 1: Large-Caps (50) — liquid, lower-volatility blue-chips ─────────
LARGE_CAPS: set = {
    # Mega-cap Tech & Semiconductors (15)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",
    "AVGO", "ORCL", "CRM", "ADBE", "AMD", "QCOM", "INTC", "PLTR",
    # Financials (10)
    "JPM", "V", "MA", "BAC", "GS", "MS", "BLK", "AXP", "WFC", "C",
    # Healthcare (9)
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "PFE", "AMGN",
    # Consumer (9)
    "PG", "HD", "MCD", "SBUX", "NKE", "COST", "TGT", "WMT", "LOW",
    # Industrials & Energy (7)
    "CAT", "HON", "BA", "XOM", "CVX", "NEE", "DE",
}  # total: 50

# ── Tier 2: Mid-Caps (100) — high-velocity growth companies ──────────────
MID_CAPS: set = {
    # Consumer & Retail (15)
    "ANF", "AEO", "BOOT", "CAVA", "ELF", "BBWI", "SFM", "FIVE",
    "YETI", "WING", "MODG", "GSHD", "DXPE", "FCFS", "PRGO",
    # Technology & Software (18)
    "GTLB", "DOCN", "BILL", "TOST", "ZI", "TASK",
    "EXLS", "JAMF", "NCNO", "POWI", "NTNX", "APPF",
    "CWAN", "AZPN", "FRPT", "TTGT", "SMAR", "FOUR",
    # Industrials & Construction (15)
    "BLD", "TREX", "IBP", "STRL", "CSWI", "ATKR", "AWI",
    "ESAB", "BCPC", "MATX", "MYRG", "NVT", "ITRI", "AAON", "KFRC",
    # Healthcare & Biotech (14)
    "CELH", "HIMS", "ACAD", "AXSM", "ENSG", "ITCI",
    "EXEL", "TMDX", "BRKR", "LNTH", "NEOG", "STVN", "PRCT", "RCKT",
    # Financials (10)
    "PIPR", "LPLA", "RYAN", "HLNE", "STEP",
    "GBCI", "WSFS", "CVBF", "PRI", "CTRE",
    # Energy & Commodities (6)
    "CEIX", "AM", "HESM", "DINO", "DNOW", "LBRT",
    # Specialty & Other (22)
    "AXTA", "FOXF", "MOD", "PATK", "GNRC", "PLMR",
    "WMS", "RXO", "PLAB", "CHEF", "NVST", "MGNI", "IIPR",
    "ALKT", "CALX", "SITM", "VCEL", "HRMY", "HALO", "GKOS", "PODD", "INSP",
}  # total: 100

# ── Combined universe & fast-lookup helpers ──────────────────────────────
WATCHLIST: list = sorted(LARGE_CAPS | MID_CAPS)   # 150 symbols, alphabetically sorted

# Maps every symbol → its tier string for O(1) lookup in position checks
ASSET_TYPE: dict = {s: "large" for s in LARGE_CAPS} | {s: "mid" for s in MID_CAPS}


def get_symbol_params(symbol: str) -> dict:
    """Return stop-loss, trailing trigger, and position-size parameters for a symbol's tier."""
    if ASSET_TYPE.get(symbol) == "mid":
        return {
            "stop_loss_pct":   CONFIG["MID_CAP_STOP_LOSS_PCT"],      # -5.0%
            "take_profit_pct": CONFIG["MID_CAP_TAKE_PROFIT_PCT"],    # +11.0% fixed take profit
            "position_pct":    CONFIG["MID_CAP_POSITION_PCT"],       # 6% of capital
        }
    # Default to large-cap profile (also covers any unlisted symbol)
    return {
        "stop_loss_pct":   CONFIG["LARGE_CAP_STOP_LOSS_PCT"],        # -3.5%
        "take_profit_pct": CONFIG["LARGE_CAP_TAKE_PROFIT_PCT"],      # +8.0% fixed take profit
        "position_pct":    CONFIG["LARGE_CAP_POSITION_PCT"],         # 10% of capital
    }

# ─────────────────────────────────────────────
# Data structures — unchanged
# ─────────────────────────────────────────────
@dataclass
class PaperPosition:
    symbol: str
    entry_price: float
    entry_date: str
    shares: float
    cost_usd: float
    stop_loss_price: float
    take_profit_price: float
    status: str = "open"
    exit_price: float = 0.0
    exit_date: str = ""
    exit_reason: str = ""
    pnl_usd: float = 0.0
    daily_pct_at_entry: float = 0.0
    # Alpaca order ID of the native GTC hard stop placed at entry.
    # Empty string means no native stop was placed (Python fallback active).
    stop_order_id: str = ""
    # Alpaca order ID of the native GTC limit (take profit) order placed at entry.
    # Empty string means no native take profit was placed (Python fallback active).
    take_profit_order_id: str = ""

@dataclass
class DailyStats:
    date: str
    starting_capital: float
    ending_capital: float
    trades_executed: int
    pnl_usd: float
    bot_stopped: bool = False
    stop_reason: str = ""

# ─────────────────────────────────────────────
# SERVERLESS MEMORY — read/write amara_dashboard.md
# ─────────────────────────────────────────────

def read_previous_dashboard() -> str:
    """
    Read amara_dashboard.md from the last run (if it exists).
    Returns the raw markdown string so Amara has context on her previous
    decisions and positions before running the new market scan.
    Returns empty string on first ever run.
    Reads from DASHBOARD_PATH (Google Drive sync folder).
    """
    path = DASHBOARD_PATH
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            log.info(f"📖 Previous dashboard loaded ({len(content):,} chars) — Amara has prior-run context")
            return content
        except Exception as e:
            log.warning(f"⚠️ Could not read previous dashboard: {e}")
    else:
        log.info("📖 No previous dashboard found — this appears to be Amara's first run")
    return ""


def write_amara_dashboard(bot: "AmaraBot") -> None:
    """
    Write (overwrite) amara_dashboard.md with a full Markdown run report.
    Sections:
      1. Run summary table (timestamp, cash, P&L, positions, win rate)
      2. Open positions table
      3. This run's decisions (buys / sells / skips)
      4. Latest scan results (up to 20 rows)
      5. Recent trade history (last 10 closed)
      6. SPY benchmark (if active)
    """
    now = datetime.now()
    data = bot.logger.data
    capital = CONFIG["TOTAL_CAPITAL"]

    cash        = data.get("cash_available", 0)
    port_val    = data.get("portfolio_value", 0)
    buy_power   = data.get("buying_power", 0)
    total_pnl   = bot.logger.get_total_pnl()
    today_pnl   = bot.logger.get_today_pnl()
    open_pos    = bot.logger.get_open_positions()
    all_closed  = [p for p in data["positions"] if p["status"] == "closed"]
    winners     = len([p for p in all_closed if p["pnl_usd"] > 0])
    win_rate_str = f"{winners}/{len(all_closed)} ({winners/len(all_closed)*100:.0f}%)" if all_closed else "No closed trades yet"

    mode = "🧪 PAPER" if CONFIG.get("PAPER_TRADING", True) else "💰 LIVE"
    pnl_pct = total_pnl / capital * 100 if capital else 0
    pnl_arrow = "▲" if total_pnl >= 0 else "▼"

    lines = []

    # ── Header ───────────────────────────────────────────────────────────────
    lines.append("# 🤖 Amara — Trading Dashboard")
    lines.append("")

    # ── Run Summary Table ─────────────────────────────────────────────────────
    lines.append("## 📊 Run Summary")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|:------|:------|")
    lines.append(f"| **Last Run** | `{now.strftime('%Y-%m-%d %H:%M:%S')}` |")
    lines.append(f"| **Mode** | {mode} |")
    lines.append(f"| **Cash Available** | `${cash:,.2f}` |")
    lines.append(f"| **Portfolio Value** | `${port_val:,.2f}` |")
    lines.append(f"| **Buying Power** | `${buy_power:,.2f}` |")
    lines.append(f"| **Total P&L** | `${total_pnl:+,.2f}` ({pnl_arrow} {abs(pnl_pct):.2f}%) |")
    lines.append(f"| **Today's P&L** | `${today_pnl:+,.2f}` |")
    lines.append(f"| **Win Rate** | {win_rate_str} |")
    lines.append(f"| **Open Positions** | {len(open_pos)} / {int(1 / CONFIG['MAX_POSITION_PCT'])} |")
    lines.append(f"| **Stocks Watched** | {len(WATCHLIST)} |")
    lines.append("")

    # ── Open Positions ────────────────────────────────────────────────────────
    lines.append("## 📋 Open Positions")
    lines.append("")
    if open_pos:
        lines.append("| Symbol | Entry Price | Last Price | Unrealised P&L | Entry Date | Stop Loss | Take Profit | Cost (USD) |")
        lines.append("|:------:|------------:|----------:|:--------------:|:----------:|----------:|------------:|-----------:|")
        for pos in open_pos:
            last = pos.get("last_price") or pos["entry_price"]
            pct  = (last - pos["entry_price"]) / pos["entry_price"] * 100
            sign = "🟢" if pct >= 0 else "🔴"
            lines.append(
                f"| **{pos['symbol']}** | ${pos['entry_price']:.2f} | ${last:.2f} | "
                f"{sign} {pct:+.1f}% | {pos['entry_date']} | "
                f"${pos['stop_loss_price']:.2f} | ${pos['take_profit_price']:.2f} | "
                f"${pos['cost_usd']:,.0f} |"
            )
    else:
        lines.append("*No open positions at time of this run.*")
    lines.append("")

    # ── This Run's Decisions ──────────────────────────────────────────────────
    lines.append("## 🧠 This Run's Decisions")
    lines.append("")
    decisions = getattr(bot, "_run_decisions", [])
    if decisions:
        lines.append("| Time | Symbol | Action | Confidence | Reason |")
        lines.append("|:----:|:------:|:------:|:----------:|:-------|")
        for d in decisions:
            reason = (d.get("reason") or "—").replace("|", "\\|").replace("\n", " ")
            reason = reason[:140] + ("…" if len(reason) > 140 else "")
            conf   = d.get("confidence", "—")
            lines.append(
                f"| {d.get('time', '—')} | **{d['symbol']}** | {d['action']} | "
                f"{conf} | {reason} |"
            )
    else:
        lines.append("*No buy/sell decisions this run — market was closed or no signals met the threshold.*")
    lines.append("")

    # ── Latest Scan Results (today, up to 20) ────────────────────────────────
    lines.append("## 🔍 Latest Scan Results")
    lines.append("")
    today_str    = now.strftime("%Y-%m-%d")
    scan_log     = data.get("scan_log", [])
    today_scans  = [s for s in scan_log if s.get("date") == today_str]
    display_scans = today_scans[-20:] if today_scans else scan_log[-20:]

    if display_scans:
        lines.append("| Time | Symbol | Score | RSI | Vol× | Sent to AI | Decision | AI Reason |")
        lines.append("|:----:|:------:|------:|----:|-----:|:----------:|:--------:|:----------|")
        for s in display_scans:
            sent    = "🧠 Yes" if s.get("sent_to_claude") else "—"
            if s.get("hold_review"):
                decision = "🔍 Hold Review"
            elif s.get("sent_to_claude"):
                decision = "✅ BUY" if s.get("claude_approved") else "❌ SKIP"
            else:
                decision = "—"
            reason = (s.get("claude_reason") or "—").replace("|", "\\|").replace("\n", " ")
            reason = reason[:100] + ("…" if len(reason) > 100 else "")
            lines.append(
                f"| {s.get('timestamp', '—')} | **{s['symbol']}** | {s.get('score', 0)} | "
                f"{s.get('rsi', 0):.1f} | {s.get('volume_ratio', 0):.1f}x | "
                f"{sent} | {decision} | {reason} |"
            )
    else:
        lines.append("*No scan data recorded yet.*")
    lines.append("")

    # ── Recent Trade History (last 10 closed) ─────────────────────────────────
    lines.append("## 📒 Recent Trade History")
    lines.append("")
    recent_closed = [p for p in data["positions"] if p["status"] == "closed"][-10:]
    if recent_closed:
        lines.append("| Symbol | Entry | Exit | P&L (USD) | Result | Reason | Closed |")
        lines.append("|:------:|------:|-----:|----------:|:------:|:-------|:------:|")
        for p in reversed(recent_closed):
            result = "✅ Win" if p.get("pnl_usd", 0) > 0 else "❌ Loss"
            reason = (p.get("exit_reason") or "—").replace("|", "\\|")
            lines.append(
                f"| **{p['symbol']}** | ${p['entry_price']:.2f} | "
                f"${p.get('exit_price', 0):.2f} | "
                f"`${p.get('pnl_usd', 0):+,.2f}` | {result} | {reason} | "
                f"{(p.get('exit_date') or '—')[:10]} |"
            )
    else:
        lines.append("*No closed trades yet.*")
    lines.append("")

    # ── SPY Benchmark ─────────────────────────────────────────────────────────
    bm = data.get("benchmark", {})
    if bm and bm.get("start_price"):
        spy_ret = (bm["current_price"] - bm["start_price"]) / bm["start_price"] * 100
        our_ret = pnl_pct
        days_in = (now - datetime.strptime(bm["start_date"], "%Y-%m-%d")).days + 1
        beating = "✅ Beating S&P" if our_ret > spy_ret else "❌ Trailing S&P"
        lines.append("## 🏁 S&P 500 Benchmark")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|:-------|:------|")
        lines.append(f"| SPY Start | `${bm['start_price']:.2f}` on {bm['start_date']} |")
        lines.append(f"| SPY Now | `${bm['current_price']:.2f}` ({spy_ret:+.2f}%) |")
        lines.append(f"| Our Return | `{our_ret:+.2f}%` |")
        lines.append(f"| Challenge Status | {beating} — Day {days_in} / 14 |")
        lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append("---")
    lines.append(f"*Amara · single-run serverless mode · generated {now.strftime('%Y-%m-%d %H:%M:%S')}*")

    dashboard_path = DASHBOARD_PATH
    dashboard_dir  = os.path.dirname(dashboard_path)
    if dashboard_dir:
        os.makedirs(dashboard_dir, exist_ok=True)
    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"📊 amara_dashboard.md written → {dashboard_path} ({len(lines)} lines)")


def write_dashboard_html(bot: "AmaraBot") -> None:
    """
    Write index.html — a mobile-friendly light-theme HTML dashboard for GitHub Pages.
    Accessible at https://fanglilli.github.io/project-retirement/
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tw_tz)
    data = bot.logger.data
    capital = CONFIG["TOTAL_CAPITAL"]

    cash       = data.get("cash_available", 0)
    port_val   = data.get("portfolio_value", 0)
    total_pnl  = bot.logger.get_total_pnl()
    today_pnl  = bot.logger.get_today_pnl()
    open_pos   = bot.logger.get_open_positions()
    all_closed = [p for p in data["positions"] if p["status"] == "closed"]
    winners    = len([p for p in all_closed if p["pnl_usd"] > 0])
    win_rate_str = f"{winners}/{len(all_closed)} ({winners/len(all_closed)*100:.0f}%)" if all_closed else "No closed trades yet"
    pnl_pct    = total_pnl / capital * 100 if capital else 0
    mode       = "PAPER" if CONFIG.get("PAPER_TRADING", True) else "LIVE"
    pnl_color  = "#16a34a" if total_pnl >= 0 else "#dc2626"
    pnl_sign   = "+" if total_pnl >= 0 else ""

    # ── Open positions rows ───────────────────────────────────────────────────
    if open_pos:
        pos_rows = ""
        for pos in open_pos:
            last = pos.get("last_price") or pos["entry_price"]
            pct  = (last - pos["entry_price"]) / pos["entry_price"] * 100
            dot  = "🟢" if pct >= 0 else "🔴"
            pos_rows += f"""
            <div class="card pos-card">
              <div class="pos-header">
                <span class="symbol">{pos['symbol']}</span>
                <span class="pnl" style="color:{'#16a34a' if pct>=0 else '#dc2626'}">{dot} {pct:+.1f}%</span>
              </div>
              <div class="pos-grid">
                <div><label>Entry</label><val>${pos['entry_price']:.2f}</val></div>
                <div><label>Last</label><val>${last:.2f}</val></div>
                <div><label>Stop</label><val>${pos['stop_loss_price']:.2f}</val></div>
                <div><label>Target</label><val>${pos['take_profit_price']:.2f}</val></div>
                <div><label>Cost</label><val>${pos['cost_usd']:,.0f}</val></div>
                <div><label>Since</label><val>{pos['entry_date']}</val></div>
              </div>
            </div>"""
        open_section = f'<div class="pos-list">{pos_rows}</div>'
    else:
        open_section = '<p class="empty">No open positions.</p>'

    # ── Decisions rows ────────────────────────────────────────────────────────
    decisions = getattr(bot, "_run_decisions", [])
    if decisions:
        dec_rows = ""
        for d in decisions:
            reason = (d.get("reason") or "—")[:200]
            action_color = "#16a34a" if "BUY" in str(d.get("action","")).upper() else "#dc2626" if "SELL" in str(d.get("action","")).upper() else "#6b7280"
            dec_rows += f"""
            <div class="card dec-card">
              <div class="dec-header">
                <span class="symbol">{d['symbol']}</span>
                <span style="color:{action_color};font-weight:700">{d.get('action','—')}</span>
                <span class="conf">{d.get('confidence','—')}</span>
              </div>
              <p class="reason">{reason}</p>
            </div>"""
        dec_section = f'<div class="dec-list">{dec_rows}</div>'
    else:
        dec_section = '<p class="empty">No buy/sell decisions this run — market closed or no signals met the threshold.</p>'

    # ── Scan results rows (top 20 qualifying stocks, with why not sent to Claude) ──
    today_str    = now.strftime("%Y-%m-%d")
    scan_log     = data.get("scan_log", [])
    today_scans  = [s for s in scan_log if s.get("date") == today_str]
    display_scans = sorted(today_scans, key=lambda x: x.get("score", 0), reverse=True)[:20] \
                    if today_scans else sorted(scan_log, key=lambda x: x.get("score", 0), reverse=True)[:20]

    if display_scans:
        scan_rows = ""
        for s in display_scans:
            sent = s.get("sent_to_claude", False)
            approved = s.get("claude_approved", False)
            hold = s.get("hold_review", False)

            if hold:
                status_badge = '<span class="sbadge review">Hold Review</span>'
                status_reason = s.get("claude_reason") or "Position under review"
            elif sent and approved:
                status_badge = '<span class="sbadge bought">✅ Bought</span>'
                status_reason = s.get("claude_reason") or "—"
            elif sent and not approved:
                status_badge = '<span class="sbadge skipped">❌ Claude Skip</span>'
                status_reason = s.get("claude_reason") or "Claude rejected"
            else:
                status_badge = '<span class="sbadge notsent">Not top 5</span>'
                status_reason = f"Score {s.get('score',0)}/100 — qualified but ranked outside top {CONFIG['TOP_CANDIDATES']}"

            scan_rows += f"""
            <div class="card scan-card">
              <div class="scan-header">
                <span class="symbol">{s['symbol']}</span>
                <span class="score">Score: {s.get('score',0)}/100</span>
                {status_badge}
              </div>
              <div class="scan-stats">
                <span>RSI {s.get('rsi',0):.1f}</span>
                <span>Vol {s.get('volume_ratio',0):.1f}x</span>
                <span>{s.get('timestamp','—')}</span>
              </div>
              <p class="reason">{status_reason[:200]}</p>
            </div>"""
        scan_section = f'<div class="scan-list">{scan_rows}</div>'
    else:
        scan_section = '<p class="empty">No scan data yet — runs during market hours only.</p>'

    # ── Trade history rows ────────────────────────────────────────────────────
    recent_closed = [p for p in data["positions"] if p["status"] == "closed"][-10:]
    if recent_closed:
        hist_rows = ""
        for p in reversed(recent_closed):
            win    = p.get("pnl_usd", 0) > 0
            badge  = '<span class="badge win">Win</span>' if win else '<span class="badge loss">Loss</span>'
            reason = (p.get("exit_reason") or "—")[:100]
            hist_rows += f"""
            <div class="card hist-card">
              <div class="hist-header">
                <span class="symbol">{p['symbol']}</span>
                {badge}
                <span class="hist-pnl" style="color:{'#16a34a' if win else '#dc2626'}">${p.get('pnl_usd',0):+,.2f}</span>
              </div>
              <div class="hist-detail">${p['entry_price']:.2f} → ${p.get('exit_price',0):.2f} &nbsp;·&nbsp; {(p.get('exit_date') or '—')[:10]}</div>
              <div class="reason">{reason}</div>
            </div>"""
        hist_section = f'<div class="hist-list">{hist_rows}</div>'
    else:
        hist_section = '<p class="empty">No closed trades yet.</p>'

    # ── SPY benchmark ─────────────────────────────────────────────────────────
    bm = data.get("benchmark", {})
    if bm and bm.get("start_price"):
        spy_ret   = (bm["current_price"] - bm["start_price"]) / bm["start_price"] * 100
        days_in   = (now.replace(tzinfo=None) - datetime.strptime(bm["start_date"], "%Y-%m-%d")).days + 1
        beating   = pnl_pct > spy_ret
        bm_section = f"""
        <section>
          <h2>S&P 500 Benchmark</h2>
          <div class="card">
            <div class="stat-grid">
              <div><label>SPY Start</label><val>${bm['start_price']:.2f} on {bm['start_date']}</val></div>
              <div><label>SPY Now</label><val>${bm['current_price']:.2f} ({spy_ret:+.2f}%)</val></div>
              <div><label>Our Return</label><val style="color:{'#16a34a' if pnl_pct>=0 else '#dc2626'}">{pnl_pct:+.2f}%</val></div>
              <div><label>Status</label><val>{'✅ Beating S&P' if beating else '❌ Trailing S&P'} — Day {days_in}/14</val></div>
            </div>
          </div>
        </section>"""
    else:
        bm_section = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>Amara Trading Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #f3f4f6; color: #111827; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px; max-width: 640px; margin: 0 auto; }}
    h1 {{ font-size: 1.3rem; font-weight: 700; margin-bottom: 4px; color: #111827; }}
    h2 {{ font-size: 0.75rem; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: .06em; margin: 24px 0 8px; }}
    .meta {{ font-size: 0.72rem; color: #9ca3af; margin-bottom: 16px; }}
    .badge-mode {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:.7rem; font-weight:700; background:#dbeafe; color:#1d4ed8; margin-left:8px; vertical-align:middle; }}
    .hero {{ background:#fff; border:1px solid #e5e7eb; border-radius:14px; padding:22px; margin-bottom:6px; text-align:center; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
    .hero .big {{ font-size:2.8rem; font-weight:800; color:{pnl_color}; line-height:1; }}
    .hero .sub {{ font-size:.82rem; color:#6b7280; margin-top:6px; }}
    .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:14px; margin-bottom:8px; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
    .stat-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    label {{ display:block; font-size:.68rem; color:#9ca3af; text-transform:uppercase; letter-spacing:.05em; margin-bottom:2px; }}
    val {{ display:block; font-size:.92rem; font-weight:600; color:#111827; }}
    .symbol {{ font-size:1rem; font-weight:700; color:#111827; }}
    .pnl {{ font-size:.95rem; font-weight:700; }}
    .pos-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }}
    .pos-grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; }}
    .dec-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; gap:8px; flex-wrap:wrap; }}
    .conf {{ font-size:.72rem; color:#9ca3af; }}
    .reason {{ font-size:.78rem; color:#6b7280; line-height:1.5; margin-top:5px; }}
    .scan-header {{ display:flex; align-items:center; gap:8px; margin-bottom:4px; flex-wrap:wrap; }}
    .score {{ font-size:.78rem; color:#6b7280; font-weight:600; }}
    .scan-stats {{ display:flex; gap:14px; font-size:.72rem; color:#9ca3af; margin-bottom:4px; }}
    .hist-header {{ display:flex; align-items:center; gap:8px; margin-bottom:4px; }}
    .hist-pnl {{ margin-left:auto; font-weight:700; font-size:.9rem; }}
    .hist-detail {{ font-size:.72rem; color:#9ca3af; margin-bottom:3px; }}
    .badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:.68rem; font-weight:700; }}
    .badge.win {{ background:#dcfce7; color:#16a34a; }}
    .badge.loss {{ background:#fee2e2; color:#dc2626; }}
    .sbadge {{ display:inline-block; padding:2px 7px; border-radius:4px; font-size:.66rem; font-weight:700; }}
    .sbadge.bought {{ background:#dcfce7; color:#16a34a; }}
    .sbadge.skipped {{ background:#fee2e2; color:#dc2626; }}
    .sbadge.notsent {{ background:#f3f4f6; color:#6b7280; border:1px solid #e5e7eb; }}
    .sbadge.review {{ background:#fef9c3; color:#854d0e; }}
    .empty {{ color:#9ca3af; font-size:.85rem; padding:6px 0; }}
    .footer {{ font-size:.68rem; color:#d1d5db; text-align:center; margin-top:28px; padding-top:16px; border-top:1px solid #e5e7eb; }}
  </style>
</head>
<body>
  <h1>🤖 Amara <span class="badge-mode">{mode}</span></h1>
  <p class="meta">Last updated: {now.strftime('%Y-%m-%d %H:%M:%S')} TWN · auto-refreshes every 5 min</p>

  <div class="hero">
    <div class="big">{pnl_sign}${abs(total_pnl):,.2f}</div>
    <div class="sub">Total P&amp;L &nbsp;·&nbsp; {pnl_sign}{abs(pnl_pct):.2f}%</div>
  </div>

  <section>
    <h2>Portfolio</h2>
    <div class="card">
      <div class="stat-grid">
        <div><label>Cash</label><val>${cash:,.2f}</val></div>
        <div><label>Portfolio Value</label><val>${port_val:,.2f}</val></div>
        <div><label>Today's P&amp;L</label><val style="color:{'#16a34a' if today_pnl>=0 else '#dc2626'}">{'+' if today_pnl>=0 else ''}${today_pnl:,.2f}</val></div>
        <div><label>Win Rate</label><val>{win_rate_str}</val></div>
        <div><label>Open Positions</label><val>{len(open_pos)} / {int(1 / CONFIG['MAX_POSITION_PCT'])}</val></div>
        <div><label>Watchlist</label><val>{len(WATCHLIST)} stocks</val></div>
      </div>
    </div>
  </section>

  <section>
    <h2>Open Positions ({len(open_pos)})</h2>
    {open_section}
  </section>

  <section>
    <h2>This Run's Decisions</h2>
    {dec_section}
  </section>

  <section>
    <h2>Top Scan Results — Why Each Stock Did or Didn't Proceed</h2>
    {scan_section}
  </section>

  <section>
    <h2>Recent Trade History</h2>
    {hist_section}
  </section>

  {bm_section}

  <p class="footer">Amara · single-run serverless mode · generated {now.strftime('%Y-%m-%d %H:%M:%S')}</p>
</body>
</html>"""

    html_path = os.path.join(_SCRIPT_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"🌐 index.html written → {html_path}")

# ─────────────────────────────────────────────
# Trade Logger — save() no longer rewrites an HTML file
# ─────────────────────────────────────────────
class TradeLogger:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "positions": [],
            "daily_stats": [],
            "scan_log": [],
            "total_capital_usd": CONFIG["TOTAL_CAPITAL"]
        }

    def save(self):
        """Persist trade data to JSON. No HTML dashboard update in serverless mode."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def log_scan_result(self, symbol: str, score: int, rsi: float, volume_ratio: float,
                        tech_signal: bool, claude_approved: bool, claude_reason: str,
                        sent_to_claude: bool = False, confidence: int = 0, risk: str = "",
                        key_signal: str = "", reasons: list = None, momentum_5d_pct: float = 0.0,
                        current_price: float = 0.0, hold_review: bool = False):
        if "scan_log" not in self.data:
            self.data["scan_log"] = []
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "symbol": symbol,
            "score": score,
            "rsi": round(rsi, 1),
            "volume_ratio": round(volume_ratio, 2),
            "tech_signal": tech_signal,
            "sent_to_claude": sent_to_claude,
            "hold_review": hold_review,
            "claude_approved": claude_approved,
            "claude_reason": claude_reason,
            "confidence": confidence,
            "risk": risk,
            "key_signal": key_signal,
            "reasons": reasons or [],
            "momentum_5d_pct": round(momentum_5d_pct, 2),
            "current_price": round(current_price, 2)
        }
        self.data["scan_log"].append(entry)
        self.data["scan_log"] = self.data["scan_log"][-200:]
        self.save()

    def add_position(self, pos: PaperPosition):
        self.data["positions"].append(asdict(pos))
        self.save()
        log.info(f"📋 New position: {pos.symbol} x{pos.shares:.4f} @ ${pos.entry_price:.2f} (${pos.cost_usd:,.0f} USD)")

    def update_position(self, symbol: str, updates: dict):
        for pos in self.data["positions"]:
            if pos["symbol"] == symbol and pos["status"] == "open":
                pos.update(updates)
                break
        self.save()

    def get_open_positions(self) -> list:
        return [p for p in self.data["positions"] if p["status"] == "open"]

    def get_total_invested(self) -> float:
        return sum(p["cost_usd"] for p in self.get_open_positions())

    def get_today_pnl(self) -> float:
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(
            p["pnl_usd"] for p in self.data["positions"]
            if p.get("exit_date", "").startswith(today)
        )

    def get_total_pnl(self) -> float:
        return sum(p["pnl_usd"] for p in self.data["positions"] if p["status"] == "closed")

    def get_unrealized_pnl(self) -> float:
        """Sum unrealized P&L across all open positions using last known price."""
        total = 0.0
        for p in self.get_open_positions():
            last = p.get("last_price") or p["entry_price"]
            pnl_pct = (last - p["entry_price"]) / p["entry_price"]
            total += pnl_pct * p["cost_usd"]
        return total

    def log_daily_stats(self, stats: DailyStats):
        self.data["daily_stats"].append(asdict(stats))
        self.save()

# ─────────────────────────────────────────────
# Market Data (Alpaca) — unchanged
# ─────────────────────────────────────────────
class MarketData:
    def __init__(self):
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            self.client = StockHistoricalDataClient(
                CONFIG["ALPACA_API_KEY"],
                CONFIG["ALPACA_SECRET_KEY"]
            )
            log.info("✅ Alpaca market data connected")
        except Exception as e:
            log.error(f"❌ Alpaca connection failed: {e}")
            self.client = None

    def get_bars(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        if not self.client:
            return None
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import DataFeed
            end   = datetime.now()
            start = end - timedelta(days=days)
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX
            )
            bars = self.client.get_stock_bars(request)
            df   = bars.df
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(symbol, level=0)
            return df.reset_index()
        except Exception as e:
            log.warning(f"⚠️ Cannot fetch bars for {symbol}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            from alpaca.data.enums import DataFeed
            request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
            trade   = self.client.get_stock_latest_trade(request)
            price   = float(trade[symbol].price)
            return price if price > 0 else None
        except Exception as e:
            log.warning(f"⚠️ Cannot fetch price for {symbol}: {e}")
            return None

    def get_bars_batch(self, symbols: list, days: int = 300) -> Optional[pd.DataFrame]:
        """
        Fetch daily OHLCV bars for ALL symbols in a single Alpaca API call.

        Returns a MultiIndex DataFrame with (symbol, timestamp) as the index,
        or None on failure.  days=300 (≈214 trading days) ensures enough history
        for the 200-day MA. Do NOT reduce below 290 — rolling(200) needs 200
        trading day rows minimum, which requires ~285 calendar days.
        """
        if not self.client or not symbols:
            return None
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import DataFeed
            end   = datetime.now()
            start = end - timedelta(days=days)
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX
            )
            bars = self.client.get_stock_bars(request)
            df   = bars.df   # MultiIndex: (symbol, timestamp)
            n_syms = df.index.get_level_values(0).nunique()
            log.info(f"📦 Batch fetch complete — {n_syms}/{len(symbols)} symbols returned, {days}d window")
            return df
        except Exception as e:
            log.error(f"❌ Batch bar fetch failed: {e}")
            return None

    def get_news(self, symbol: str, hours: int = 48, limit: int = 5) -> list:
        """Fetch Benzinga headlines via Alpaca News API."""
        try:
            from alpaca.data.historical.news import NewsClient
            from alpaca.data.requests import NewsRequest
            news_client = NewsClient(CONFIG["ALPACA_API_KEY"], CONFIG["ALPACA_SECRET_KEY"])
            end   = datetime.now()
            start = end - timedelta(hours=hours)
            request = NewsRequest(
                symbols=symbol,
                start=start,
                end=end,
                limit=limit,
                sort="desc"
            )
            result    = news_client.get_news(request)
            headlines = []
            for article in result.news:
                ts = article.created_at.strftime("%m/%d %H:%M") if article.created_at else "?"
                headlines.append(f"• {article.headline} ({article.source}, {ts})")
            if headlines:
                log.info(f"📰 {symbol} news: {len(headlines)} items (last {hours}h)")
            return headlines
        except Exception as e:
            log.warning(f"⚠️ Cannot fetch news for {symbol}: {e}")
            return []

# ─────────────────────────────────────────────
# Technical Analysis — unchanged
# ─────────────────────────────────────────────
class TechnicalAnalysis:
    @staticmethod
    def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
        delta = prices.diff()
        gain  = delta.where(delta > 0, 0).rolling(period).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    @staticmethod
    def calculate_sma(prices: pd.Series, period: int) -> float:
        return float(prices.rolling(period).mean().iloc[-1])

    @staticmethod
    def volume_surge(volumes: pd.Series, period: int = 20) -> float:
        avg_volume     = volumes.rolling(period).mean().iloc[-1]
        current_volume = volumes.iloc[-1]
        return float(current_volume / avg_volume) if avg_volume > 0 else 0

    # momentum_score() retired — replaced by vectorised batch pipeline in
    # scan_for_opportunities(). calculate_rsi(), calculate_sma(), and
    # volume_surge() are still used individually for hold-review computation.

# ─────────────────────────────────────────────
# Claude AI Analysis — now receives previous-run context
# ─────────────────────────────────────────────
class ClaudeAnalyst:
    def __init__(self):
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=CONFIG["ANTHROPIC_API_KEY"])
            log.info("✅ Claude API connected")
        except Exception as e:
            log.error(f"❌ Claude API failed: {e}")
            self.client = None

    def analyze(self, symbol: str, tech_data: dict, news: list = None,
                sym_params: dict = None, previous_context: str = "") -> dict:
        """
        Final buy/skip judgment from Claude.
        Only called when technical score >= 60 (saves API cost).
        previous_context: brief excerpt from amara_dashboard.md (prior run summary).
        """
        if not self.client:
            return {"approve": tech_data["buy_signal"], "reason": "Claude offline — technical signal used"}

        if sym_params is None:
            sym_params = get_symbol_params(symbol)
        sl_pct = sym_params["stop_loss_pct"]
        tp_pct = sym_params["take_profit_pct"]

        news_section = (
            "Recent news (Benzinga):\n" + "\n".join(news)
            if news else
            "Recent news: none returned by API"
        )

        # Include a brief prior-run summary if available
        prior_block = ""
        if previous_context:
            # Extract just the Run Summary table (first ~800 chars) to stay token-efficient
            excerpt = previous_context[:800].strip()
            prior_block = f"""
Prior run context (for awareness only — do not let it override current signals):
{excerpt}
...
"""

        prompt = f"""
You are a disciplined short-term trading analyst. Based on the technical indicators and
recent news below, decide whether to BUY this stock for a 3-5 day momentum trade.
{prior_block}
Stock: {symbol}
Current price: ${tech_data['current_price']:.2f}
RSI (14-day): {tech_data['rsi']:.1f}
20-day SMA: ${tech_data['sma_20']:.2f} (price is {((tech_data['current_price']/tech_data['sma_20'])-1)*100:.1f}% above)
Volume multiple: {tech_data['volume_ratio']:.1f}x (vs 20-day avg)
5-day momentum: {tech_data['momentum_5d_pct']:.1f}%
Technical score: {tech_data['score']}/100
Score breakdown: {', '.join(tech_data['reasons'])}

{news_section}

Trade parameters:
- Stop loss: -{sl_pct*100:.0f}% (→ ${tech_data['current_price'] * (1 - sl_pct):.2f})
- Take profit: +{tp_pct*100:.0f}% (→ ${tech_data['current_price'] * (1 + tp_pct):.2f})
- Max hold: {CONFIG['MAX_HOLD_DAYS']} days

News scoring guide:
- Positive catalyst (earnings beat, analyst upgrade, contract win) → increases conviction
- Negative event (miss, downgrade, regulatory risk, lawsuit) → veto or reduce confidence
- Neutral / no news → pure technical judgment

Reply ONLY with JSON (no other text):
{{
  "approve": true or false,
  "confidence": integer 1-10,
  "analysis": "3-4 sentences: ① key technical signal or news catalyst ② news vs. technical alignment ③ overall risk/reward ④ primary downside risk"
}}
"""
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            import re
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(json_match.group()) if json_match else json.loads(raw)
            verdict = "✅ APPROVED" if result["approve"] else "❌ REJECTED"
            log.info(f"🧠 Claude on {symbol}: {verdict} (conf {result.get('confidence','?')}/10) — {result.get('analysis','')[:80]}")
            return result
        except Exception as e:
            log.warning(f"⚠️ Claude analysis failed: {e} — falling back to technical signal")
            approve = tech_data["buy_signal"] and tech_data["score"] >= 70
            return {"approve": approve, "confidence": 7,
                    "analysis": "Claude analysis failed; technical score was sufficient"}

    def review_hold(self, symbol: str, pos: dict, tech_data: dict,
                    news: list = None, previous_context: str = "") -> dict:
        """Qualitative hold review for an existing position. Informational only."""
        if not self.client:
            return {"hold": True, "confidence": 5, "analysis": "Claude offline — cannot review hold"}

        entry_date     = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
        hold_days      = (datetime.now() - entry_date).days
        current_price  = tech_data["current_price"]
        entry_price    = pos["entry_price"]
        pnl_pct        = (current_price - entry_price) / entry_price * 100
        stop_distance  = (current_price - pos["stop_loss_price"]) / current_price * 100
        tp_distance    = (pos["take_profit_price"] - current_price) / current_price * 100

        news_section = (
            "Recent news (Benzinga):\n" + "\n".join(news)
            if news else
            "Recent news: none"
        )

        # Include a brief prior-run summary if available (mirrors analyze() logic)
        prior_block = ""
        if previous_context:
            excerpt = previous_context[:800].strip()
            prior_block = f"""
Prior run context (for awareness only — do not let it override current signals):
{excerpt}
...
"""

        prompt = f"""
You are a disciplined short-term trading analyst reviewing an existing position.
This is informational only — it does NOT trigger any stop-loss or take-profit rules.
{prior_block}

Stock: {symbol}
Entry price: ${entry_price:.2f}
Current price: ${current_price:.2f} ({pnl_pct:+.1f}%)
Days held: {hold_days} (max {CONFIG['MAX_HOLD_DAYS']} days)
Stop loss: ${pos['stop_loss_price']:.2f} ({stop_distance:.1f}% away)
Take profit: ${pos['take_profit_price']:.2f} ({tp_distance:.1f}% away)

Latest technical indicators:
RSI (14-day): {tech_data['rsi']:.1f}
Volume multiple: {tech_data['volume_ratio']:.1f}x
5-day momentum: {tech_data['momentum_5d_pct']:.1f}%
Technical score: {tech_data['score']}/100
Score breakdown: {', '.join(tech_data['reasons']) if tech_data['reasons'] else 'no strong signals'}

{news_section}

Reply ONLY with JSON:
{{
  "hold": true or false,
  "confidence": integer 1-10,
  "analysis": "2-3 sentences: ① current momentum vs. original buy thesis ② any early warning signals or negative news ③ recommended watch points"
}}
"""
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            import re
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(json_match.group()) if json_match else json.loads(raw)
            verdict = "✅ Continue holding" if result.get("hold") else "⚠️ Watch risk"
            log.info(f"🔍 Hold review {symbol}: {verdict} (conf {result.get('confidence','?')}/10)")
            return result
        except Exception as e:
            log.warning(f"⚠️ Hold review failed for {symbol}: {e}")
            return {"hold": True, "confidence": 5, "analysis": f"Review failed — check manually: {e}"}

# ─────────────────────────────────────────────
# Main Bot — single-run, serverless
# ─────────────────────────────────────────────
class AmaraBot:
    def __init__(self, previous_context: str = ""):
        self.logger          = TradeLogger(CONFIG["TRADES_FILE"])
        self.market          = MarketData()
        self.ta              = TechnicalAnalysis()
        self.claude          = ClaudeAnalyst()
        self.daily_stopped   = False
        self.today           = datetime.now().strftime("%Y-%m-%d")
        self.previous_context = previous_context   # prior-run markdown for Claude context
        self._run_decisions  = []                  # collects every buy/sell/skip this run

        # Alpaca trading client (order execution)
        self.trading = None
        try:
            from alpaca.trading.client import TradingClient
            paper = CONFIG.get("PAPER_TRADING", True)
            self.trading = TradingClient(
                CONFIG["ALPACA_API_KEY"],
                CONFIG["ALPACA_SECRET_KEY"],
                paper=paper
            )
            mode = "PAPER" if paper else "⚠️  LIVE (real money)"
            log.info(f"✅ Alpaca Trading Client connected — mode: {mode}")
        except Exception as e:
            log.error(f"❌ Alpaca Trading Client failed: {e}")

        # Sync local JSON with Alpaca's actual account state
        self._sync_with_alpaca()

        log.info("🚀 Amara initialised")
        log.info(f"   Capital: ${CONFIG['TOTAL_CAPITAL']:,} USD")
        log.info(f"   Max position: ${CONFIG['TOTAL_CAPITAL'] * CONFIG['MAX_POSITION_PCT']:,.0f} USD")
        log.info(f"   Daily loss limit: ${CONFIG['TOTAL_CAPITAL'] * CONFIG['DAILY_LOSS_LIMIT_PCT']:,.0f} USD")

    # ── Market hours check ─────────────────────────────────────────────────
    def is_market_open(self) -> bool:
        import pytz
        et      = pytz.timezone("America/New_York")
        now_et  = datetime.now(et)
        if now_et.weekday() >= 5:
            return False
        market_open  = now_et.replace(hour=9, minute=30, second=0)
        market_close = now_et.replace(hour=16, minute=0, second=0)
        return market_open <= now_et < market_close

    # ── Risk limits ────────────────────────────────────────────────────────
    def check_daily_limits(self) -> bool:
        # Include unrealized losses — open positions bleeding money count against the limit
        today_pnl    = self.logger.get_today_pnl()
        unrealized   = self.logger.get_unrealized_pnl()
        combined     = today_pnl + unrealized
        daily_limit  = -CONFIG["TOTAL_CAPITAL"] * CONFIG["DAILY_LOSS_LIMIT_PCT"]
        if combined <= daily_limit:
            log.warning(
                f"🚨 Daily loss limit hit — realized ${today_pnl:+,.2f} + "
                f"unrealized ${unrealized:+,.2f} = ${combined:+,.2f} USD. No new trades."
            )
            self.daily_stopped = True
            return False
        return True

    def check_total_limits(self) -> bool:
        # Include unrealized losses — total drawdown includes open positions
        total_pnl   = self.logger.get_total_pnl()
        unrealized  = self.logger.get_unrealized_pnl()
        combined    = total_pnl + unrealized
        total_limit = -CONFIG["TOTAL_CAPITAL"] * CONFIG["TOTAL_LOSS_LIMIT_PCT"]
        if combined <= total_limit:
            log.critical(
                f"🚨🚨 Total loss limit hit — realized ${total_pnl:+,.2f} + "
                f"unrealized ${unrealized:+,.2f} = ${combined:+,.2f} USD. Bot halted."
            )
            return False
        return True

    # ── Position monitoring ────────────────────────────────────────────────
    def check_existing_positions(self):
        positions = self.logger.get_open_positions()
        if not positions:
            return
        log.info(f"📋 Checking {len(positions)} open position(s)...")
        for pos in positions:
            symbol        = pos["symbol"]
            current_price = self.market.get_current_price(symbol)
            if not current_price:
                continue

            params     = get_symbol_params(symbol)
            tier       = ASSET_TYPE.get(symbol, "large").upper()
            entry_price = pos["entry_price"]
            pnl_pct    = (current_price - entry_price) / entry_price

            entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
            hold_days  = (datetime.now() - entry_date).days

            # ── Hard stop-loss check ──────────────────────────────────────
            if current_price <= pos["stop_loss_price"]:
                if pos.get("stop_order_id"):
                    # Native GTC stop is live — Alpaca will execute it.
                    # _sync_with_alpaca() reconciles the closure on next run.
                    log.info(
                        f"  {symbol}: ⚠️ price ${current_price:.2f} at/below "
                        f"stop ${pos['stop_loss_price']:.2f} — "
                        f"native Alpaca stop active, awaiting fill"
                    )
                else:
                    # No native stop was placed — Python fallback
                    log.warning(
                        f"  {symbol}: ⚠️ no native stop order found — "
                        f"executing Python fallback stop-loss"
                    )
                    self._close_position(
                        pos, current_price,
                        f"Hard stop-loss -{params['stop_loss_pct']*100:.1f}% [{tier}] "
                        f"(Python fallback — no native stop)"
                    )
                continue

            # ── Fixed take profit check ───────────────────────────────────
            if current_price >= pos["take_profit_price"]:
                if pos.get("take_profit_order_id"):
                    # Native GTC limit order is live — Alpaca will execute it.
                    # _sync_with_alpaca() reconciles the closure on next run.
                    log.info(
                        f"  {symbol}: 🎯 price ${current_price:.2f} at/above "
                        f"take profit ${pos['take_profit_price']:.2f} — "
                        f"native Alpaca limit order active, awaiting fill"
                    )
                else:
                    # No native take profit was placed — Python fallback
                    log.warning(
                        f"  {symbol}: ⚠️ no native take profit order found — "
                        f"executing Python fallback take profit"
                    )
                    self._close_position(
                        pos, current_price,
                        f"Fixed take profit +{params['take_profit_pct']*100:.1f}% [{tier}] "
                        f"(Python fallback — no native limit order)"
                    )
                continue

            # ── Max hold days ─────────────────────────────────────────────
            if hold_days >= CONFIG["MAX_HOLD_DAYS"]:
                self._close_position(pos, current_price, f"Max hold days ({hold_days}d)")
                continue

            log.info(f"  {symbol}: ${current_price:.2f} | {pnl_pct*100:+.1f}% | {hold_days}d held")
            self.logger.update_position(symbol, {
                "last_price":   round(current_price, 2),
                "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })

    def _close_position(self, pos: dict, exit_price: float, reason: str):
        """Submit SELL order to Alpaca, record locally, append to run decisions."""
        fill_price = exit_price

        # ── Cancel both native GTC orders (stop + take profit) ───────────
        # Prevents orphan orders from triggering a short sale after the position
        # is already closed by the Python fallback.
        if self.trading:
            for order_id, label in [
                (pos.get("stop_order_id", ""),       "stop"),
                (pos.get("take_profit_order_id", ""), "take profit"),
            ]:
                if order_id:
                    try:
                        self.trading.cancel_order_by_id(order_id)
                        log.info(f"🗑️  Cancelled native {label} order {order_id} for {pos['symbol']}")
                    except Exception as e:
                        log.warning(
                            f"⚠️ Could not cancel {label} order for {pos['symbol']} "
                            f"(may already be processed): {e}"
                        )

        if self.trading:
            try:
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums   import OrderSide, TimeInForce
                shares = pos.get("shares", 0)
                order  = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=pos["symbol"],
                        qty=shares,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                )
                log.info(f"📤 SELL submitted: {pos['symbol']} x{shares} (order {order.id})")
                filled = self._wait_for_fill(order.id, timeout=60)
                if filled:
                    fill_price = float(filled.filled_avg_price)
                    log.info(f"✅ SELL filled: {pos['symbol']} @ ${fill_price:.2f}")
                else:
                    log.warning(f"⚠️ Fill not confirmed for {pos['symbol']} — using trigger price")
            except Exception as e:
                log.error(f"❌ SELL order failed for {pos['symbol']}: {e}")

        # Use actual shares × price delta — more accurate than planned notional
        shares  = pos.get("shares", 0)
        pnl_usd = (fill_price - pos["entry_price"]) * shares
        pnl_pct = (fill_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0
        self.logger.update_position(pos["symbol"], {
            "status":     "closed",
            "exit_price": fill_price,
            "exit_date":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "exit_reason": reason,
            "pnl_usd":    pnl_usd
        })
        emoji = "✅" if pnl_usd > 0 else "❌"
        mode  = "LIVE" if not CONFIG.get("PAPER_TRADING", True) else "Paper"
        log.info(f"{emoji} [{mode}] Closed {pos['symbol']} @ ${fill_price:.2f} | {reason} | P&L: ${pnl_usd:+,.2f}")

        # Record for dashboard
        self._run_decisions.append({
            "time":       datetime.now().strftime("%H:%M"),
            "symbol":     pos["symbol"],
            "action":     f"🔴 SELL ({reason})",
            "confidence": "—",
            "reason":     f"P&L: ${pnl_usd:+,.2f} ({pnl_pct*100:+.1f}%)"
        })

    # ── Opportunity scanner — vectorized batch pipeline ───────────────────
    def scan_for_opportunities(self):
        """
        Hybrid dual-tier scan across 150 symbols (50 large-cap + 100 mid-cap).

        Pipeline:
          1. Single batch Alpaca request → MultiIndex DataFrame (symbol × timestamp)
          2. Vectorised MA20, MA200, RSI(14), volume ratio across all symbols
          3. Real-time price fetch (bulk latest trade) for live price > MA20 comparison
          4. 4-criterion composite score (max 100):
               Volume ≥ 1.5× + bullish candle → 35 pts  (conviction)
               RSI 55–75                       → 30 pts  (momentum quality)
               Real-time price > 20-MA         → 25 pts  (direction)
               MA20 > MA200 bonus              → 10 pts  (long-term context)
          5. Hard filter: score ≥ 60
          6. Rank by score desc; tiebreak by % above 20-MA desc
          7. Top 5 fresh candidates → news fetch → Claude.analyze()
          8. Hold reviews for open positions using the same batch data
        """
        if self.daily_stopped:
            log.info("⏸️ Daily loss limit active — skipping new trade scan")
            return

        open_positions = self.logger.get_open_positions()
        open_symbols   = {p["symbol"] for p in open_positions}
        max_positions  = int(1 / CONFIG["MAX_POSITION_PCT"])
        current_count  = len(open_positions)
        slots          = max_positions - current_count

        if slots <= 0:
            log.info(f"📊 Positions full ({current_count}/{max_positions}) — will still run hold reviews")
        else:
            log.info(f"📊 Positions: {current_count}/{max_positions} — {slots} slot(s) free")

        # ── Step 1: Single batch fetch for all 150 symbols ────────────────
        log.info(f"🔍 Batch-fetching bars for {len(WATCHLIST)} symbols (300-day window)...")
        raw = self.market.get_bars_batch(WATCHLIST, days=300)
        if raw is None or raw.empty:
            log.warning("⚠️ Batch bar fetch returned no data — skipping scan this run")
            return

        # ── Step 2: Vectorised indicator calculation ──────────────────────
        # raw MultiIndex is (symbol, timestamp); unstack symbol → columns
        # Result: rows=timestamps, columns=symbols
        close_all  = raw["close"].unstack(level=0)
        open_all   = raw["open"].unstack(level=0)
        volume_all = raw["volume"].unstack(level=0)

        # Moving averages
        ma5_all   = close_all.rolling(5).mean()
        ma20_all  = close_all.rolling(20).mean()
        ma200_all = close_all.rolling(200).mean()   # kept for dashboard display only

        # 5-day momentum (short-term price change — directly relevant to 3-5 day swing trades)
        mom5_all = ((close_all - close_all.shift(5)) / close_all.shift(5) * 100)

        # Vectorised RSI(14) across all symbols simultaneously
        _delta     = close_all.diff()
        _gain      = _delta.clip(lower=0).rolling(14).mean()
        _loss      = (-_delta.clip(upper=0)).rolling(14).mean()
        rsi_all    = 100 - (100 / (1 + _gain / _loss))

        # Vectorised volume ratio: today's volume vs 20-day average
        vol_ratio_all = volume_all / volume_all.rolling(20).mean()

        # Latest values (most recent completed daily bar)
        last_close     = close_all.iloc[-1]
        last_open      = open_all.iloc[-1]
        last_ma5       = ma5_all.iloc[-1]
        last_ma20      = ma20_all.iloc[-1]
        last_ma200     = ma200_all.iloc[-1]   # kept for hold review context only
        last_rsi       = rsi_all.iloc[-1]
        last_vol_ratio = vol_ratio_all.iloc[-1]
        last_mom5      = mom5_all.iloc[-1]

        # ── Real-time price fetch (single bulk call for all 150 symbols) ──
        # Used for price > MA20 comparison so intraday scans reflect live price.
        # Falls back to last daily close if live fetch fails.
        real_price = last_close.copy()
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            from alpaca.data.enums    import DataFeed
            req    = StockLatestTradeRequest(symbol_or_symbols=list(WATCHLIST), feed=DataFeed.IEX)
            trades = self.market.client.get_stock_latest_trade(req)
            for sym, trade in trades.items():
                if sym in real_price.index:
                    real_price[sym] = float(trade.price)
            log.info(f"📡 Real-time prices fetched for {len(trades)} symbols")
        except Exception as e:
            log.warning(f"⚠️ Real-time price fetch failed ({e}) — using last daily close")

        # ── Step 3: 4-criterion composite score (max 100) ────────────────
        # 200-MA removed — long-term context irrelevant for 3-5 day swing trades.
        # Replaced with 5-day momentum: directly measures short-term price velocity.
        valid = (last_close.notna() & last_ma5.notna() &
                 last_ma20.notna() &
                 last_rsi.notna() & last_vol_ratio.notna())

        rsi_ok         = (last_rsi >= CONFIG["RSI_BUY_THRESHOLD"]) & (last_rsi <= CONFIG["RSI_OVERBOUGHT"])
        price_vs_ma    = real_price > last_ma20          # direction: real-time price above 20-day MA
        bullish_candle = last_close > last_open          # green day: yesterday close > yesterday open
        vol_surge      = (last_vol_ratio >= CONFIG["VOLUME_SURGE_FACTOR"]) & bullish_candle
        # 5-day momentum: +2% minimum over last 5 trading days — confirms swing is already underway
        # NaN-safe: False if insufficient history
        mom5_ok        = last_mom5 > 2.0

        # ── Scoring (max 100 pts) ─────────────────────────────────────────
        # Four dimensions of short-term swing strength.
        score_all = (
            (vol_surge.astype(int)   * 35) +   # conviction: unusual buying volume  (35 pts)
            (rsi_ok.astype(int)      * 30) +   # momentum quality: RSI sweet spot   (30 pts)
            (price_vs_ma.astype(int) * 25) +   # direction: price above 20-MA       (25 pts)
            (mom5_ok.astype(int)     * 10)     # short-term velocity: +2% over 5d   (10 pts)
        ).where(valid, other=0)

        # ── Step 4: Filter — score ≥ 60 ──────────────────────────────────
        qualifying = valid & (score_all >= 60)

        pct_above_ma20 = ((last_close - last_ma20) / last_ma20 * 100).where(qualifying)

        log.info(f"   Qualifying (score≥60, vol+RSI+MA20 signals): "
                 f"{qualifying.sum()}/{len(WATCHLIST)} symbols")

        # ── Step 5: Rank by score desc, tiebreak by % above 20-MA desc ───
        ranking_df = pd.DataFrame({
            "score":         score_all[qualifying],
            "pct_above_ma20": pct_above_ma20[qualifying],
        }).sort_values(["score", "pct_above_ma20"], ascending=[False, False])

        # Exclude symbols already held
        fresh_df = ranking_df.drop(
            index=[s for s in open_symbols if s in ranking_df.index],
            errors="ignore"
        )
        top_n      = CONFIG.get("TOP_CANDIDATES", 5)
        top_symbols = fresh_df.head(top_n).index.tolist()

        log.info(f"   Top {top_n} candidates → Claude: "
                 f"{', '.join(top_symbols) if top_symbols else 'none'}")

        # ── Step 6: Claude analysis for top N only ────────────────────────
        bought_this_scan = 0
        for symbol in top_symbols:
            if symbol not in close_all.columns:
                log.warning(f"⚠️ {symbol} missing from batch close data — skipping")
                continue

            close_ser = close_all[symbol].dropna()
            if close_ser.empty:
                continue

            current_price = float(real_price[symbol]) if symbol in real_price.index else float(close_ser.iloc[-1])
            sma_5         = float(last_ma5[symbol])
            sma_20        = float(last_ma20[symbol])
            sma_200       = float(last_ma200[symbol])
            rsi           = float(last_rsi[symbol])
            vol_ratio     = float(last_vol_ratio[symbol]) if not pd.isna(last_vol_ratio[symbol]) else 0.0
            score         = int(score_all[symbol])
            pct_above     = float(pct_above_ma20[symbol])

            # 5-day momentum and daily change
            p5ago      = float(close_ser.iloc[-6]) if len(close_ser) > 5 else current_price
            mom5       = (current_price - p5ago) / p5ago * 100
            prev_close = float(close_ser.iloc[-2]) if len(close_ser) > 1 else current_price
            daily_pct  = (current_price - prev_close) / prev_close * 100

            asset_type = ASSET_TYPE.get(symbol, "large")
            tier_label = "[LARGE]" if asset_type == "large" else "[MID]"

            # Build human-readable score breakdown for Claude
            mom5_val = float(last_mom5[symbol]) if symbol in last_mom5.index and not pd.isna(last_mom5[symbol]) else 0.0
            reasons = []
            if vol_ratio >= CONFIG["VOLUME_SURGE_FACTOR"]:
                reasons.append(f"Volume {vol_ratio:.1f}× 20-day avg (bullish candle, close>open) +35pts")
            else:
                reasons.append(f"Volume {vol_ratio:.1f}× avg — below 1.5× surge threshold, no conviction pts")
            if CONFIG["RSI_BUY_THRESHOLD"] <= rsi <= CONFIG["RSI_OVERBOUGHT"]:
                reasons.append(f"RSI {rsi:.1f} in momentum zone ({CONFIG['RSI_BUY_THRESHOLD']}–{CONFIG['RSI_OVERBOUGHT']}) +30pts")
            else:
                reasons.append(f"RSI {rsi:.1f} outside momentum zone ({CONFIG['RSI_BUY_THRESHOLD']}–{CONFIG['RSI_OVERBOUGHT']}) — no RSI pts")
            if current_price > sma_20:
                reasons.append(f"Price ${current_price:.2f} above 20-MA (${sma_20:.2f}) +25pts")
            else:
                reasons.append(f"Price ${current_price:.2f} below 20-MA (${sma_20:.2f}) — no direction pts")
            if mom5_val > 2.0:
                reasons.append(f"5-day momentum +{mom5_val:.1f}% (above +2% threshold) +10pts")
            else:
                reasons.append(f"5-day momentum {mom5_val:+.1f}% — below +2% threshold, no velocity pts")

            candidate = {
                "symbol":          symbol,
                "score":           score,
                "rsi":             rsi,
                "current_price":   current_price,
                "sma_20":          sma_20,
                "volume_ratio":    vol_ratio,
                "momentum_5d_pct": mom5,
                "daily_pct":       round(daily_pct, 2),
                "reasons":         reasons,
                "buy_signal":      True,
            }

            news_hours = 48 if asset_type == "large" else 72
            news       = self.market.get_news(symbol, hours=news_hours, limit=5)
            sym_params = get_symbol_params(symbol)
            analysis   = self.claude.analyze(
                symbol, candidate, news=news,
                sym_params=sym_params,
                previous_context=self.previous_context
            )
            approved = analysis.get("approve") and analysis.get("confidence", 0) >= 7
            can_buy  = (current_count + bought_this_scan) < max_positions

            self.logger.log_scan_result(
                symbol=symbol, score=score, rsi=rsi,
                volume_ratio=vol_ratio, tech_signal=True,
                sent_to_claude=True, claude_approved=approved,
                claude_reason=analysis.get("analysis", "—") + (
                    "" if can_buy else "\n[Positions full — analysis only, no trade opened]"
                ),
                confidence=analysis.get("confidence", 0), risk="", key_signal="",
                reasons=reasons, momentum_5d_pct=mom5,
                current_price=current_price
            )

            action = ("✅ BUY" if (approved and can_buy)
                      else ("🟡 SKIP (positions full)" if approved else "❌ SKIP"))
            self._run_decisions.append({
                "time":       datetime.now().strftime("%H:%M"),
                "symbol":     f"{symbol} {tier_label}",
                "action":     action,
                "confidence": f"{analysis.get('confidence', '?')}/10",
                "reason":     analysis.get("analysis", "—")
            })

            if approved and can_buy:
                self._open_position(symbol, current_price, analysis, daily_pct=daily_pct)
                bought_this_scan += 1

        # ── Step 7: Hold reviews for open positions ───────────────────────
        # Reuse batch data — no extra API call needed
        pos_map = {p["symbol"]: p for p in open_positions}
        for symbol in open_symbols:
            if symbol not in close_all.columns:
                log.warning(f"⚠️ {symbol} not in batch data — skipping hold review")
                continue
            pos_data = pos_map.get(symbol)
            if not pos_data:
                continue

            close_ser = close_all[symbol].dropna()
            if close_ser.empty:
                continue

            current_price = float(close_ser.iloc[-1])
            rsi_hold      = (float(last_rsi[symbol])
                             if symbol in last_rsi.index and not pd.isna(last_rsi[symbol])
                             else TechnicalAnalysis.calculate_rsi(close_ser))
            vol_hold      = (float(last_vol_ratio[symbol])
                             if symbol in last_vol_ratio.index and not pd.isna(last_vol_ratio[symbol])
                             else 0.0)
            p5ago         = float(close_ser.iloc[-6]) if len(close_ser) > 5 else current_price
            mom5          = (current_price - p5ago) / p5ago * 100
            sma20_val     = float(last_ma20[symbol]) if not pd.isna(last_ma20[symbol]) else current_price
            score_hold    = int(score_all[symbol]) if symbol in score_all.index else 0

            tech_for_hold = {
                "symbol":          symbol,
                "score":           score_hold,
                "rsi":             rsi_hold,
                "current_price":   current_price,
                "sma_20":          sma20_val,
                "volume_ratio":    vol_hold,
                "momentum_5d_pct": mom5,
                "reasons":         [],
                "buy_signal":      False,
            }

            log.info(f"🔍 Hold review: {symbol} [{ASSET_TYPE.get(symbol, 'large').upper()}]...")
            asset_type  = ASSET_TYPE.get(symbol, "large")
            news_hours  = 48 if asset_type == "large" else 72
            news        = self.market.get_news(symbol, hours=news_hours, limit=3)
            hold_result = self.claude.review_hold(
                symbol, pos_data, tech_for_hold, news=news,
                previous_context=self.previous_context
            )
            self.logger.log_scan_result(
                symbol=symbol, score=score_hold, rsi=rsi_hold,
                volume_ratio=vol_hold, tech_signal=False,
                sent_to_claude=True, hold_review=True,
                claude_approved=hold_result.get("hold", True),
                claude_reason=hold_result.get("analysis", "—"),
                confidence=hold_result.get("confidence", 0),
                reasons=[], momentum_5d_pct=mom5,
                current_price=current_price
            )

    def _open_position(self, symbol: str, price: float, analysis: dict, daily_pct: float = 0.0):
        """Submit BUY order to Alpaca, record locally, append to run decisions."""
        params = get_symbol_params(symbol)
        tier   = ASSET_TYPE.get(symbol, "large").upper()

        # ── Use actual buying power, not static capital figure ────────────
        intended_size    = CONFIG["TOTAL_CAPITAL"] * params["position_pct"]
        actual_bp        = self.logger.data.get("buying_power", CONFIG["TOTAL_CAPITAL"])
        position_size_usd = min(intended_size, actual_bp * 0.95)  # 95% to leave a buffer
        if position_size_usd < 100:
            log.warning(f"⚠️ Insufficient buying power (${actual_bp:,.0f}) for {symbol} — skipping")
            return

        fill_price = price
        shares     = round(position_size_usd / price, 4)

        if self.trading:
            try:
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums   import OrderSide, TimeInForce
                order = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        notional=round(position_size_usd, 2),
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY
                    )
                )
                log.info(f"📤 BUY submitted: {symbol} notional=${position_size_usd:,.2f} (order {order.id})")
                filled = self._wait_for_fill(order.id, timeout=60)
                if filled:
                    fill_price = float(filled.filled_avg_price)
                    shares     = float(filled.filled_qty)
                    log.info(f"✅ BUY filled: {symbol} x{shares} @ ${fill_price:.2f}")
                else:
                    log.warning(f"⚠️ Fill not confirmed for {symbol} — using scan price")
            except Exception as e:
                log.error(f"❌ BUY order failed for {symbol}: {e}")
                return

        stop_loss        = round(fill_price * (1 - params["stop_loss_pct"]), 2)
        take_profit      = round(fill_price * (1 + params["take_profit_pct"]), 2)
        # Actual cost uses real fill price × real shares (not planned notional)
        actual_cost_usd  = round(fill_price * shares, 2)

        pos = PaperPosition(
            symbol=symbol,
            entry_price=fill_price,
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            shares=shares,
            cost_usd=actual_cost_usd,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            daily_pct_at_entry=round(daily_pct, 2),
        )
        self.logger.add_position(pos)
        mode = "LIVE" if not CONFIG.get("PAPER_TRADING", True) else "Paper"
        log.info(f"🛒 [{mode}] Bought {symbol} x{shares:.4f} @ ${fill_price:.2f} | SL ${stop_loss:.2f} | TP ${take_profit:.2f}")
        log.info(f"   Claude: {analysis.get('analysis', '')[:120]}")

        # ── Submit native GTC hard stop + GTC limit (take profit) ─────────
        # Both orders live on Alpaca's servers independently of the bot.
        # Whichever triggers first closes the position; we cancel the other
        # in _close_position() / _sync_with_alpaca() to prevent orphan orders.
        if self.trading and shares > 0:
            # Hard stop
            try:
                from alpaca.trading.requests import StopOrderRequest
                from alpaca.trading.enums   import OrderSide, TimeInForce
                stop_order = self.trading.submit_order(
                    StopOrderRequest(
                        symbol=symbol,
                        qty=round(float(shares), 4),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        stop_price=stop_loss,
                    )
                )
                stop_order_id = str(stop_order.id)
                self.logger.update_position(symbol, {"stop_order_id": stop_order_id})
                log.info(f"🛡️ Native GTC stop placed: {symbol} @ ${stop_loss:.2f} (order {stop_order_id})")
            except Exception as e:
                log.warning(
                    f"⚠️ Native stop order failed for {symbol}: {e} — "
                    f"Python fallback active (bot must be running to enforce stop)"
                )

            # Fixed take profit (limit order)
            try:
                from alpaca.trading.requests import LimitOrderRequest
                from alpaca.trading.enums   import OrderSide, TimeInForce
                tp_order = self.trading.submit_order(
                    LimitOrderRequest(
                        symbol=symbol,
                        qty=round(float(shares), 4),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        limit_price=take_profit,
                    )
                )
                tp_order_id = str(tp_order.id)
                self.logger.update_position(symbol, {"take_profit_order_id": tp_order_id})
                log.info(f"🎯 Native GTC take profit placed: {symbol} @ ${take_profit:.2f} +{params['take_profit_pct']*100:.1f}% [{tier}] (order {tp_order_id})")
            except Exception as e:
                log.warning(
                    f"⚠️ Native take profit order failed for {symbol}: {e} — "
                    f"Python fallback active (bot must be running to enforce take profit)"
                )

    def _wait_for_fill(self, order_id: str, timeout: int = 60):
        """Poll Alpaca until filled, cancelled, expired, or rejected."""
        from alpaca.trading.enums import OrderStatus
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                order = self.trading.get_order_by_id(str(order_id))
                if order.status == OrderStatus.FILLED:
                    return order
                if order.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
                    log.warning(f"⚠️ Order {order_id} ended: {order.status}")
                    return None
            except Exception as e:
                log.warning(f"⚠️ Order poll error (id={order_id}): {e}")
            time.sleep(3)
        log.warning(f"⚠️ Order {order_id} not confirmed within {timeout}s — using scan price as fallback")
        return None

    def _sync_with_alpaca(self):
        """Reconcile local JSON with actual Alpaca account on startup."""
        if not self.trading:
            log.warning("⚠️ Trading client unavailable — skipping Alpaca sync")
            return
        try:
            alpaca_positions = self.trading.get_all_positions()
        except Exception as e:
            log.error(f"❌ Alpaca sync failed: {e}")
            return

        alpaca_map = {p.symbol: p for p in alpaca_positions}
        local_map  = {p["symbol"]: p for p in self.logger.get_open_positions()}
        added = closed = updated = 0

        # Case 1: on Alpaca but missing locally
        for symbol, ap in alpaca_map.items():
            if symbol not in local_map:
                entry_price = float(ap.avg_entry_price)
                shares      = float(ap.qty)
                sym_params  = get_symbol_params(symbol)
                stop_price  = round(entry_price * (1 - sym_params["stop_loss_pct"]), 2)
                pos = PaperPosition(
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_date=datetime.now().strftime("%Y-%m-%d"),
                    shares=shares,
                    cost_usd=entry_price * shares,
                    stop_loss_price=stop_price,
                    take_profit_price=round(entry_price * (1 + sym_params["take_profit_pct"]), 2),
                )
                self.logger.add_position(pos)
                log.info(f"🔄 Sync [added]   {symbol} x{shares} @ ${entry_price:.2f}")
                added += 1

                # Bot may have crashed after BUY but before placing the stop order.
                # Submit a native stop now so this position is protected immediately.
                try:
                    from alpaca.trading.requests import StopOrderRequest
                    from alpaca.trading.enums   import OrderSide, TimeInForce
                    stop_ord = self.trading.submit_order(
                        StopOrderRequest(
                            symbol=symbol,
                            qty=round(shares, 4),
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC,
                            stop_price=stop_price,
                        )
                    )
                    orphan_stop_id = str(stop_ord.id)
                    self.logger.update_position(symbol, {"stop_order_id": orphan_stop_id})
                    log.info(
                        f"🛡️ Sync: native stop placed for {symbol} @ "
                        f"${stop_price:.2f} (order {orphan_stop_id})"
                    )
                except Exception as e:
                    log.warning(
                        f"⚠️ Sync: could not place stop for {symbol}: {e} — "
                        f"Python fallback active"
                    )

        # Case 2: in local JSON but gone from Alpaca — closed by stop or take profit
        for symbol, lp in local_map.items():
            if symbol not in alpaca_map:
                # Try to get the actual fill price from Alpaca's closed order history
                exit_price = lp.get("last_price") or lp["entry_price"]
                exit_reason = "Sync: closed by Alpaca (native order)"
                try:
                    from alpaca.trading.requests import GetOrdersRequest
                    from alpaca.trading.enums   import QueryOrderStatus, OrderSide, OrderStatus
                    orders = self.trading.get_orders(filter=GetOrdersRequest(
                        status=QueryOrderStatus.CLOSED,
                        symbols=[symbol],
                        limit=10
                    ))
                    for order in sorted(
                        orders,
                        key=lambda o: o.filled_at or o.updated_at or datetime.min,
                        reverse=True
                    ):
                        if (order.side == OrderSide.SELL and
                                order.status == OrderStatus.FILLED and
                                order.filled_avg_price):
                            exit_price  = float(order.filled_avg_price)
                            exit_reason = f"Sync: filled by Alpaca native order @ ${exit_price:.2f}"
                            log.info(f"🔄 Sync: actual fill price found for {symbol}: ${exit_price:.2f}")
                            break
                except Exception as e:
                    log.warning(f"⚠️ Sync: could not fetch order history for {symbol}: {e}")

                # Cancel any remaining open sell orders for this symbol (orphan prevention)
                if self.trading:
                    try:
                        open_orders = self.trading.get_orders()
                        for order in open_orders:
                            if str(order.symbol) == symbol:
                                self.trading.cancel_order_by_id(str(order.id))
                                log.info(f"🗑️ Sync: cancelled orphan order {order.id} for {symbol}")
                    except Exception as e:
                        log.warning(f"⚠️ Sync: could not clean up orders for {symbol}: {e}")

                shares  = lp.get("shares", 0)
                pnl_usd = (exit_price - lp["entry_price"]) * shares
                self.logger.update_position(symbol, {
                    "status":      "closed",
                    "exit_price":  round(exit_price, 2),
                    "exit_date":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "exit_reason": exit_reason,
                    "pnl_usd":     round(pnl_usd, 2),
                })
                log.warning(f"🔄 Sync [closed]  {symbol} — gone from Alpaca (P&L ${pnl_usd:+,.2f})")
                closed += 1

        # Case 3: in both — refresh from Alpaca
        for symbol, ap in alpaca_map.items():
            if symbol in local_map:
                self.logger.update_position(symbol, {
                    "shares":       float(ap.qty),
                    "last_price":   float(ap.current_price),
                    "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
                updated += 1

        log.info(f"✅ Alpaca sync — added {added} | closed {closed} | updated {updated}")

        # Sync cash balance
        try:
            account = self.trading.get_account()
            self.logger.data["cash_available"]  = round(float(account.cash), 2)
            self.logger.data["portfolio_value"] = round(float(account.portfolio_value), 2)
            self.logger.data["buying_power"]    = round(float(account.buying_power), 2)
            self.logger.save()
            log.info(f"💰 Account — cash ${float(account.cash):,.2f} | portfolio ${float(account.portfolio_value):,.2f}")
        except Exception as e:
            log.warning(f"⚠️ Account balance sync failed: {e}")

    def _refresh_account_balance(self):
        """Refresh cash & portfolio value from Alpaca."""
        if not self.trading:
            return
        try:
            account = self.trading.get_account()
            self.logger.data["cash_available"]  = round(float(account.cash), 2)
            self.logger.data["portfolio_value"] = round(float(account.portfolio_value), 2)
            self.logger.data["buying_power"]    = round(float(account.buying_power), 2)
        except Exception as e:
            log.warning(f"⚠️ Account balance refresh failed: {e}")

    def _update_spy_benchmark(self):
        """Track SPY as the 2-week benchmark."""
        spy_price = self.market.get_current_price("SPY")
        if not spy_price:
            return
        if "benchmark" not in self.logger.data:
            self.logger.data["benchmark"] = {
                "symbol":        "SPY",
                "start_date":    datetime.now().strftime("%Y-%m-%d"),
                "start_price":   spy_price,
                "current_price": spy_price,
                "goal_days":     14
            }
            log.info(f"📌 SPY benchmark set: ${spy_price:.2f} on {self.logger.data['benchmark']['start_date']}")
        else:
            self.logger.data["benchmark"]["current_price"] = spy_price
        self.logger.save()

    def send_run_summary(self):
        """
        Send a LINE message summarising this single run.

        Time-gate (Asia/Taipei):
          The bot runs 4× daily during the US session, which spans roughly
          21:30–04:00 TW time.  To avoid waking anyone up, LINE notifications
          are suppressed on all runs whose local TW clock is before 03:45.
          Only the final run of the session (≥ 03:45 TW) fires the message,
          delivering one clean end-of-session summary per day.
        """
        import pytz
        tz     = pytz.timezone("Asia/Taipei")
        now_tw = datetime.now(tz)
        gate_open  = now_tw.replace(hour=3, minute=45, second=0, microsecond=0)
        gate_close = now_tw.replace(hour=4, minute=30, second=0, microsecond=0)
        if not (gate_open <= now_tw <= gate_close):
            log.info(
                f"🔕 LINE notification suppressed — TW time is "
                f"{now_tw.strftime('%H:%M')} (outside 03:45–04:30 window). "
                f"Only the pre-close run fires LINE."
            )
            return

        if not CONFIG.get("LINE_CHANNEL_ACCESS_TOKEN") or not CONFIG.get("LINE_USER_IDS"):
            return

        open_pos  = self.logger.get_open_positions()
        today_pnl = self.logger.get_today_pnl()
        total_pnl = self.logger.get_total_pnl()
        capital   = CONFIG["TOTAL_CAPITAL"]
        buys      = [d for d in self._run_decisions if "BUY" in d.get("action", "") and "SKIP" not in d.get("action", "")]
        sells     = [d for d in self._run_decisions if "SELL" in d.get("action", "")]

        if buys:
            buy_lines = "\n💰 Bought:\n" + "\n".join(f"  • {d['symbol']}" for d in buys)
        else:
            buy_lines = "\n💰 Bought: none"

        if sells:
            sell_lines = "\n📤 Sold:\n" + "\n".join(f"  • {d['symbol']} — {d['reason']}" for d in sells)
        else:
            sell_lines = "\n📤 Sold: none"

        message = (
            f"🤖 Amara — Run Complete\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"\n💵 Today's P&L: ${today_pnl:+,.2f} USD"
            f"\n📈 Total P&L:   ${total_pnl:+,.2f} ({total_pnl/capital*100:+.2f}%)"
            f"\n📋 Positions:   {len(open_pos)}/{int(1/CONFIG['MAX_POSITION_PCT'])}"
            f"{buy_lines}{sell_lines}"
        )
        send_line_message(message)

    # ── Single-run execution pipeline ─────────────────────────────────────
    def run_once(self) -> bool:
        """
        Execute one complete scan cycle from top to bottom and return.
        Returns False if the total loss limit has been hit (caller should exit).
        """
        now = datetime.now()
        log.info(f"\n{'='*55}")
        log.info(f"⏰ Amara run: {now.strftime('%Y-%m-%d %H:%M')}")

        if not self.check_total_limits():
            log.critical("⛔ Total loss limit exceeded — Amara stopping.")
            return False

        market_open = self.is_market_open()
        if not market_open:
            log.info("🌙 US market is closed — skipping new trade scan.")
            log.info("   Monitoring existing positions, refreshing balances, and updating dashboard.")

        if not market_open or not self.check_daily_limits():
            if market_open:
                log.info("⏸️ Daily loss limit active — monitoring only, no new trades.")

        self._update_spy_benchmark()
        self._refresh_account_balance()
        self.check_existing_positions()

        if market_open and not self.daily_stopped:
            self.scan_for_opportunities()
        elif not market_open:
            log.info("🔍 Scan skipped — market closed.")

        open_pos  = self.logger.get_open_positions()
        today_pnl = self.logger.get_today_pnl()
        total_pnl = self.logger.get_total_pnl()
        log.info(
            f"\n📊 Run complete — "
            f"positions: {len(open_pos)} | "
            f"today P&L: ${today_pnl:+,.2f} | "
            f"total P&L: ${total_pnl:+,.2f}"
        )
        return True


# ─────────────────────────────────────────────
# ENTRY POINT — single-run serverless pipeline
# ─────────────────────────────────────────────
if __name__ == "__main__":

    log.info("=" * 55)
    log.info("🤖  AMARA — serverless single-run mode")
    log.info("=" * 55)

    # ── Step 1: Load prior-run context from amara_dashboard.md ──────────────
    previous_context = read_previous_dashboard()

    # ── Step 2: Initialise bot (Alpaca sync, Claude connect) ────────────────
    bot = AmaraBot(previous_context=previous_context)

    # ── Step 3: Execute one complete scan cycle ──────────────────────────────
    ok = bot.run_once()

    # ── Step 4: Write updated dashboards ─────────────────────────────────────
    write_amara_dashboard(bot)
    write_dashboard_html(bot)

    # ── Step 5: Send LINE summary ─────────────────────────────────────────────
    bot.send_run_summary()

    if not ok:
        log.critical("⛔ Amara exited due to total loss limit. Review strategy before restarting.")
    else:
        log.info("✅ Amara run complete — exiting cleanly.")
