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

# --- НАСТРОЙКИ СИСТЕМЫ (v7.4.1-WFO-LIMIT) ---
VERSION = "v7.4.1-WFO-LIMIT"
SYMBOL = "BTC/USD"
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15                  # 15 минут
LIMIT = 100
PAPER_BALANCE_START = 1000.0

# --- РИСК И ПАРАМЕТРЫ СТРАТЕГИИ ---
RISK_PCT = 0.01                 # 1% риска на сделку
MAX_LEVERAGE = 1.0              # без кредитного плеча
BB_PERIOD = 20
BB_STD = 2.2                    # откалибровано в WFO
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5            # адаптивный стоп = 1.5 * ATR
TAKE_PROFIT_PCT = 0.015         # 1.5% тейк-профит

# --- КОМИССИИ И ЛИМИТНЫЕ ОРДЕРА (KRAKEN POST-ONLY) ---
FEE_MAKER = 0.0016              # 0.16% Maker (лимитный вход и TP)
FEE_TAKER = 0.0026              # 0.26% Taker (рыночный SL)
LIMIT_ORDER_TTL_SEC = 300       # 5 минут жизни неисполненного лимитного ордера
LIMIT_PRICE_OFFSET = 0.0        # Постановка точно по лучшему bid / ask стакана

# --- ФИЛЬТРЫ И ЗАЩИТА (DAILY CIRCUIT BREAKER) ---
MIN_ATR_PCT = 0.00045           # анти-флэт порог
TREND_FILTER = True             # фильтр тренда SMA20/SMA50
COOLDOWN_HOURS = 4              # пауза после стопа (часы)
MAX_TRADES_PER_DAY = 3          # максимум сделок в сутки
MAX_CONSECUTIVE_STOPS = 2       # стоп-кран после 2 стопов подряд за день
MAX_DAILY_LOSS_PCT = 0.02       # стоп-кран при дневной просадке -2.0%

# --- ДЕРИВАТИВНЫЕ ФИЛЬТРЫ (BINANCE FUTURES) ---
MAX_FUNDING_LONG = 0.0003
MIN_FUNDING_SHORT = -0.0003

bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
shutdown_event = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# --- СТЕЙТ ---
def default_state() -> dict:
    return {
        "paper_balance": PAPER_BALANCE_START,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
        "open_position": None,
        "pending_order": None,
        "last_stop_time": 0,
        "daily_trades": 0,
        "daily_pnl": 0.0,
        "daily_stop_streak": 0,
        "missed_limit_orders": 0,
        "last_day": str(date.today()),
    }


def load_state() -> dict:
    state = default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    state.update(saved)
        except Exception as e:
            logging.error(f"Error reading state file: {e}")
    return state


def save_state(state: dict) -> None:
    try:
        tmp_file = f"{STATE_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        logging.error(f"Error saving state: {e}")


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
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_telegram_async(text))
        else:
            asyncio.run(send_telegram_async(text))
    except Exception as e:
        logging.error(f"send_telegram wrapper error: {e}")


# --- KRAKEN & BINANCE PUBLIC API ---
def get_ohlc(symbol: str = KRAKEN_SYMBOL, interval: int = TIMEFRAME, limit: int = LIMIT) -> dict | None:
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}"
        r = requests.get(url, timeout=10)
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


# --- ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ ---
def get_vwap(data: dict, periods: int = 20) -> float:
    if not data or len(data["closes"]) < periods:
        return 0.0
    h_s, l_s, c_s, v_s = data["highs"][-periods:], data["lows"][-periods:], data["closes"][-periods:], data["volumes"][-periods:]
    total_vol = sum(v_s)
    if total_vol == 0:
        return sum(c_s) / periods
    typical = [(h + l + c) / 3.0 for h, l, c in zip(h_s, l_s, c_s)]
    return sum(tp * v for tp, v in zip(typical, v_s)) / total_vol


def get_atr(data: dict, periods: int = ATR_PERIOD) -> float:
    if not data or len(data["closes"]) < periods + 1:
        return 0.0
    highs, lows, closes = data["highs"], data["lows"], data["closes"]
    tr_list = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(-periods, 0)
    ]
    return sum(tr_list) / len(tr_list)


def get_trend(data: dict) -> str:
    if not data or len(data["closes"]) < 50:
        return "range"
    closes = data["closes"]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    if sma20 < sma50 * 0.997:
        return "down"
    if sma20 > sma50 * 1.003:
        return "up"
    return "range"


