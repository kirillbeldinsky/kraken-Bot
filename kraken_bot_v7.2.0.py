import requests
import time
import logging
import json
import os
from datetime import date
from telegram import Bot
import asyncio

# --- CONFIG ---
from dotenv import load_dotenv
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", 0))
STATE_FILE = "bot_state.json"

# --- НАСТРОЙКИ СИСТЕМЫ ---
SYMBOL = "BTC/USD"
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15
EMA_PERIOD = 50
TRADE_STAGE_LIMIT = 100
PAPER_BALANCE_START = 1000.0

# --- РИСК ---
RISK_PCT = 0.01 # 1% на сделку
STOP_LOSS_PCT = 0.015 # 1.5% стоп
TAKE_PROFIT_PCT = 0.025 # 2.5% тейк, RR 1:1.67

# --- ФИЛЬТРЫ v7.2.0 ---
MIN_VOLUME_FILTER = 8 # было 15, резало шорты на BTC
MIN_ATR_PCT = 0.003 # не торгуем флэт < 0.3%
TREND_FILTER = True # вкл фильтр EMA
COOLDOWN_HOURS = 4 # пауза после стопа
MAX_TRADES_PER_DAY = 3 # лимит сделок

FEE_PCT = 0.0026
bot = Bot(token=TELEGRAM_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# --- СТЕЙТ ---
def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

stats = load_state()

# --- TELEGRAM ---
async def send_telegram_async(text):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def send_telegram(text):
    loop = asyncio.get_event_loop()
    if   loop.is_running():
          loop.create_task(send_telegram_async(text))
    else:
       asyncio.run(send_telegram_async(text))

# --- KRAKEN API ---
def get_ohlc():
    try:
        resp = requests.get(f"https://api.kraken.com/0/public/OHLC?pair={SYMBOL}")
        data = resp.json()["result"]
        pair_key = list(data.keys())[0] # автоматом возьмет XXBTZUSD
        ohlc = data[pair_key]
        prices = [float(x[4]) for x in ohlc]

    except Exception as e:
        logging.error(f"{func_name} error: {e}")
        return None
def get_ticker():
    try:
        resp = requests.get(f"https://api.kraken.com/0/public/Ticker?pair={SYMBOL}")
        data = resp.json()["result"]
        pair_key = list(data.keys())[0]
        ticker = data[pair_key]
        return float(ticker['c'][0])

    except Exception as e:
        logging.error(f"{func_name} error: {e}")
        return None
# --- ИНДИКАТОРЫ ---
def calculate_ema(p, n):
    ema = [sum(p[:n]) / n]
    k = 2 / (n + 1)
    for price in p[n:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

def get_atr():
    data = get_ohlc()
    if not prices or len(prices) < 15: return 0
    tr_list = []
    for i in range(1, 15):
        h = prices[i]
        l = prices[i] 
        c_prev = prices[i-1]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
    return sum(tr_list) / len(tr_list)
def get_trend():
    prices = get_ohlc()
    if not prices or len(prices) < 50: return "range"
    ema20 = sum(prices[-20:])/20
    ema50 = sum(prices[-50:])/50
    if ema20 < ema50 * 0.997: return "down"
    if ema20 > ema50 * 1.003: return "up"
    return "range"
def get_bb():
    prices = get_ohlc()
    if not prices: return 0, 0, 0, 0
    ma20 = sum(prices[-20:]) / 20
    std = (sum([(x - ma20) ** 2 for x in prices[-20:]]) / 20) ** 0.5
    upper = ma20 + 2 * std
    lower = ma20 - 2 * std
    return upper, lower, ma20, std

# --- TELEGRAM COMMANDS ---
async def handle_command(update, context):
    text = update.message.text
    stats = load_state() # всегда читаем свежий стейт
    
    if text == "/stats":
        wr = (stats["total_wins"]/stats["total_trades"]*100) if stats["total_trades"] else 0
        regime = get_trend()
        daily_r = 0 # TODO: добавить расчет
        msg = f"📊 Stats {SYMBOL} v7.2.0\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`\nPnL: `${stats['total_pnl']:.2f}`\nRegime: `{regime}` | DailyR: `{daily_r:.2f}`"
        await update.message.reply_text(msg)
    
    elif text == "/pos":
        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("Нет открытой позиции")
            return
        side = "BUY" if pos["side"] == "buy" else "SELL"
        msg = f"📍 Position\nSide: `{side}` @ `${pos['entry']:.2f}`\nVolume: `{pos['volume']:.4f}`\nSL: `${pos['sl']:.2f}` | TP: `${pos['tp']:.2f}`"
        await update.message.reply_text(msg)
    
    elif text == "/close":
        pos = stats.get("open_position")
        if not pos: 
            await update.message.reply_text("Нет открытой позиции")
            return
        ticker = get_ticker()
        price = ticker['bid'] if pos['side'] == 'buy' else ticker['ask']
        pnl = (price - pos['entry']) * pos['volume'] * (1 - FEE_PCT) if pos['side'] == 'buy' else (pos['entry'] - price) * pos['volume'] * (1 - FEE_PCT)
        stats["paper_balance"] += pnl
        stats["total_trades"] += 1
        stats["total_wins"] += 1 if pnl > 0 else 0
        stats["total_pnl"] += pnl
        stats["open_position"] = None
        save_state(stats)
        msg = f"🔴 *MANUAL CLOSE*\nPrice: {price:.2f}\nPnL: {pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}`"
        await update.message.reply_text(msg)

# --- ОСНОВНОЙ ЦИКЛ ---
def run_bot():
    from telegram.ext import Application, CommandHandler
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("stats", handle_command))
    app.add_handler(CommandHandler("pos", handle_command))
    app.add_handler(CommandHandler("close", handle_command))
    
    async def trading_loop():
        await app.initialize()
        await app.start()
        send_telegram(f"🚀 Bot started 7.2.0-filters | Pair: {SYMBOL}")
        
        while True:
            try:
                stats = load_state()
                
                # --- ФИЛЬТР: Сброс дневного лимита ---
                today = str(date.today())
                if stats["last_day"]!= today:
                    stats["last_day"] = today
                    stats["daily_trades"] = 0
                    save_state(stats)
                
                # --- ФИЛЬТР: Лимит сделок ---
                if stats.get("daily_trades", 0) >= MAX_TRADES_PER_DAY:
                    await asyncio.sleep(60)
                    continue
                
                # --- ФИЛЬТР: Кулдаун после стопа ---
                if time.time() - stats.get("last_stop_time", 0) < COOLDOWN_HOURS * 3600:
                    await asyncio.sleep(60)
                    continue
                
                # --- ПРОВЕРКА ОТКРЫТОЙ ПОЗИЦИИ ---
                if stats.get("open_position"):
                    pos = stats["open_position"]
                    ticker = get_ticker()
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
                        wr = (stats["total_wins"]/stats["total_trades"]*100)
                        msg = f"🔴 *SL CLOSE*\nSide: `LONG`\nPrice: `${pos['sl']:.2f}`\nPnL: {pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                        send_telegram(msg)
                        logging.info(f"[CLOSE] SL {msg}")
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
                        wr = (stats["total_wins"]/stats["total_trades"]*100)
                        msg = f"🟢 *TP CLOSE*\nSide: `LONG`\nPrice: `${pos['tp']:.2f}`\nPnL: {pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                        send_telegram(msg)
                        logging.info(f"[CLOSE] TP {msg}")
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
                        wr = (stats["total_wins"]/stats["total_trades"]*100)
                        msg = f"🔴 *SL CLOSE*\nSide: `SHORT`\nPrice: `${pos['sl']:.2f}`\nPnL: {pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                        send_telegram(msg)
                        logging.info(f"[CLOSE] SL {msg}")
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
                        wr = (stats["total_wins"]/stats["total_trades"]*100)
                        msg = f"🟢 *TP CLOSE*\nSide: `SHORT`\nPrice: `${pos['tp']:.2f}`\nPnL: {pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                        send_telegram(msg)
                        logging.info(f"[CLOSE] TP {msg}")
                        continue
                    
                    await asyncio.sleep(5)
                    continue
                
                # --- ПОИСК ВХОДА ---
                upper, ma, lower, volume = get_bb()
                if not upper: 
                    await asyncio.sleep(60)
                    continue
                
                ticker = get_ticker()
                price = ticker["bid"]
                
                # --- ФИЛЬТР: Объём ---
                if volume < MIN_VOLUME_FILTER:
                    await asyncio.sleep(60)
                    continue
                
                # --- ФИЛЬТР: Волатильность ATR ---
                atr = get_atr()
                if atr / price < MIN_ATR_PCT:
                    logging.info(f" Low volatility ATR={atr:.0f}")
                    await asyncio.sleep(60)
                    continue
                
                # --- ФИЛЬТР: Тренд ---
                trend = get_trend()
                
                # ЛОНГ
                if price <= lower:
                    if TREND_FILTER and trend == "down":
                        logging.info(f" SKIP Long в даунтренде")
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
                    msg = f"🟢 *OPEN LONG*\nEntry: {price:.2f}\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\nVolume: `{volume:.4f}` | Trend: `{trend}`"
                    send_telegram(msg)
                    logging.info(f"[OPEN] {msg}")
                
                # ШОРТ
                elif price >= upper:
                    if TREND_FILTER and trend == "up":
                        logging.info(f" SKIP Short в аптренде")
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
                    msg = f"🔴 *OPEN SHORT*\nEntry: {price:.2f}\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\nVolume: `{volume:.4f}` | Trend: `{trend}`"
                    send_telegram(msg)
                    logging.info(f"[OPEN] {msg}")
                
                await asyncio.sleep(60)
                
            except Exception as e:
                logging.error(f"Loop error: {e}")
                await asyncio.sleep(60)
    
    asyncio.run(trading_loop())

if __name__ == "__main__":
    run_bot()
