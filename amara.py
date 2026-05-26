"""
Amara — Serverless Single-Run Trading Bot
==========================================
策略：大型股＋中型股動能交易，技術面分析
模式：紙上交易（不使用真實資金）
執行：單次執行（適合 Claude Code routines / cron / 任何排程器）

需要安裝：
    pip install alpaca-py anthropic pandas ta requests python-dotenv pytz

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
DASHBOARD_PATH = dashboard_path = "amara_dashboard.md"

from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# Load secrets from secrets.env (must be in the same folder as this script)
load_dotenv(os.path.join(_SCRIPT_DIR, "secrets.env"))

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
    "MAX_HOLD_DAYS": 5,
    "DAILY_LOSS_LIMIT_PCT": 0.05,
    "TOTAL_LOSS_LIMIT_PCT": 0.30,

    # ── Dual-tier risk profiles ──────────────────────────────────────────
    # Large-caps: tighter stop, earlier exit — lower volatility tolerance
    "LARGE_CAP_STOP_LOSS_PCT":  0.035,  # Hard stop-loss  -3.5%
    "LARGE_CAP_TRAILING_PCT":   0.080,  # Trailing trigger +8.0%
    "LARGE_CAP_POSITION_PCT":   0.10,   # 10% of capital per position

    # Mid-caps: wider stop, higher target — accommodates larger swings
    "MID_CAP_STOP_LOSS_PCT":    0.050,  # Hard stop-loss  -5.0%
    "MID_CAP_TRAILING_PCT":     0.110,  # Trailing trigger +11.0%
    "MID_CAP_POSITION_PCT":     0.06,   # 6% of capital per position

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
            "stop_loss_pct":   CONFIG["MID_CAP_STOP_LOSS_PCT"],   # -5.0%
            "take_profit_pct": CONFIG["MID_CAP_TRAILING_PCT"],    # +11.0% trailing trigger
            "position_pct":    CONFIG["MID_CAP_POSITION_PCT"],    # 6% of capital
        }
    # Default to large-cap profile (also covers any unlisted symbol)
    return {
        "stop_loss_pct":   CONFIG["LARGE_CAP_STOP_LOSS_PCT"],     # -3.5%
        "take_profit_pct": CONFIG["LARGE_CAP_TRAILING_PCT"],      # +8.0% trailing trigger
        "position_pct":    CONFIG["LARGE_CAP_POSITION_PCT"],      # 10% of capital
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
    # Set to True once the +N% trailing trigger has fired and a GTC trailing
    # stop order has been submitted to Alpaca.  Prevents re-submission on
    # subsequent hourly runs and suppresses the hard-stop check (Alpaca owns
    # the exit from this point forward).
    trailing_stop_active: bool = False

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
    # Ensure the Google Drive folder exists (it should, but guard for first run)
    os.makedirs(os.path.dirname(dashboard_path), exist_ok=True)
    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"📊 amara_dashboard.md written → {dashboard_path} ({len(lines)} lines)")


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

    def get_bars_batch(self, symbols: list, days: int = 220) -> Optional[pd.DataFrame]:
        """
        Fetch daily OHLCV bars for ALL symbols in a single Alpaca API call.

        Returns a MultiIndex DataFrame with (symbol, timestamp) as the index,
        or None on failure.  days=220 ensures enough history for the 200-day MA.
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
        return market_open <= now_et <= market_close

    # ── Risk limits ────────────────────────────────────────────────────────
    def check_daily_limits(self) -> bool:
        today_pnl   = self.logger.get_today_pnl()
        daily_limit = -CONFIG["TOTAL_CAPITAL"] * CONFIG["DAILY_LOSS_LIMIT_PCT"]
        if today_pnl <= daily_limit:
            log.warning(f"🚨 Daily loss limit hit — ${abs(today_pnl):,.2f} USD lost today. No new trades.")
            self.daily_stopped = True
            return False
        return True

    def check_total_limits(self) -> bool:
        total_pnl   = self.logger.get_total_pnl()
        total_limit = -CONFIG["TOTAL_CAPITAL"] * CONFIG["TOTAL_LOSS_LIMIT_PCT"]
        if total_pnl <= total_limit:
            log.critical(f"🚨🚨 Total loss limit hit — ${abs(total_pnl):,.2f} USD. Bot halted.")
            return False
        return True

    # ── Position monitoring ────────────────────────────────────────────────
    def check_existing_positions(self):
        positions = self.logger.get_open_positions()
        if not positions:
            return
        log.info(f"📋 Checking {len(positions)} open position(s)...")
        for pos in positions:
            symbol           = pos["symbol"]
            current_price    = self.market.get_current_price(symbol)
            if not current_price:
                continue

            entry_price      = pos["entry_price"]
            pnl_pct          = (current_price - entry_price) / entry_price
            trailing_active  = pos.get("trailing_stop_active", False)

            entry_date = datetime.strptime(pos["entry_date"], "%Y-%m-%d")
            hold_days  = (datetime.now() - entry_date).days

            if trailing_active:
                # Alpaca owns the exit via a live GTC trailing-stop order.
                # We monitor-only: log the current state and respect max hold days,
                # but do NOT re-submit stop orders or check the original fixed floor.
                log.info(
                    f"  {symbol} 🎯 trailing: ${current_price:.2f} | "
                    f"{pnl_pct*100:+.1f}% | {hold_days}d held | Alpaca managing exit"
                )
                if hold_days >= CONFIG["MAX_HOLD_DAYS"]:
                    self._close_position(pos, current_price, f"Max hold days ({hold_days}d) — overriding trailing stop")
                    continue
                self.logger.update_position(symbol, {
                    "last_price":   round(current_price, 2),
                    "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
                continue

            # ── Fixed-floor phase (trailing not yet active) ───────────────
            if current_price <= pos["stop_loss_price"]:
                params = get_symbol_params(symbol)
                tier   = ASSET_TYPE.get(symbol, "large").upper()
                self._close_position(
                    pos, current_price,
                    f"Hard stop-loss -{params['stop_loss_pct']*100:.1f}% [{tier}]"
                )
                continue

            if current_price >= pos["take_profit_price"]:
                # Profit milestone crossed → hand exit management to Alpaca
                # via a GTC trailing-stop order.  Do NOT close immediately.
                self._activate_trailing_stop(pos, current_price)
                continue

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

        pnl_pct = (fill_price - pos["entry_price"]) / pos["entry_price"]
        pnl_usd = pnl_pct * pos["cost_usd"]
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
        Dual-tier scan across 150 symbols (50 large-cap + 100 mid-cap).

        Pipeline:
          1. Single batch Alpaca request → MultiIndex DataFrame (symbol × timestamp)
          2. Vectorized 20-MA and 200-MA across all symbols simultaneously
          3. Filter: Price > 20-MA > 200-MA (trend structure confirmed)
          4. Momentum score: ((Close − 20-MA) / 20-MA) × 100  → rank descending
          5. Top 5 fresh candidates → news fetch → Claude.analyze()
          6. Hold reviews for open positions using the same batch data
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
        log.info(f"🔍 Batch-fetching bars for {len(WATCHLIST)} symbols (220-day window for 20-MA / 200-MA)...")
        raw = self.market.get_bars_batch(WATCHLIST, days=220)
        if raw is None or raw.empty:
            log.warning("⚠️ Batch bar fetch returned no data — skipping scan this run")
            return

        # ── Step 2: Vectorised MA calculation ────────────────────────────
        # raw MultiIndex is (symbol, timestamp); unstack symbol → columns
        # Result: rows=timestamps, columns=symbols
        close_all  = raw["close"].unstack(level=0)
        volume_all = raw["volume"].unstack(level=0)

        ma20_all  = close_all.rolling(20).mean()
        ma200_all = close_all.rolling(200).mean()

        # Latest values (most recent trading day)
        last_close  = close_all.iloc[-1]
        last_ma20   = ma20_all.iloc[-1]
        last_ma200  = ma200_all.iloc[-1]

        # ── Step 3: Price > 20-MA > 200-MA filter ────────────────────────
        # Drop any symbol where MAs couldn't be computed (insufficient history)
        valid = last_close.notna() & last_ma20.notna() & last_ma200.notna()
        mask  = valid & (last_close > last_ma20) & (last_ma20 > last_ma200)
        passing = mask[mask].index.tolist()
        log.info(f"   Filter passed: {len(passing)}/{len(WATCHLIST)} symbols satisfy Price > 20-MA > 200-MA")

        # ── Step 4: Momentum score & ranking ─────────────────────────────
        # Score = % price is above its 20-MA  (higher → stronger near-term momentum)
        raw_scores     = ((last_close - last_ma20) / last_ma20 * 100)[passing]
        ranked_scores  = raw_scores.sort_values(ascending=False)

        # Exclude symbols already in open positions from new-entry candidates
        fresh_ranked = ranked_scores.drop(
            index=[s for s in open_symbols if s in ranked_scores.index],
            errors="ignore"
        )
        top_n  = CONFIG.get("TOP_CANDIDATES", 5)
        top5   = fresh_ranked.head(top_n).index.tolist()

        log.info(f"   Top {top_n} momentum candidates → Claude: {', '.join(top5) if top5 else 'none'}")

        # ── Step 5: Claude analysis for top 5 only ───────────────────────
        bought_this_scan = 0
        for symbol in top5:
            raw_score  = float(ranked_scores[symbol])
            # Normalise to 0-100 for the Claude prompt (12.5% above MA → 100)
            norm_score = int(min(100, raw_score * 8))

            # --- Safe MultiIndex cross-section slicing ---
            # top5 comes from ranked_scores which was built from last_close, so the
            # symbol should always be present — but a delayed/partial batch response
            # could drop it, so guard explicitly to avoid KeyError.
            if symbol in close_all.columns:
                close_ser = close_all[symbol].dropna()
            else:
                log.warning(f"⚠️ {symbol} missing from batch close data — skipping")
                continue

            vol_ser = (volume_all[symbol].dropna()
                       if symbol in volume_all.columns else pd.Series(dtype=float))
            # ----------------------------------------------

            current_price = float(close_ser.iloc[-1])
            sma_20        = float(last_ma20[symbol])
            sma_200       = float(last_ma200[symbol])
            rsi           = TechnicalAnalysis.calculate_rsi(close_ser)
            vol_ratio     = (TechnicalAnalysis.volume_surge(vol_ser)
                             if not vol_ser.empty else 0.0)

            # 5-day momentum and daily change
            p5ago     = float(close_ser.iloc[-6]) if len(close_ser) > 5 else current_price
            mom5      = (current_price - p5ago) / p5ago * 100
            prev_close = float(close_ser.iloc[-2]) if len(close_ser) > 1 else current_price
            daily_pct  = (current_price - prev_close) / prev_close * 100

            asset_type = ASSET_TYPE.get(symbol, "large")
            tier_label = "[LARGE]" if asset_type == "large" else "[MID]"

            candidate = {
                "symbol":          symbol,
                "score":           norm_score,
                "rsi":             rsi,
                "current_price":   current_price,
                "sma_20":          sma_20,
                "volume_ratio":    vol_ratio,
                "momentum_5d_pct": mom5,
                "daily_pct":       round(daily_pct, 2),
                "reasons": [
                    f"Momentum {raw_score:.1f}% above 20-MA",
                    f"Trend confirmed: 20-MA (${sma_20:.2f}) > 200-MA (${sma_200:.2f})",
                ],
                "buy_signal": True,
            }

            news_hours = 48 if asset_type == "large" else 72
            news       = self.market.get_news(symbol, hours=news_hours, limit=5)
            sym_params = get_symbol_params(symbol)
            analysis   = self.claude.analyze(
                symbol, candidate, news=news,
                sym_params=sym_params,
                previous_context=self.previous_context
            )
            approved = analysis.get("approve") and analysis.get("confidence", 0) >= 6
            can_buy  = (current_count + bought_this_scan) < max_positions

            self.logger.log_scan_result(
                symbol=symbol, score=norm_score, rsi=rsi,
                volume_ratio=vol_ratio, tech_signal=True,
                sent_to_claude=True, claude_approved=approved,
                claude_reason=analysis.get("analysis", "—") + (
                    "" if can_buy else "\n[Positions full — analysis only, no trade opened]"
                ),
                confidence=analysis.get("confidence", 0), risk="", key_signal="",
                reasons=candidate["reasons"], momentum_5d_pct=mom5,
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

        # ── Step 6: Hold reviews for open positions ───────────────────────
        # Reuse batch data — no extra API call needed
        pos_map = {p["symbol"]: p for p in open_positions}
        for symbol in open_symbols:
            if symbol not in close_all.columns:
                log.warning(f"⚠️ {symbol} not in batch data — skipping hold review")
                continue
            pos_data = pos_map.get(symbol)
            if not pos_data:
                continue

            close_ser  = close_all[symbol].dropna()
            vol_ser    = (volume_all[symbol].dropna()
                          if symbol in volume_all.columns else pd.Series(dtype=float))

            current_price  = float(close_ser.iloc[-1])
            rsi            = TechnicalAnalysis.calculate_rsi(close_ser)
            vol_ratio      = (TechnicalAnalysis.volume_surge(vol_ser)
                              if not vol_ser.empty else 0.0)
            p5ago          = float(close_ser.iloc[-6]) if len(close_ser) > 5 else current_price
            mom5           = (current_price - p5ago) / p5ago * 100
            sma20_val      = float(close_ser.rolling(20).mean().iloc[-1]) if len(close_ser) >= 20 else current_price

            tech_for_hold = {
                "symbol":          symbol,
                "score":           0,
                "rsi":             rsi,
                "current_price":   current_price,
                "sma_20":          sma20_val,
                "volume_ratio":    vol_ratio,
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
                symbol=symbol, score=0, rsi=rsi,
                volume_ratio=vol_ratio, tech_signal=False,
                sent_to_claude=True, hold_review=True,
                claude_approved=hold_result.get("hold", True),
                claude_reason=hold_result.get("analysis", "—"),
                confidence=hold_result.get("confidence", 0),
                reasons=[], momentum_5d_pct=mom5,
                current_price=current_price
            )

    def _activate_trailing_stop(self, pos: dict, current_price: float):
        """
        Called when an open position's price crosses its trailing trigger
        threshold (e.g. +8% large-cap, +11% mid-cap).

        Instead of closing immediately, we submit a GTC trailing-stop SELL
        order to Alpaca so it manages the exit dynamically.  The local
        position is marked trailing_stop_active=True to prevent re-submission
        on subsequent hourly runs and to suppress the fixed hard-stop check.

        If the Alpaca order submission fails, we fall back to a standard
        market close so no trade is left unmanaged.
        """
        symbol    = pos["symbol"]
        params    = get_symbol_params(symbol)
        trail_pct = params["take_profit_pct"] * 100        # e.g. 11.0 for mid-caps
        tier      = ASSET_TYPE.get(symbol, "large").upper()
        shares    = pos.get("shares", 0)
        entry_pnl = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

        log.info(
            f"🎯 [{tier}] {symbol}: +{trail_pct:.1f}% trigger hit "
            f"@ ${current_price:.2f} ({entry_pnl:+.1f}% unrealised) — "
            f"submitting Alpaca {trail_pct:.1f}% trailing stop"
        )

        order_ok = False
        if self.trading and shares > 0:
            try:
                from alpaca.trading.requests import TrailingStopOrderRequest
                from alpaca.trading.enums   import OrderSide, TimeInForce
                order = self.trading.submit_order(
                    TrailingStopOrderRequest(
                        symbol=symbol,
                        qty=round(shares, 4),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,   # persists between sessions
                        trail_percent=round(trail_pct, 2),
                    )
                )
                log.info(
                    f"✅ GTC trailing stop submitted: {symbol} "
                    f"{trail_pct:.1f}% trail | order {order.id}"
                )
                order_ok = True
            except Exception as e:
                log.error(f"❌ Trailing stop order failed for {symbol}: {e} — falling back to market close")

        if not order_ok:
            # Fallback: close at market to avoid an unmanaged position
            self._close_position(
                pos, current_price,
                f"Trailing trigger +{trail_pct:.1f}% [{tier}] (Alpaca order failed — market close)"
            )
            return

        # Mark position as Alpaca-managed; persist activation metadata
        self.logger.update_position(symbol, {
            "trailing_stop_active":      True,
            "trailing_activated_price":  round(current_price, 2),
            "trailing_activated_date":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        self._run_decisions.append({
            "time":       datetime.now().strftime("%H:%M"),
            "symbol":     f"{symbol} [{tier}]",
            "action":     f"🎯 TRAILING STOP ACTIVATED (+{trail_pct:.1f}%)",
            "confidence": "—",
            "reason":     (
                f"Trigger hit at ${current_price:.2f} ({entry_pnl:+.1f}% unrealised). "
                f"Alpaca GTC trailing stop set at {trail_pct:.1f}% — fixed floor replaced."
            ),
        })

    def _open_position(self, symbol: str, price: float, analysis: dict, daily_pct: float = 0.0):
        """Submit BUY order to Alpaca, record locally, append to run decisions."""
        params           = get_symbol_params(symbol)
        position_size_usd = CONFIG["TOTAL_CAPITAL"] * params["position_pct"]
        fill_price       = price
        shares           = round(position_size_usd / price, 4)

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

        stop_loss   = fill_price * (1 - params["stop_loss_pct"])
        take_profit = fill_price * (1 + params["take_profit_pct"])

        pos = PaperPosition(
            symbol=symbol,
            entry_price=fill_price,
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            shares=shares,
            cost_usd=position_size_usd,
            stop_loss_price=round(stop_loss, 2),
            take_profit_price=round(take_profit, 2),
            daily_pct_at_entry=round(daily_pct, 2),
        )
        self.logger.add_position(pos)
        mode = "LIVE" if not CONFIG.get("PAPER_TRADING", True) else "Paper"
        log.info(f"🛒 [{mode}] Bought {symbol} x{shares:.4f} @ ${fill_price:.2f} | SL ${stop_loss:.2f} | TP ${take_profit:.2f}")
        log.info(f"   Claude: {analysis.get('analysis', '')[:120]}")

        # Record decision for dashboard (update the entry already added in scan_for_opportunities)
        # (The scan loop already added an entry; this is a no-op since we don't double-append)

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
                pos = PaperPosition(
                    symbol=symbol,
                    entry_price=entry_price,
                    entry_date=datetime.now().strftime("%Y-%m-%d"),
                    shares=shares,
                    cost_usd=entry_price * shares,
                    stop_loss_price=round(entry_price * (1 - sym_params["stop_loss_pct"]), 2),
                    take_profit_price=round(entry_price * (1 + sym_params["take_profit_pct"]), 2),
                )
                self.logger.add_position(pos)
                log.info(f"🔄 Sync [added]   {symbol} x{shares} @ ${entry_price:.2f}")
                added += 1

        # Case 2: in local JSON but gone from Alpaca
        for symbol, lp in local_map.items():
            if symbol not in alpaca_map:
                current_price = self.market.get_current_price(symbol) or lp["entry_price"]
                pnl_pct       = (current_price - lp["entry_price"]) / lp["entry_price"]
                pnl_usd       = pnl_pct * lp["cost_usd"]
                self.logger.update_position(symbol, {
                    "status":     "closed",
                    "exit_price": current_price,
                    "exit_date":  datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "exit_reason": "Sync: not found on Alpaca",
                    "pnl_usd":    round(pnl_usd, 2),
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
        gate   = now_tw.replace(hour=3, minute=45, second=0, microsecond=0)
        if now_tw < gate:
            log.info(
                f"🔕 LINE notification suppressed — TW time is "
                f"{now_tw.strftime('%H:%M')} (gate: 03:45). "
                f"Silent run — message will fire on the ≥03:45 pass."
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

    # ── Step 4: Write updated Markdown dashboard ─────────────────────────────
    write_amara_dashboard(bot)

    # ── Step 5: Send LINE summary ─────────────────────────────────────────────
    bot.send_run_summary()

    if not ok:
        log.critical("⛔ Amara exited due to total loss limit. Review strategy before restarting.")
    else:
        log.info("✅ Amara run complete — exiting cleanly.")