def get_bb(data: dict, periods: int = BB_PERIOD, num_std: float = BB_STD):
    if not data or len(data["closes"]) < periods:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    closes = data["closes"][-periods:]
    volumes = data["volumes"][-periods:]
    ma = sum(closes) / periods
    std = (sum((x - ma) ** 2 for x in closes) / periods) ** 0.5
    return ma + num_std * std, ma, ma - num_std * std, sum(volumes) / periods, volumes[-1]


def calc_net_pnl(side: str, entry: float, exit_price: float, volume: float, exit_fee_pct: float) -> float:
    gross = (exit_price - entry) * volume if side == "buy" else (entry - exit_price) * volume
    fees = (entry * volume * FEE_MAKER) + (exit_price * volume * exit_fee_pct)
    return gross - fees


# --- TELEGRAM COMMANDS ---
async def stats_command(update: Update, context):
    try:
        stats = load_state()
        wr = (stats["total_wins"] / stats["total_trades"] * 100) if stats["total_trades"] else 0.0
        ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME)
        regime = get_trend(ohlc) if ohlc else "unknown"
        roi = ((stats["paper_balance"] - PAPER_BALANCE_START) / PAPER_BALANCE_START * 100) if PAPER_BALANCE_START > 0 else 0.0

        deriv = get_derivatives_data()
        funding_str = f"{deriv['funding_rate_pct']:+.4f}%" if deriv["status"] == "ok" else "N/A"
        pending_str = "ACTIVE" if stats.get("pending_order") else "NONE"

        msg = (
            f"📊 *Stats {SYMBOL} {VERSION}*\n\n"
            f"💰 *Balance:* `${stats['paper_balance']:.2f}`\n"
            f"📈 *Total Trades:* `{stats['total_trades']}` | *WR:* `{wr:.1f}%`\n"
            f"💵 *Total PnL:* `${stats['total_pnl']:.2f}` (`{roi:+.2f}%`)\n"
            f"📅 *Today PnL:* `${stats.get('daily_pnl', 0.0):+.2f}` | *Trades:* `{stats.get('daily_trades', 0)}/{MAX_TRADES_PER_DAY}`\n"
            f"🛡️ *Stop Streak:* `{stats.get('daily_stop_streak', 0)}/{MAX_CONSECUTIVE_STOPS}`\n"
            f"⏳ *Pending Limit:* `{pending_str}` (TTL missed: `{stats.get('missed_limit_orders', 0)}`)\n"
            f"📈 *Regime:* `{regime}` | *Funding:* `{funding_str}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"[ERROR] /stats: {e}", exc_info=True)


async def pos_command(update: Update, context):
    try:
        stats = load_state()
        pending = stats.get("pending_order")
        if pending:
            rem_sec = max(0, int(pending["expires_at"] - time.time()))
            msg = (
                f"⏳ *PENDING LIMIT ORDER ({pending['side'].upper()})*\n"
                f"Limit Price: `${pending['limit_price']:.2f}` | Vol: `{pending['volume']:.4f} BTC`\n"
                f"SL: `${pending['sl']:.2f}` | TP: `${pending['tp']:.2f}`\n"
                f"Auto-cancel in: `{rem_sec}s`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("❌ No open position or pending order")
            return

        ticker = get_ticker()
        if not ticker:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            return

        current_price = ticker["bid"] if pos["side"] == "buy" else ticker["ask"]
        current_pnl = calc_net_pnl(pos["side"], pos["entry"], current_price, pos["volume"], FEE_MAKER)

        if pos["side"] == "buy":
            pnl_pct = ((current_price - pos["entry"]) / pos["entry"]) * 100
            dist_sl = ((pos["entry"] - pos["sl"]) / pos["entry"]) * 100
            dist_tp = ((pos["tp"] - pos["entry"]) / pos["entry"]) * 100
            side_emoji, side_text = "🟢", "LONG"
        else:
            pnl_pct = ((pos["entry"] - current_price) / pos["entry"]) * 100
            dist_sl = ((pos["sl"] - pos["entry"]) / pos["entry"]) * 100
            dist_tp = ((pos["entry"] - pos["tp"]) / pos["entry"]) * 100
            side_emoji, side_text = "🔴", "SHORT"

        pnl_emoji = "📈" if current_pnl > 0 else "📉"
        msg = (
            f"{side_emoji} *Position {side_text}*\n\n"
            f"💵 *Entry (Maker):* `${pos['entry']:.2f}` | *Current:* `${current_price:.2f}`\n"
            f"📊 *Volume:* `{pos['volume']:.4f} BTC`\n"
            f"{pnl_emoji} *Net PnL:* `${current_pnl:.2f}` (`{pnl_pct:+.2f}%`)\n"
            f"🛑 *SL (Taker):* `${pos['sl']:.2f}` (`-{dist_sl:.2f}%`) | 🎯 *TP (Maker):* `${pos['tp']:.2f}` (`+{dist_tp:.2f}%`)"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"[ERROR] /pos: {e}", exc_info=True)


async def close_command(update: Update, context):
    try:
        stats = load_state()
        if stats.get("pending_order"):
            stats["pending_order"] = None
            save_state(stats)
            await update.message.reply_text("🗑️ *Pending limit order cancelled*")
            return

        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("❌ No open position")
            return
        ticker = get_ticker()
        if not ticker:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            return
        price = ticker["bid"] if pos["side"] == "buy" else ticker["ask"]
        pnl = calc_net_pnl(pos["side"], pos["entry"], price, pos["volume"], FEE_TAKER)
        stats["paper_balance"] += pnl
        stats["total_trades"] += 1
        stats["total_wins"] += 1 if pnl > 0 else 0
        stats["total_pnl"] += pnl
        stats["daily_pnl"] = stats.get("daily_pnl", 0.0) + pnl
        stats["open_position"] = None
        save_state(stats)
        msg = f"🔴 *MANUAL CLOSE*\nPrice: `${price:.2f}`\nNet PnL: `${pnl:.2f}`\nBalance: `${stats['paper_balance']:.2f}`"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"[ERROR] /close: {e}", exc_info=True)


async def shutdown_command(update: Update, context):
    try:
        await update.message.reply_text("🛑 *Bot Shutdown Initiated*", parse_mode="Markdown")
        global shutdown_event
        if shutdown_event:
            shutdown_event.set()
    except Exception as e:
        logging.error(f"[ERROR] /shutd: {e}", exc_info=True)


# --- ОСНОВНОЙ ЦИКЛ ---
def run_bot():
    global shutdown_event
    shutdown_event = asyncio.Event()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("pos", pos_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("shutd", shutdown_command))

    async def trading_loop():
        async with app:
            await app.initialize()
            await app.start()
            logging.info("Bot updater polling started ✓")
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            send_telegram(
                f"🚀 *Bot started {VERSION}* | `{SYMBOL}` (`{TIMEFRAME}m`)\n"
                f"Limit TTL: `{LIMIT_ORDER_TTL_SEC}s` | Maker: `0.16%` | SL Taker: `0.26%`"
            )

            try:
                while not shutdown_event.is_set():
                    try:
                        stats = load_state()

                        # 1. Сброс дневных счётчиков и предохранителей
                        today = str(date.today())
                        if stats.get("last_day") != today:
                            stats["last_day"] = today
                            stats["daily_trades"] = 0
                            stats["daily_pnl"] = 0.0
                            stats["daily_stop_streak"] = 0
                            save_state(stats)

                        # 2. ОБРАБОТКА АКТИВНОГО ЛИМИТНОГО ОРДЕРА В СТАКАНЕ (PENDING ORDER + TTL)
                        if stats.get("pending_order"):
                            pending = stats["pending_order"]
                            now = time.time()

                            # Проверка истечения TTL (если цена ушла и ордер не исполнился за 5 минут)
                            if now >= pending["expires_at"]:
                                stats["pending_order"] = None
                                stats["missed_limit_orders"] = stats.get("missed_limit_orders", 0) + 1
                                save_state(stats)
                                logging.info(f"LIMIT EXPIRED: cancelled unfilled {pending['side']} @ {pending['limit_price']:.2f}")
                                send_telegram(
                                    f"⌛ *LIMIT ORDER CANCELLED (TTL)*\n"
                                    f"Side: `{pending['side'].upper()}` | Price: `${pending['limit_price']:.2f}`"
                                )
                                await asyncio.sleep(10)
                                continue

                            # Проверка касания лимитной цены в стакане (симуляция исполнения post-only)
                            ticker = get_ticker()
                            if ticker:
                                filled = (
                                    (pending["side"] == "buy" and ticker["ask"] <= pending["limit_price"])
                                    or (pending["side"] == "sell" and ticker["bid"] >= pending["limit_price"])
                                )
                                if filled:
                                    stats["open_position"] = {
                                        "side": pending["side"],
                                        "entry": pending["limit_price"],
                                        "volume": pending["volume"],
                                        "sl": pending["sl"],
                                        "tp": pending["tp"],
                                        "checked_candle_time": 0,
                                    }
                                    stats["pending_order"] = None
                                    stats["daily_trades"] = stats.get("daily_trades", 0) + 1
                                    save_state(stats)
                                    badge = "🟢" if pending["side"] == "buy" else "🔴"
                                    send_telegram(
                                        f"{badge} *LIMIT FILLED ({pending['side'].upper()})*\n"
                                        f"Entry (Maker 0.16%): `${pending['limit_price']:.2f}` | Vol: `{pending['volume']:.4f} BTC`\n"
                                        f"SL: `${pending['sl']:.2f}` | TP: `${pending['tp']:.2f}`"
                                    )
                                    logging.info(f"[FILL] Limit {pending['side']} filled @ {pending['limit_price']:.2f}")
                                    continue

                            await asyncio.sleep(10)
                            continue

                        # 3. ПРОВЕРКА ОТКРЫТОЙ ПОЗИЦИИ ПО ЗАКРЫТИЮ СВЕЧИ
                        if stats.get("open_position"):
                            pos = stats["open_position"]
                            ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME, limit=2)
                            if not ohlc or len(ohlc["times"]) < 2:
                                await asyncio.sleep(5)
                                continue

                            last_closed_time = ohlc["times"][-2]
                            if pos.get("checked_candle_time") != last_closed_time:
                                pos["checked_candle_time"] = last_closed_time
                                prev_high = ohlc["highs"][-2]
                                prev_low = ohlc["lows"][-2]

                                exit_triggered = False
                                exit_reason = ""
                                exit_price = 0.0
                                is_stop = False

                                if pos["side"] == "buy":
                                    if prev_low <= pos["sl"]:
                                        exit_triggered, exit_reason, exit_price, is_stop = True, "SL CLOSE (LONG)", pos["sl"], True
                                    elif prev_high >= pos["tp"]:
                                        exit_triggered, exit_reason, exit_price = True, "TP CLOSE (LONG)", pos["tp"]
                                elif pos["side"] == "sell":
                                    if prev_high >= pos["sl"]:
                                        exit_triggered, exit_reason, exit_price, is_stop = True, "SL CLOSE (SHORT)", pos["sl"], True
                                    elif prev_low <= pos["tp"]:
                                        exit_triggered, exit_reason, exit_price = True, "TP CLOSE (SHORT)", pos["tp"]

                                if exit_triggered:
                                    exit_fee = FEE_TAKER if is_stop else FEE_MAKER
                                    final_pnl = calc_net_pnl(pos["side"], pos["entry"], exit_price, pos["volume"], exit_fee)
                                    stats["paper_balance"] += final_pnl
                                    stats["total_trades"] += 1
                                    stats["total_wins"] += 1 if final_pnl > 0 else 0
                                    stats["total_pnl"] += final_pnl
                                    stats["daily_pnl"] = stats.get("daily_pnl", 0.0) + final_pnl

                                    if is_stop:
                                        stats["last_stop_time"] = time.time()
                                        stats["daily_stop_streak"] = stats.get("daily_stop_streak", 0) + 1
                                        badge = "🔴"
                                    else:
                                        stats["daily_stop_streak"] = 0
                                        badge = "🟢"

                                    stats["open_position"] = None
                                    save_state(stats)

                                    wr = (stats["total_wins"] / stats["total_trades"] * 100) if stats["total_trades"] else 0.0
                                    send_telegram(
                                        f"{badge} *{exit_reason}*\n"
                                        f"Exit: `${exit_price:.2f}` | Net PnL: `${final_pnl:+.2f}`\n"
                                        f"Balance: `${stats['paper_balance']:.2f}` | WR: `{wr:.1f}%`"
                                    )
                                    continue

                                save_state(stats)

                            await asyncio.sleep(15)
                            continue

                        # 4. ДНЕВНОЙ ПРЕДОХРАНИТЕЛЬ (DAILY CIRCUIT BREAKER)
                        if stats.get("daily_stop_streak", 0) >= MAX_CONSECUTIVE_STOPS:
                            logging.info("CIRCUIT BREAKER: max consecutive stops reached today")
                            await asyncio.sleep(60)
                            continue

                        if stats.get("daily_pnl", 0.0) <= -(stats["paper_balance"] * MAX_DAILY_LOSS_PCT):
                            logging.info("CIRCUIT BREAKER: max daily loss reached today")
                            await asyncio.sleep(60)
                            continue

                        if stats.get("daily_trades", 0) >= MAX_TRADES_PER_DAY:
                            await asyncio.sleep(60)
                            continue

                        if time.time() - stats.get("last_stop_time", 0) < COOLDOWN_HOURS * 3600:
                            await asyncio.sleep(60)
                            continue

                        # 5. ПОЛУЧЕНИЕ РЫНОЧНЫХ ДАННЫХ
                        ticker = get_ticker()
                        ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME, limit=LIMIT)
                        if not ticker or not ohlc:
                            await asyncio.sleep(30)
                            continue

                        price = ticker["bid"]
                        upper, ma, lower, avg_vol, last_vol = get_bb(ohlc)
                        vwap = get_vwap(ohlc)
                        atr = get_atr(ohlc)
                        trend = get_trend(ohlc)

                        if upper == 0 or vwap == 0:
                            await asyncio.sleep(30)
                            continue

                        if last_vol < avg_vol * 0.55:
                            await asyncio.sleep(60)
                            continue

                        if atr == 0 or (atr / price) < MIN_ATR_PCT:
                            await asyncio.sleep(60)
                            continue

                        deriv = get_derivatives_data()
                        funding_rate = deriv.get("funding_rate", 0.0)
                        vol_spike = last_vol > avg_vol

                        long_signal = (price <= lower) or (
                            price > vwap and vol_spike and trend == "up" and price < upper
                        )
                        short_signal = (price >= upper) or (
                            price < vwap and vol_spike and trend == "down" and price > lower
                        )

                        # 6. ВЫСТАВЛЕНИЕ ЛИМИТНОГО POST-ONLY ОРДЕРА С TTL
                        if long_signal:
                            if (TREND_FILTER and trend == "down") or funding_rate > MAX_FUNDING_LONG:
                                await asyncio.sleep(60)
                                continue

                            limit_price = round(ticker["bid"] * (1.0 - LIMIT_PRICE_OFFSET), 1)
                            risk_amount = stats["paper_balance"] * RISK_PCT
                            sl_dist = atr * ATR_MULTIPLIER
                            volume = min(risk_amount / sl_dist, (stats["paper_balance"] * MAX_LEVERAGE) / limit_price)

                            stats["pending_order"] = {
                                "order_id": str(uuid.uuid4())[:8],
                                "side": "buy",
                                "limit_price": limit_price,
                                "volume": volume,
                                "sl": limit_price - sl_dist,
                                "tp": limit_price * (1.0 + TAKE_PROFIT_PCT),
                                "created_at": time.time(),
                                "expires_at": time.time() + LIMIT_ORDER_TTL_SEC,
                            }
                            save_state(stats)
                            send_telegram(
                                f"⏳ *PLACED LIMIT BUY (post-only)*\n"
                                f"Limit: `${limit_price:.2f}` | Vol: `{volume:.4f} BTC`\n"
                                f"TTL: `{LIMIT_ORDER_TTL_SEC}s` | Trend: `{trend}`"
                            )

                        elif short_signal:
                            if (TREND_FILTER and trend == "up") or funding_rate < MIN_FUNDING_SHORT:
                                await asyncio.sleep(60)
                                continue

                            limit_price = round(ticker["ask"] * (1.0 + LIMIT_PRICE_OFFSET), 1)
                            risk_amount = stats["paper_balance"] * RISK_PCT
                            sl_dist = atr * ATR_MULTIPLIER
                            volume = min(risk_amount / sl_dist, (stats["paper_balance"] * MAX_LEVERAGE) / limit_price)

                            stats["pending_order"] = {
                                "order_id": str(uuid.uuid4())[:8],
                                "side": "sell",
                                "limit_price": limit_price,
                                "volume": volume,
                                "sl": limit_price + sl_dist,
                                "tp": limit_price * (1.0 - TAKE_PROFIT_PCT),
                                "created_at": time.time(),
                                "expires_at": time.time() + LIMIT_ORDER_TTL_SEC,
                            }
                            save_state(stats)
                            send_telegram(
                                f"⏳ *PLACED LIMIT SELL (post-only)*\n"
                                f"Limit: `${limit_price:.2f}` | Vol: `{volume:.4f} BTC`\n"
                                f"TTL: `{LIMIT_ORDER_TTL_SEC}s` | Trend: `{trend}`"
                            )

                        await asyncio.sleep(60)

                    except Exception as e:
                        logging.error(f"[ERROR] Loop iteration: {e}", exc_info=True)
                        await asyncio.sleep(60)

            except KeyboardInterrupt:
                logging.info("Stop signal received...")
            finally:
                await app.updater.stop_polling()
                await app.stop()
                logging.info("Bot stopped cleanly ✓")

    asyncio.run(trading_loop())


if __name__ == "__main__":
    run_bot()
