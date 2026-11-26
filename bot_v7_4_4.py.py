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
        net_pnl=net_pnl,
        exit_ts=now_ts,
    )
    return net_pnl


# --- TELEGRAM ---
async def send_telegram_async(text: str) -> None:
    if not bot or not CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Telegram send error: {e}")


def send_telegram(text: str) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(send_telegram_async(text))
    except RuntimeError:
        try:
            asyncio.run(send_telegram_async(text))
        except Exception as e:
            logging.error(f"send_telegram wrapper error: {e}")


async def stats_command(update: Update, context) -> None:
    stats = load_state()
    wr = (stats["total_wins"] / stats["total_trades"] * 100.0) if stats["total_trades"] > 0 else 0.0
    msg = (
        f"📊 *{VERSION} Stats*\n"
        f"Balance: `${stats['paper_balance']:.2f}`\n"
        f"Total PnL: `${stats['total_pnl']:+.2f}`\n"
        f"Trades: `{stats['total_trades']}` | Wins: `{stats['total_wins']}` ({wr:.1f}%)\n"
        f"Daily Trades: `{stats['daily_trades']}/{MAX_TRADES_PER_DAY}` | Daily PnL: `${stats['daily_pnl']:+.2f}`\n"
        f"Stop Streaks: day=`{stats['daily_stop_streak']}`, cross=`{stats['consecutive_stops']}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def pos_command(update: Update, context) -> None:
    stats = load_state()
    pos = stats.get("open_position")
    pending = stats.get("pending_order")
    if not pos and not pending:
        await update.message.reply_text("ℹ️ No open position or pending order")
        return
    parts = []
    if pos:
        parts.append(
            f"📌 *OPEN POSITION*\n"
            f"Side: `{pos['side'].upper()}` | Entry: `${pos['entry']:.2f}`\n"
            f"Vol: `{pos['volume']:.5f}` | SL: `${pos['sl']:.2f}` | TP: `${pos['tp']:.2f}`"
        )
    if pending:
        parts.append(
            f"⏳ *PENDING ORDER*\n"
            f"Side: `{pending['side'].upper()}` | Limit: `${pending['limit_price']:.2f}` | Vol: `{pending['volume']:.5f}`"
        )
    await update.message.reply_text("\n\n".join(parts), parse_mode="Markdown")


async def close_command(update: Update, context) -> None:
    try:
        logging.info("[CMD] /close received")
        stats = load_state()
        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("❌ No open position")
            return
        ticker = get_ticker()
        if not ticker:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            return
        exit_price = float(ticker["bid"] if pos["side"] == "buy" else ticker["ask"])
        pnl = finalize_trade_close(
            stats=stats,
            pos=pos,
            exit_price=exit_price,
            exit_reason="MANUAL CLOSE",
            fee_rate=FEE_PCT,
            is_stop=False,
        )
        msg = (
            f"🔴 *MANUAL CLOSE*\n"
            f"Price: `${exit_price:.2f}`\n"
            f"PnL: `${pnl:+.2f}`\n"
            f"Balance: `${stats['paper_balance']:.2f}`\n"
            f"Trades: `{stats['total_trades']}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        logging.info(f"[CMD] /close executed at {exit_price:.2f}, pnl={pnl:+.2f}")
    except Exception as e:
        logging.error(f"[ERROR] /close: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        except Exception:
            pass


async def shutdown_command(update: Update, context) -> None:
    try:
        logging.info("[CMD] /shutd received - initiating shutdown")
        await update.message.reply_text(
            "🛑 *Bot Shutdown Initiated*\nThe bot will stop in a few seconds...",
            parse_mode="Markdown",
        )
        global shutdown_event
        if shutdown_event:
            shutdown_event.set()
    except Exception as e:
        logging.error(f"[ERROR] /shutd: {e}", exc_info=True)


# --- KRAKEN & BINANCE PUBLIC API ---
def get_ohlc(symbol: str = KRAKEN_SYMBOL, interval: int = TIMEFRAME, limit: int = LIMIT) -> dict | None:
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            logging.error(f"Kraken API error: {data['error']}")
            return None
        result = data.get("result", {})
        pair_key = next((k for k in result.keys() if k != "last"), None)
        if not pair_key or not result.get(pair_key):
            return None
        ohlc_data = result[pair_key][-limit:]
        return {
            "times": [int(c[0]) for c in ohlc_data],
            "opens": [float(c[1]) for c in ohlc_data],
            "highs": [float(c[2]) for c in ohlc_data],
            "lows": [float(c[3]) for c in ohlc_data],
            "closes": [float(c[4]) for c in ohlc_data],
            "volumes": [float(c[6]) for c in ohlc_data],
        }
    except Exception as e:
        logging.error(f"Error fetching OHLC data: {e}")
        return None


def get_ticker(symbol: str = KRAKEN_SYMBOL) -> dict | None:
    try:
        resp = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={symbol}", timeout=10)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        pair_key = next((k for k in result.keys()), None)
        if not pair_key:
            return None
        ticker = result[pair_key]
        return {
            "bid": float(ticker["b"][0]),
            "ask": float(ticker["a"][0]),
            "last": float(ticker["c"][0]),
        }
    except Exception as e:
        logging.error(f"get_ticker error: {e}")
        return None


def get_derivatives_data(symbol: str = "BTCUSDT") -> dict:
    data = {"funding_rate": 0.0, "funding_rate_pct": 0.0, "open_interest": 0.0, "status": "ok"}
    try:
        f_res = requests.get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1", timeout=5)
        if f_res.status_code == 200:
            f_json = f_res.json()
            if f_json and isinstance(f_json, list):
                rate = float(f_json[0].get("fundingRate", 0.0))
                data["funding_rate"] = rate
                data["funding_rate_pct"] = rate * 100.0
        oi_res = requests.get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}", timeout=5)
        if oi_res.status_code == 200:
            data["open_interest"] = float(oi_res.json().get("openInterest", 0.0))
        return data
    except Exception as e:
        logging.warning(f"Derivatives API warning: {e}")
        data["status"] = "error"
        return data


# --- ИНДИКАТОРЫ ---
def calc_sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def get_bb(ohlc: dict, period: int = BB_PERIOD, num_std: float = BB_STD):
    closes = ohlc["closes"]
    vols = ohlc["volumes"]
    if len(closes) < period or len(vols) < period:
        return None
    window = closes[-period:]
    ma = sum(window) / period
    var = sum((x - ma) ** 2 for x in window) / period
    std = var ** 0.5
    upper = ma + num_std * std
    lower = ma - num_std * std
    avg_vol = sum(vols[-period:]) / period
    last_vol = vols[-1]
    return upper, ma, lower, avg_vol, last_vol


def get_vwap(ohlc: dict, period: int = BB_PERIOD) -> float | None:
    highs, lows, closes, vols = ohlc["highs"], ohlc["lows"], ohlc["closes"], ohlc["volumes"]
    if len(closes) < period:
        return None
    num = 0.0
    den = 0.0
    for h, l, c, v in zip(highs[-period:], lows[-period:], closes[-period:], vols[-period:]):
        tp = (h + l + c) / 3.0
        num += tp * v
        den += v
    return (num / den) if den > 0 else None


def get_atr(ohlc: dict, period: int = ATR_PERIOD) -> float | None:
    highs, lows, closes = ohlc["highs"], ohlc["lows"], ohlc["closes"]
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def get_trend(ohlc: dict) -> str:
    closes = ohlc["closes"]
    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)
    if not sma20 or not sma50:
        return "range"
    if sma20 < sma50 * 0.997:
        return "down"
    if sma20 > sma50 * 1.003:
        return "up"
    return "range"


def is_volatility_expanding(closed_ohlc: dict, atr: float) -> bool:
    closes = closed_ohlc["closes"]
    highs = closed_ohlc["highs"]
    lows = closed_ohlc["lows"]
    if len(closes) < BB_PERIOD + 10 or atr <= 0:
        return False

    last_range = highs[-1] - lows[-1]
    if last_range > IMPULSE_BAR_ATR_MAX * atr:
        return True

    widths = []
    for idx in range(len(closes) - 10, len(closes)):
        win = closes[idx - BB_PERIOD + 1 : idx + 1]
        if len(win) < BB_PERIOD:
            continue
        ma = sum(win) / BB_PERIOD
        std = (sum((x - ma) ** 2 for x in win) / BB_PERIOD) ** 0.5
        if ma > 0:
            widths.append((2.0 * BB_STD * std) / ma)

    if len(widths) < 2:
        return False
    avg_width = sum(widths[:-1]) / len(widths[:-1])
    return avg_width > 0 and (widths[-1] / avg_width) > BB_EXPANSION_MAX


# --- ОСНОВНОЙ ЦИКЛ ---
async def trading_loop() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("pos", pos_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("shutd", shutdown_command))

    async with app:
        await app.initialize()
        await app.start()
        logging.info("Bot updater polling started ✓")
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        send_telegram(f"🚀 *Bot started {VERSION}* | Pair: {SYMBOL}")

        try:
            while not shutdown_event.is_set():
                try:
                    stats = load_state()
                    now_ts = time.time()
                    today = str(date.today())

                    # 1. Сброс дневных счётчиков при смене календарного дня
                    if stats.get("last_day") != today:
                        stats["last_day"] = today
                        stats["daily_trades"] = 0
                        stats["daily_pnl"] = 0.0
                        stats["daily_stop_streak"] = 0
                        save_state(stats)

                    # 2. Проверка открытой позиции
                    if stats.get("open_position"):
                        pos = stats["open_position"]
                        ticker = get_ticker()
                        if not ticker:
                            await asyncio.sleep(5)
                            continue
                        bid, ask = float(ticker["bid"]), float(ticker["ask"])

                        if pos["side"] == "buy" and bid <= float(pos["sl"]):
                            pnl = finalize_trade_close(stats, pos, float(pos["sl"]), "SL CLOSE (LONG)", FEE_PCT, True)
                            send_telegram(f"🔴 *SL CLOSE (LONG)*\nPrice: `${pos['sl']:.2f}` | PnL: `${pnl:+.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                            continue
                        if pos["side"] == "buy" and ask >= float(pos["tp"]):
                            pnl = finalize_trade_close(stats, pos, float(pos["tp"]), "TP CLOSE (LONG)", FEE_PCT, False)
                            send_telegram(f"🟢 *TP CLOSE (LONG)*\nPrice: `${pos['tp']:.2f}` | PnL: `${pnl:+.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                            continue
                        if pos["side"] == "sell" and ask >= float(pos["sl"]):
                            pnl = finalize_trade_close(stats, pos, float(pos["sl"]), "SL CLOSE (SHORT)", FEE_PCT, True)
                            send_telegram(f"🔴 *SL CLOSE (SHORT)*\nPrice: `${pos['sl']:.2f}` | PnL: `${pnl:+.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                            continue
                        if pos["side"] == "sell" and bid <= float(pos["tp"]):
                            pnl = finalize_trade_close(stats, pos, float(pos["tp"]), "TP CLOSE (SHORT)", FEE_PCT, False)
                            send_telegram(f"🟢 *TP CLOSE (SHORT)*\nPrice: `${pos['tp']:.2f}` | PnL: `${pnl:+.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                            continue

                        await asyncio.sleep(15)
                        continue

                    # 3. Проверка отложенного лимитного ордера (если используется)
                    pending = stats.get("pending_order")
                    if pending:
                        if (now_ts - float(pending.get("created_at", now_ts))) >= LIMIT_ORDER_TTL_SEC:
                            stats["pending_order"] = None
                            stats["missed_limit_orders"] = int(stats.get("missed_limit_orders", 0)) + 1
                            save_state(stats)
                            logging.info("LIMIT EXPIRED: order canceled after TTL")
                            await asyncio.sleep(30)
                            continue
                        ticker = get_ticker()
                        if ticker:
                            bid, ask = float(ticker["bid"]), float(ticker["ask"])
                            lp = float(pending["limit_price"])
                            filled = (pending["side"] == "buy" and ask <= lp) or (pending["side"] == "sell" and bid >= lp)
                            if filled:
                                stats["open_position"] = {
                                    "side": pending["side"],
                                    "entry": lp,
                                    "volume": float(pending["volume"]),
                                    "sl": float(pending["sl"]),
                                    "tp": float(pending["tp"]),
                                }
                                stats["pending_order"] = None
                                stats["daily_trades"] = int(stats.get("daily_trades", 0)) + 1
                                save_state(stats)
                                logging.info(f"[FILL] side={pending['side']} price={lp:.2f}")
                                send_telegram(f"✅ *LIMIT FILLED ({pending['side'].upper()})*\nEntry: `${lp:.2f}`")
                                continue
                        await asyncio.sleep(10)
                        continue

                    # 4. Предохранители и лимиты
                    if stats.get("consecutive_stops", 0) >= MAX_CROSS_DAY_STOPS:
                        elapsed = now_ts - float(stats.get("last_stop_time", 0))
                        if elapsed < CROSS_DAY_CB_HOURS * 3600:
                            rem_min = int((CROSS_DAY_CB_HOURS * 3600 - elapsed) / 60)
                            logging.info(f"CIRCUIT BREAKER (3 stops streak): waiting {rem_min}m before auto-reset")
                            await asyncio.sleep(60)
                            continue
                        stats["consecutive_stops"] = 0
                        save_state(stats)
                        logging.info("CIRCUIT BREAKER (3 stops streak): cooldown expired, counter reset to 0")

                    if stats.get("daily_stop_streak", 0) >= MAX_CONSECUTIVE_STOPS:
                        logging.info("CIRCUIT BREAKER: max consecutive stops reached today")
                        await asyncio.sleep(60)
                        continue

                    if abs(float(stats.get("daily_pnl", 0.0))) >= float(stats.get("paper_balance", PAPER_BALANCE_START)) * MAX_DAILY_LOSS_PCT and float(stats.get("daily_pnl", 0.0)) < 0:
                        logging.info("CIRCUIT BREAKER: max daily loss reached")
                        await asyncio.sleep(60)
                        continue

                    if int(stats.get("daily_trades", 0)) >= MAX_TRADES_PER_DAY:
                        await asyncio.sleep(60)
                        continue

                    if (now_ts - float(stats.get("last_stop_time", 0))) < COOLDOWN_HOURS * 3600:
                        await asyncio.sleep(60)
                        continue

                    # 5. Рыночные данные и сигналы по закрытой свече
                    ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME, LIMIT)
                    ticker = get_ticker(KRAKEN_SYMBOL)
                    if not ohlc or not ticker or len(ohlc["closes"]) < BB_PERIOD + 12:
                        await asyncio.sleep(30)
                        continue

                    closed_ohlc = {k: v[:-1] for k, v in ohlc.items()}
                    bb = get_bb(closed_ohlc)
                    vwap = get_vwap(closed_ohlc)
                    atr = get_atr(closed_ohlc)
                    trend = get_trend(closed_ohlc)
                    deriv = get_derivatives_data("BTCUSDT")
                    funding_rate = float(deriv.get("funding_rate", 0.0))

                    if not bb or vwap is None or atr is None or atr <= 0:
                        await asyncio.sleep(60)
                        continue

                    upper, ma, lower, avg_vol, last_vol = bb
                    prev_close = closed_ohlc["closes"][-1]
                    prev_open = closed_ohlc["opens"][-1]

                    if last_vol < avg_vol * 0.55 or (atr / prev_close) < MIN_ATR_PCT:
                        await asyncio.sleep(60)
                        continue

                    vol_expanding = is_volatility_expanding(closed_ohlc, atr)
                    vol_spike = last_vol > avg_vol

                    bb_long = (
                        (prev_close <= lower)
                        and (trend != "down")
                        and not (trend == "range" and prev_close < lower * 0.996)
                    )
                    vwap_long = (
                        (prev_close > vwap)
                        and vol_spike
                        and (trend == "up")
                        and (prev_close < upper)
                    )
                    long_signal = (bb_long or vwap_long) and (funding_rate <= MAX_FUNDING_LONG)
                    if TREND_FILTER and trend == "down":
                        long_signal = False

                    bb_short = (
                        (prev_close >= upper)
                        and (trend != "up")
                        and not (trend == "range" and prev_close > upper * 1.004)
                    )
                    vwap_short = (
                        (prev_close < vwap)
                        and vol_spike
                        and (trend == "down")
                        and (prev_close > lower)
                    )
                    short_signal = (bb_short or vwap_short) and (funding_rate >= MIN_FUNDING_SHORT)
                    if TREND_FILTER and trend == "up":
                        short_signal = False

                    if (long_signal or short_signal) and vol_expanding:
                        stats["blocked_impulse_signals"] = int(stats.get("blocked_impulse_signals", 0)) + 1
                        save_state(stats)
                        logging.info("SKIP BB bounce: volatility expansion / impulse detected")
                        await asyncio.sleep(60)
                        continue

                    entry_price = float(ticker["ask"] if long_signal else ticker["bid"])
                    sl_dist = atr * ATR_MULTIPLIER
                    if sl_dist <= 0 or entry_price <= 0:
                        await asyncio.sleep(60)
                        continue

                    max_qty_by_lev = (float(stats["paper_balance"]) * MAX_LEVERAGE) / entry_price
                    risk_qty = (float(stats["paper_balance"]) * RISK_PCT) / sl_dist
                    volume = min(risk_qty, max_qty_by_lev)
                    if volume <= 0:
                        await asyncio.sleep(60)
                        continue

                    if long_signal:
                        sl = entry_price - sl_dist
                        tp = entry_price * (1.0 + TAKE_PROFIT_PCT)
                        stats["open_position"] = {
                            "id": str(uuid.uuid4())[:8],
                            "side": "buy",
                            "entry": entry_price,
                            "volume": volume,
                            "sl": sl,
                            "tp": tp,
                            "opened_at": now_ts,
                        }
                        stats["daily_trades"] = int(stats.get("daily_trades", 0)) + 1
                        save_state(stats)
                        logging.info(f"[TRADE] OPEN LONG at {entry_price:.2f} sl={sl:.2f} tp={tp:.2f}")
                        send_telegram(
                            f"🟢 *OPEN LONG*\nEntry: `${entry_price:.2f}`\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\n"
                            f"Vol: `{volume:.5f}` | Trend: `{trend}`"
                        )
                    elif short_signal:
                        sl = entry_price + sl_dist
                        tp = entry_price * (1.0 - TAKE_PROFIT_PCT)
                        stats["open_position"] = {
                            "id": str(uuid.uuid4())[:8],
                            "side": "sell",
                            "entry": entry_price,
                            "volume": volume,
                            "sl": sl,
                            "tp": tp,
                            "opened_at": now_ts,
                        }
                        stats["daily_trades"] = int(stats.get("daily_trades", 0)) + 1
                        save_state(stats)
                        logging.info(f"[TRADE] OPEN SHORT at {entry_price:.2f} sl={sl:.2f} tp={tp:.2f}")
                        send_telegram(
                            f"🔴 *OPEN SHORT*\nEntry: `${entry_price:.2f}`\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\n"
                            f"Vol: `{volume:.5f}` | Trend: `{trend}`"
                        )

                    await asyncio.sleep(60)

                except Exception as e:
                    logging.error(f"[ERROR] Loop iteration: {e}", exc_info=True)
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            logging.info("Trading loop cancelled")
        finally:
            await app.updater.stop_polling()
            await app.stop()
            logging.info("Bot stopped cleanly ✓")


def run_bot() -> None:
    global shutdown_event
    shutdown_event = asyncio.Event()
    try:
        asyncio.run(trading_loop())
    except KeyboardInterrupt:
        logging.info("Bot interrupted by user")


if __name__ == "__main__":
    run_bot()