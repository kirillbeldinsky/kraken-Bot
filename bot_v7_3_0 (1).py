import requests
import time
import logging
import json
import os
from datetime import date
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler
import asyncio
from dotenv import load_dotenv

# --- CONFIG ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", 0))
STATE_FILE = "bot_state.json"

# --- НАСТРОЙКИ СИСТЕМЫ ---
SYMBOL = "BTC/USD"
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15          # 15 минут
LIMIT = 100
PAPER_BALANCE_START = 1000.0

# --- РИСК ---
RISK_PCT = 0.01         # 1% на сделку
STOP_LOSS_PCT = 0.015   # 1.5% стоп
TAKE_PROFIT_PCT = 0.025 # 2.5% тейк (RR 1:1.67)
FEE_PCT = 0.0026        # комиссия биржи

# --- ФИЛЬТРЫ v7.3.0 ---
MIN_VOLUME_FILTER = 8
MIN_ATR_PCT = 0.00045   # порог волатильности (анти-флэт)
TREND_FILTER = True     # фильтр тренда (EMA)
COOLDOWN_HOURS = 4      # пауза после стопа (часы)
MAX_TRADES_PER_DAY = 3  # лимит сделок в день

# --- ДЕРИВАТИВНЫЕ ФИЛЬТРЫ ---
# Фандинг > +0.03%: рынок перегрет лонгами (опасность лонг-сквиза)
MAX_FUNDING_LONG = 0.0003
# Фандинг < -0.03%: рынок перегружен шортами (опасность шорт-сквиза)
MIN_FUNDING_SHORT = -0.0003

bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
shutdown_event = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# --- СТЕЙТ ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error reading state file: {e}")
    return {
        "paper_balance": PAPER_BALANCE_START,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
        "open_position": None,
        "last_stop_time": 0,
        "daily_trades": 0,
        "last_day": str(date.today())
    }

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving state: {e}")

# --- TELEGRAM ---
async def send_telegram_async(text):
    if not bot or not CHAT_ID:
        logging.warning("Telegram bot token or CHAT_ID not set")
        return
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Telegram send error: {e}")

def send_telegram(text):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_telegram_async(text))
        else:
            asyncio.run(send_telegram_async(text))
    except Exception as e:
        logging.error(f"send_telegram wrapper error: {e}")

# --- KRAKEN API ---
def get_ohlc(symbol=KRAKEN_SYMBOL, interval=TIMEFRAME, limit=LIMIT):
    """
    Получение OHLC свечей с Kraken.
    interval: число минут (1, 5, 15, 30, 60, 240, 1440).
    """
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}"
        r = requests.get(url, timeout=10)
        data = r.json()

        if data.get("error"):
            logging.error(f"Kraken API error: {data['error']}")
            return None

        result = data.get("result", {})
        if not result:
            logging.error("Kraken returned empty result")
            return None

        pair_key = next((k for k in result.keys() if k != "last"), None)
        if not pair_key:
            return None

        ohlc_data = result[pair_key]
        if not ohlc_data:
            return None

        ohlc_data = ohlc_data[-limit:]

        closes = [float(c[4]) for c in ohlc_data]
        highs = [float(c[2]) for c in ohlc_data]
        lows = [float(c[3]) for c in ohlc_data]
        volumes = [float(c[6]) for c in ohlc_data]

        return {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes
        }
    except Exception as e:
        logging.error(f"Error fetching OHLC data: {e}")
        return None

def get_ticker(symbol=KRAKEN_SYMBOL):
    """
    Получение текущего стакана/спреда: bid, ask, last.
    """
    try:
        resp = requests.get(
            f"https://api.kraken.com/0/public/Ticker?pair={symbol}",
            timeout=10
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        pair_key = next((k for k in result.keys()), None)
        if not pair_key:
            return None
        ticker = result[pair_key]
        return {
            "bid": float(ticker["b"][0]),
            "ask": float(ticker["a"][0]),
            "last": float(ticker["c"][0])
        }
    except Exception as e:
        logging.error(f"get_ticker error: {e}")
        return None

# --- ДЕРИВАТИВНЫЕ ДАННЫЕ (BINANCE FUTURES PUBLIC API) ---
def get_derivatives_data(symbol="BTCUSDT"):
    """
    Получение ставки финансирования (Funding Rate) и Открытого Интереса (Open Interest).
    Используется публичный REST API Binance Futures (не требует API ключей).
    """
    data = {
        "funding_rate": 0.0,
        "funding_rate_pct": 0.0,
        "open_interest": 0.0,
        "status": "ok"
    }
    try:
        # 1. Funding Rate
        f_url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
        f_res = requests.get(f_url, timeout=5)
        if f_res.status_code == 200:
            f_json = f_res.json()
            if f_json and isinstance(f_json, list):
                rate = float(f_json[0].get("fundingRate", 0.0))
                data["funding_rate"] = rate
                data["funding_rate_pct"] = rate * 100.0

        # 2. Open Interest
        oi_url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
        oi_res = requests.get(oi_url, timeout=5)
        if oi_res.status_code == 200:
            oi_json = oi_res.json()
            data["open_interest"] = float(oi_json.get("openInterest", 0.0))

        return data
    except Exception as e:
        logging.warning(f"Derivatives API warning: {e}")
        data["status"] = "error"
        return data

# --- ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ ---
def get_atr(periods=14):
    """
    Расчет ATR за последние `periods` свечей.
    """
    data = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME)
    if not data or len(data["closes"]) < periods + 1:
        return 0.0
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]
    tr_list = []
    for i in range(-periods, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list) / len(tr_list)

