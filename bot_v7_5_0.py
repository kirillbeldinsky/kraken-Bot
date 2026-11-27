import asyncio
import json
import logging
import os
import time
import uuid
from datetime import date
import requests
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler

# --- CONFIG ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", 0))
STATE_FILE = "bot_state.json"
TRADES_HISTORY_FILE = "trades_history.jsonl"

# --- НАСТРОЙКИ СИСТЕМЫ (v7.4.3-ANTI-IMPULSE) ---
VERSION = "v7.4.3-ANTI-IMPULSE"
SYMBOL = "BTC/USD"
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15                  # 15 минут
LIMIT = 100
PAPER_BALANCE_START = 1000.0

# --- РИСК И ПАРАМЕТРЫ СТРАТЕГИИ ---
RISK_PCT = 0.01                 # 1% риска на сделку
MAX_LEVERAGE = 1.0              # ограничение плеча
BB_PERIOD = 20
BB_STD = 2.2
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.8            # адаптивный стоп = 1.8 * ATR
TAKE_PROFIT_PCT = 0.015         # 1.5% тейк-профит

# --- КОМИССИИ И ЛИМИТНЫЕ ОРДЕРА ---
FEE_MAKER = 0.0016              # 0.16% Maker
FEE_TAKER = 0.0026              # 0.26% Taker
FEE_PCT = 0.0016                # базовая ставка комиссии при расчётах
LIMIT_ORDER_TTL_SEC = 300       # TTL неисполненного лимитного ордера (сек)
LIMIT_PRICE_OFFSET = 0.0

# --- ФИЛЬТРЫ И ЗАЩИТА (DAILY + CROSS-DAY CIRCUIT BREAKER) ---
MIN_ATR_PCT = 0.00045
TREND_FILTER = True
COOLDOWN_HOURS = 4
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_STOPS = 2       # дневной лимит стопов подряд
MAX_CROSS_DAY_STOPS = 3         # сквозной лимит стопов подряд через границу дней
CROSS_DAY_CB_HOURS = 12         # автосброс сквозного стоп-крана через 12 часов
MAX_DAILY_LOSS_PCT = 0.02

# --- ФИЛЬТР РАСШИРЕНИЯ ВОЛАТИЛЬНОСТИ (АНТИ-ИМПУЛЬС) ---
BB_EXPANSION_MAX = 1.35
IMPULSE_BAR_ATR_MAX = 2.2

# --- ДЕРИВАТИВНЫЕ ФИЛЬТРЫ (BINANCE FUTURES) ---
MAX_FUNDING_LONG = 0.0003
MIN_FUNDING_SHORT = -0.0003

bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
shutdown_event = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)


# --- СТЕЙТ И ИСТОРИЯ СДЕЛОК ---
def default_state() -> dict:
    return {
        "paper_balance": PAPER_BALANCE_START,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
        "open_position": None,
        "pending_order": None,
        "last_stop_time": 0,
        "last_exit_price": None,
        "last_exit_time": 0,
        "daily_trades": 0,
        "daily_pnl": 0.0,
        "daily_stop_streak": 0,
        "consecutive_stops": 0,
        "missed_limit_orders": 0,
        "blocked_impulse_signals": 0,
        "last_day": str(date.today()),
    }


def load_state(state_path: str = STATE_FILE) -> dict:
    state = default_state()
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                state.update(saved)
        except Exception as e:
            logging.error(f"Error reading state file {state_path}: {e}")
    return state


def save_state(state: dict, state_path: str = STATE_FILE) -> None:
    tmp_file = f"{state_path}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, state_path)
    except Exception as e:
        logging.error(f"Error saving state to {state_path}: {e}", exc_info=True)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except Exception:
            pass


def append_trade_history(
    stats: dict,
    pos: dict,
    exit_price: float,
    exit_reason: str,
    gross_pnl: float,
    fee_cost: float,
    net_pnl: float,
    exit_ts: float,
    history_path: str = TRADES_HISTORY_FILE,
) -> None:
    record = {
        "closed_at_ts": exit_ts,
        "closed_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(exit_ts)),
        "day": stats.get("last_day"),
        "side": pos.get("side"),
        "entry": pos.get("entry"),
        "exit": float(exit_price),
        "sl": pos.get("sl"),
        "tp": pos.get("tp"),
        "volume": pos.get("volume"),
        "reason": exit_reason,
        "gross_pnl": round(float(gross_pnl), 6),
        "fee_cost": round(float(fee_cost), 6),
        "net_pnl": round(float(net_pnl), 6),
        "balance_after": round(float(stats.get("paper_balance", PAPER_BALANCE_START)), 6),
        "daily_trades": stats.get("daily_trades", 0),
        "daily_pnl": round(float(stats.get("daily_pnl", 0.0)), 6),
        "daily_stop_streak": stats.get("daily_stop_streak", 0),
        "consecutive_stops": stats.get("consecutive_stops", 0),
    }
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        logging.error(f"Error appending trade history to {history_path}: {e}", exc_info=True)


def finalize_trade_close(
    stats: dict,
    pos: dict,
    exit_price: float,
    exit_reason: str,
    fee_rate: float = FEE_PCT,
    is_stop: bool = False,
) -> float:
    now_ts = time.time()
    today = str(date.today())
    if stats.get("last_day") != today:
        stats["last_day"] = today
        stats["daily_trades"] = 0
        stats["daily_pnl"] = 0.0
        stats["daily_stop_streak"] = 0

    qty = float(pos["volume"])
    entry = float(pos["entry"])
    side = pos["side"]
    gross_pnl = (float(exit_price) - entry) * qty if side == "buy" else (entry - float(exit_price)) * qty
    fee_cost = (entry + float(exit_price)) * qty * float(fee_rate)
    net_pnl = gross_pnl - fee_cost

    stats["paper_balance"] = float(stats.get("paper_balance", PAPER_BALANCE_START)) + net_pnl
    stats["total_trades"] = int(stats.get("total_trades", 0)) + 1
    stats["total_wins"] = int(stats.get("total_wins", 0)) + (1 if net_pnl > 0 else 0)
    stats["total_pnl"] = float(stats.get("total_pnl", 0.0)) + net_pnl
    stats["daily_pnl"] = float(stats.get("daily_pnl", 0.0)) + net_pnl
    stats["last_exit_price"] = float(exit_price)
    stats["last_exit_time"] = now_ts
    stats["open_position"] = None

    if is_stop:
        stats["last_stop_time"] = now_ts
        stats["daily_stop_streak"] = int(stats.get("daily_stop_streak", 0)) + 1
        stats["consecutive_stops"] = int(stats.get("consecutive_stops", 0)) + 1
    elif net_pnl > 0:
        stats["daily_stop_streak"] = 0
        stats["consecutive_stops"] = 0

    save_state(stats)
    append_trade_history(
        stats=stats,
        pos=pos,
        exit_price=float(exit_price),
        exit_reason=exit_reason,
        gross_pnl=gross_pnl,
        fee_cost=fee_cost,
        net_