def get_trend():
    """
    Определение тренда через соотношение EMA20 / EMA50.
    """
    data = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME)
    if not data or len(data["closes"]) < 50:
        return "range"

    closes = data["closes"]
    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50

    if ema20 < ema50 * 0.997:
        return "down"
    if ema20 > ema50 * 1.003:
        return "up"
    return "range"

def get_bb():
    """
    Расчет Bollinger Bands (20, 2) и среднего объема.
    Возвращает: upper, ma20, lower, avg_vol, last_vol
    """
    data = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME)
    if not data or len(data["closes"]) < 20:
        return 0, 0, 0, 0, 0
    closes = data["closes"]
    volumes = data["volumes"]

    ma20 = sum(closes[-20:]) / 20
    std = (sum((x - ma20) ** 2 for x in closes[-20:]) / 20) ** 0.5
    upper = ma20 + 2 * std
    lower = ma20 - 2 * std
    avg_vol = sum(volumes[-20:]) / 20
    last_vol = volumes[-1]
    return upper, ma20, lower, avg_vol, last_vol

# --- TELEGRAM COMMANDS ---
async def stats_command(update: Update, context):
    try:
        stats = load_state()
        wr = (stats["total_wins"] / stats["total_trades"] * 100) if stats["total_trades"] else 0
        regime = get_trend()
        daily_r = stats.get("daily_trades", 0)
        roi = ((stats['paper_balance'] - PAPER_BALANCE_START) / PAPER_BALANCE_START * 100) if PAPER_BALANCE_START > 0 else 0
        
        # Получаем текущие деривативные данные для отчета
        deriv = get_derivatives_data()
        funding_str = f"{deriv['funding_rate_pct']:+.4f}%" if deriv["status"] == "ok" else "N/A"
        oi_str = f"{deriv['open_interest']:,.0f} BTC" if deriv["status"] == "ok" else "N/A"

        msg = f"""📊 *Stats {SYMBOL} v7.3.0*

💰 *Balance:* `${stats['paper_balance']:.2f}`
📈 *Total Trades:* `{stats['total_trades']}`
🎯 *Win Rate:* `{wr:.1f}%`
💵 *Total PnL:* `${stats['total_pnl']:.2f}`
📊 *ROI:* `{roi:.2f}%`
📈 *Market Regime:* `{regime}`
🔄 *Today's Trades:* `{daily_r}/{MAX_TRADES_PER_DAY}`
⚡ *Funding Rate:* `{funding_str}`
🏦 *Open Interest:* `{oi_str}`"""
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"[ERROR] /stats: {e}", exc_info=True)

async def pos_command(update: Update, context):
    try:
        stats = load_state()
        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("❌ No open position")
            return

        ticker = get_ticker()
        if not ticker:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            return

        current_price = ticker['bid'] if pos['side'] == 'buy' else ticker['ask']

        if pos['side'] == 'buy':
            current_pnl = (current_price - pos['entry']) * pos['volume'] * (1 - FEE_PCT)
            pnl_pct = ((current_price - pos['entry']) / pos['entry']) * 100
            dist_sl = ((pos['entry'] - pos['sl']) / pos['entry']) * 100
            dist_tp = ((pos['tp'] - pos['entry']) / pos['entry']) * 100
            side_emoji, side_text = "🟢", "LONG"
        else:
            current_pnl = (pos['entry'] - current_price) * pos['volume'] * (1 - FEE_PCT)
            pnl_pct = ((pos['entry'] - current_price) / pos['entry']) * 100
            dist_sl = ((pos['sl'] - pos['entry']) / pos['entry']) * 100
            dist_tp = ((pos['entry'] - pos['tp']) / pos['entry']) * 100
            side_emoji, side_text = "🔴", "SHORT"

        pnl_emoji = "📈" if current_pnl > 0 else "📉"

        msg = f"""{side_emoji} *Position {side_text}*

💵 *Entry:* `${pos['entry']:.2f}`
📍 *Current:* `${current_price:.2f}`
📊 *Volume:* `{pos['volume']:.4f}`

{pnl_emoji} *PnL:* `${current_pnl:.2f}` (`{pnl_pct:.2f}%`)

🛑 *SL:* `${pos['sl']:.2f}` (`{dist_sl:.2f}%`)
🎯 *TP:* `${pos['tp']:.2f}` (`{dist_tp:.2f}%`)"""
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"[ERROR] /pos: {e}", exc_info=True)

async def close_command(update: Update, context):
    try:
        stats = load_state()
        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("❌ No open position")
            return
        ticker = get_ticker()
        if not ticker:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            return
        price = ticker['bid'] if pos['side'] == 'buy' else ticker['ask']
        pnl = (price - pos['entry']) * pos['volume'] * (1 - FEE_PCT) if pos['side'] == 'buy' else (pos['entry'] - price) * pos['volume'] * (1 - FEE_PCT)
        stats["paper_balance"] += pnl
        stats["total_trades"] += 1
        stats["total_wins"] += 1 if pnl > 0 else 0
        stats["total_pnl"] += pnl
        stats["open_position"] = None
        save_state(stats)
        msg = f"🔴 *MANUAL CLOSE*\nPrice: `${price:.2f}`\nPnL: `${pnl:.2f}`\nBalance: `${stats['paper_balance']:.2f}`"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"[ERROR] /close: {e}", exc_info=True)

async def shutdown_command(update: Update, context):
    try:
        msg = "🛑 *Bot Shutdown Initiated*\nThe bot will stop in a few seconds..."
        await update.message.reply_text(msg, parse_mode='Markdown')
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
            send_telegram(f"🚀 *Bot started v7.3.0 with Derivatives* | Pair: `{SYMBOL}`")

            try:
                while not shutdown_event.is_set():
                    try:
                        stats = load_state()

                        # 1. Сброс счетчика дня
                        today = str(date.today())
                        if stats.get("last_day") != today:
                            stats["last_day"] = today
                            stats["daily_trades"] = 0
                            save_state(stats)

                        # 2. ПРОВЕРКА ОТКРЫТОЙ ПОЗИЦИИ (если есть)
                        if stats.get("open_position"):
                            pos = stats["open_position"]
                            ticker = get_ticker()
                            if not ticker:
                                await asyncio.sleep(5)
                                continue

                            bid, ask = ticker["bid"], ticker["ask"]

                            # Лонг SL
                            if pos["side"] == "buy" and bid <= pos["sl"]:
                                pnl = (pos["sl"] - pos["entry"]) * pos["volume"] * (1 - FEE_PCT)
                                stats["paper_balance"] += pnl
                                stats["total_trades"] += 1
                                stats["total_wins"] += 1 if pnl > 0 else 0
                                stats["total_pnl"] += pnl
                                stats["open_position"] = None
                                stats["last_stop_time"] = time.time()
                                save_state(stats)
                                send_telegram(f"🔴 *SL CLOSE (LONG)*\nPrice: `${pos['sl']:.2f}` | PnL: `${pnl:.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                                continue

                            # Лонг TP
                            if pos["side"] == "buy" and ask >= pos["tp"]:
                                pnl = (pos["tp"] - pos["entry"]) * pos["volume"] * (1 - FEE_PCT)
                                stats["paper_balance"] += pnl
                                stats["total_trades"] += 1
                                stats["total_wins"] += 1
                                stats["total_pnl"] += pnl
                                stats["open_position"] = None
                                save_state(stats)
                                send_telegram(f"🟢 *TP CLOSE (LONG)*\nPrice: `${pos['tp']:.2f}` | PnL: `${pnl:.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                                continue

                            # Шорт SL
                            if pos["side"] == "sell" and ask >= pos["sl"]:
                                pnl = (pos["entry"] - pos["sl"]) * pos["volume"] * (1 - FEE_PCT)
                                stats["paper_balance"] += pnl
                                stats["total_trades"] += 1
                                stats["total_wins"] += 1 if pnl > 0 else 0
                                stats["total_pnl"] += pnl
                                stats["open_position"] = None
                                stats["last_stop_time"] = time.time()
                                save_state(stats)
                                send_telegram(f"🔴 *SL CLOSE (SHORT)*\nPrice: `${pos['sl']:.2f}` | PnL: `${pnl:.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                                continue

                            # Шорт TP
                            if pos["side"] == "sell" and bid <= pos["tp"]:
                                pnl = (pos["entry"] - pos["tp"]) * pos["volume"] * (1 - FEE_PCT)
                                stats["paper_balance"] += pnl
                                stats["total_trades"] += 1
                                stats["total_wins"] += 1
                                stats["total_pnl"] += pnl
                                stats["open_position"] = None
                                save_state(stats)
                                send_telegram(f"🟢 *TP CLOSE (SHORT)*\nPrice: `${pos['tp']:.2f}` | PnL: `${pnl:.2f}`\nBalance: `${stats['paper_balance']:.2f}`")
                                continue

                            # Если позиция открыта, проверяем каждые 5 сек
                            await asyncio.sleep(5)
                            continue

                        # 3. ФИЛЬТРЫ ОГРАНИЧЕНИЯ ВХОДА
                        if stats.get("daily_trades", 0) >= MAX_TRADES_PER_DAY:
                            await asyncio.sleep(60)
                            continue

                        if time.time() - stats.get("last_stop_time", 0) < COOLDOWN_HOURS * 3600:
                            await asyncio.sleep(60)
                            continue

                        # 4. ПОЛУЧЕНИЕ РЫНОЧНЫХ ДАННЫХ
                        ticker = get_ticker()
                        if not ticker:
                            await asyncio.sleep(30)
                            continue
                        price = ticker["bid"]

                        upper, ma, lower, avg_vol, last_vol = get_bb()
                        if upper == 0:
                            await asyncio.sleep(30)
                            continue

                        # Фильтр относительного объема свечи
                        if last_vol < avg_vol * 0.55:
                            logging.info(f"SKIP: low relative volume ({last_vol:.2f} < {avg_vol * 0.55:.2f})")
                            await asyncio.sleep(60)
                            continue

                        # Фильтр ATR (анти-флэт)
                        atr = get_atr()
                        if atr == 0 or (atr / price) < MIN_ATR_PCT:
                            logging.info("SKIP: ATR too low (flat market)")
                            await asyncio.sleep(60)
                            continue

                        trend = get_trend()

                        # 5. ДЕРИВАТИВНЫЙ ФИЛЬТР (Funding Rate & Open Interest)
                        deriv = get_derivatives_data()
                        funding_rate = deriv.get("funding_rate", 0.0)

                        # 6. СИГНАЛЫ НА ВХОД
                        # Сигнал LONG: цена коснулась нижней полосы Боллинджера
                        if price <= lower:
                            if TREND_FILTER and trend == "down":
                                logging.info("SKIP Long: downtrend")
                                await asyncio.sleep(60)
                                continue

                            # Защита от входа в перегретый лонг
                            if funding_rate > MAX_FUNDING_LONG:
                                logging.info(f"SKIP Long: funding too high ({funding_rate * 100:.4f}% > {MAX_FUNDING_LONG * 100:.2f}%)")
                                await asyncio.sleep(60)
                                continue

                            risk_amount = stats["paper_balance"] * RISK_PCT
                            sl = price * (1 - STOP_LOSS_PCT)
                            tp = price * (1 + TAKE_PROFIT_PCT)
                            volume = risk_amount / (price - sl)

                            stats["open_position"] = {
                                "side": "buy",
                                "entry": price,
                                "volume": volume,
                                "sl": sl,
                                "tp": tp
                            }
                            stats["daily_trades"] = stats.get("daily_trades", 0) + 1
                            save_state(stats)

                            msg = f"🟢 *OPEN LONG*\nEntry: `${price:.2f}`\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\nVolume: `{volume:.4f}` | Trend: `{trend}`\nFunding: `{funding_rate*100:+.4f}%`"
                            send_telegram(msg)
                            logging.info(f"[TRADE] OPEN LONG at {price:.2f}")

                        # Сигнал SHORT: цена коснулась верхней полосы Боллинджера
                        elif price >= upper:
                            if TREND_FILTER and trend == "up":
                                logging.info("SKIP Short: uptrend")
                                await asyncio.sleep(60)
                                continue

                            # Защита от входа в перегретый шорт
                            if funding_rate < MIN_FUNDING_SHORT:
                                logging.info(f"SKIP Short: funding too low ({funding_rate * 100:.4f}% < {MIN_FUNDING_SHORT * 100:.2f}%)")
                                await asyncio.sleep(60)
                                continue

                            risk_amount = stats["paper_balance"] * RISK_PCT
                            sl = price * (1 + STOP_LOSS_PCT)
                            tp = price * (1 - TAKE_PROFIT_PCT)
                            volume = risk_amount / (sl - price)

                            stats["open_position"] = {
                                "side": "sell",
                                "entry": price,
                                "volume": volume,
                                "sl": sl,
                                "tp": tp
                            }
                            stats["daily_trades"] = stats.get("daily_trades", 0) + 1
                            save_state(stats)

                            msg = f"🔴 *OPEN SHORT*\nEntry: `${price:.2f}`\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\nVolume: `{volume:.4f}` | Trend: `{trend}`\nFunding: `{funding_rate*100:+.4f}%`"
                            send_telegram(msg)
                            logging.info(f"[TRADE] OPEN SHORT at {price:.2f}")

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