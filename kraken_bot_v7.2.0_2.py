import requests
import time
import logging
import json
import os
from datetime import date
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler
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
MIN_ATR_PCT = 0.0004 # не торгуем флэт < 0.004%
TREND_FILTER = True # вкл фильтр EMA
COOLDOWN_HOURS = 4 # пауза после стопа
MAX_TRADES_PER_DAY = 3 # лимит сделок

FEE_PCT = 0.0026
bot = Bot(token=TELEGRAM_TOKEN)

# Global flag for shutdown
shutdown_event = None

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
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def send_telegram(text):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(send_telegram_async(text))
        else:
            asyncio.run(send_telegram_async(text))
    except Exception as e:
        logging.error(f"send_telegram error: {e}")

# --- KRAKEN API ---
def get_ohlc(symbol, timeframe, limit):
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={timeframe}"
        response = requests.get(url, timeout=10)
        data = response.json()

        if "result" in data and symbol in data["result"]:
            ohlc_data = data["result"][symbol][-limit:]

            closes = [float(item[4]) for item in ohlc_data]
            highs = [float(item[2]) for item in ohlc_data]
            lows = [float(item[3]) for item in ohlc_data]
            volumes = [float(item[6]) for item in ohlc_data]

            return {
                "closes": closes,
                "highs": highs,
                "lows": lows,
                "volumes": volumes
            }
        logging.error(f"Raw OHLC response: {data}")
        return None

        logging.error("No OHLC data found")
        return None

    except Exception as e:
        logging.error(f"Error fetching OHLC data: {e}")
        return None


def get_ticker():
    """
    Returns dict: {'bid': float, 'ask': float, 'last': float} or None on error.
    """
    try:
        resp = requests.get(
            f"https://api.kraken.com/0/public/Ticker?pair={KRAKEN_SYMBOL}",
            timeout=10
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        pair_key = next((k for k in result.keys()), None)
        if not pair_key:
            logging.error("get_ticker: no pair data in response")
            return None
        ticker = result[pair_key]
        return {
            "bid": float(ticker["b"][0]),
            "ask": float(ticker["a"][0]),
            "last": float(ticker["c"][0])
        }
    except Exception as e:
        logging.exception(f"get_ticker error: {e}")
        return None

# --- ИНДИКАТОРЫ ---

def calculate_ema(p, n):
    ema = [sum(p[:n]) / n]
    k = 2 / (n + 1)
    for price in p[n:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

def get_atr(periods=14):
    """
    Returns ATR computed over the last `periods` bars (float) or 0 on error/insufficient data.
    """
    data = get_ohlc()
    if not data:
        return 0
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]
    if len(closes) < periods + 1:
        return 0
    tr_list = []
    # compute TR for the last `periods` bars
    for i in range(-periods, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list) / len(tr_list)

def get_trend():
    data = get_ohlc()
    if not data:
        return "range"
    closes = data["closes"]
    if len(closes) < 50:
        return "range"

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50

    # Мягкий порог ±0.15%
    if ema20 < ema50 * 0.997:
        return "down"
    if ema20 > ema50 * 1.003:
        return "up"
    return "range"


def get_bb():
    """
    Returns (upper, ma20, lower, avg_volume)
    Matches usage: upper, ma, lower, volume = get_bb()
    """
    data = get_ohlc()
    if not data:
        return 0, 0, 0, 0
    closes = data["closes"]
    volumes = data["volumes"]
    if len(closes) < 20:
        return 0, 0, 0, 0
    ma20 = sum(closes[-20:]) / 20
    std = (sum((x - ma20) ** 2 for x in closes[-20:]) / 20) ** 0.5
    upper = ma20 + 2 * std
    lower = ma20 - 2 * std
    avg_vol = sum(volumes[-20:]) / len(volumes[-20:])
    return upper, ma20, lower, avg_vol
    
# --- TELEGRAM COMMANDS ---
async def stats_command(update, context):
    """Handle /stats command"""
    try:
        logging.info(f"[CMD] /stats received")
        stats = load_state()
        wr = (stats["total_wins"]/stats["total_trades"]*100) if stats["total_trades"] else 0
        regime = get_trend()
        daily_r = stats.get("daily_trades", 0)
        roi = ((stats['paper_balance'] - PAPER_BALANCE_START) / PAPER_BALANCE_START * 100) if PAPER_BALANCE_START > 0 else 0
        msg = f"""📊 *Stats {SYMBOL} v7.2.0*

💰 *Balance:* `${stats['paper_balance']:.2f}`
📈 *Total Trades:* `{stats['total_trades']}`
🎯 *Win Rate:* `{wr:.1f}%`
💵 *Total PnL:* `${stats['total_pnl']:.2f}`
📊 *ROI:* `{roi:.2f}%`
📈 *Market Regime:* `{regime}`
🔄 *Today's Trades:* `{daily_r}/{MAX_TRADES_PER_DAY}`"""
        await update.message.reply_text(msg, parse_mode='Markdown')
        logging.info(f"[CMD] /stats sent response")
    except Exception as e:
        logging.error(f"[ERROR] /stats: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        except Exception as e2:
            logging.error(f"[ERROR] Failed to send error message: {e2}")

async def pos_command(update, context):
    """Handle /pos command"""
    try:
        logging.info(f"[CMD] /pos received")
        stats = load_state()
        pos = stats.get("open_position")
        if not pos:
            await update.message.reply_text("❌ No open position")
            return
        
        ticker = get_ticker()
        if not ticker:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            logging.error("[ERROR] get_ticker returned None in /pos")
            return
        
        current_price = ticker['bid'] if pos['side'] == 'buy' else ticker['ask']
        
        # Calculate current PnL
        if pos['side'] == 'buy':
            current_pnl = (current_price - pos['entry']) * pos['volume'] * (1 - FEE_PCT)
            pnl_pct = ((current_price - pos['entry']) / pos['entry']) * 100
        else:
            current_pnl = (pos['entry'] - current_price) * pos['volume'] * (1 - FEE_PCT)
            pnl_pct = ((pos['entry'] - current_price) / pos['entry']) * 100
        
        # Distance to SL/TP
        if pos['side'] == 'buy':
            dist_sl = ((pos['entry'] - pos['sl']) / pos['entry']) * 100
            dist_tp = ((pos['tp'] - pos['entry']) / pos['entry']) * 100
            side_emoji = "🟢"
            side_text = "LONG"
        else:
            dist_sl = ((pos['sl'] - pos['entry']) / pos['entry']) * 100
            dist_tp = ((pos['entry'] - pos['tp']) / pos['entry']) * 100
            side_emoji = "🔴"
            side_text = "SHORT"
        
        pnl_emoji = "📈" if current_pnl > 0 else "📉"
        
        msg = f"""{side_emoji} *Position {side_text}*

💵 *Entry:* `${pos['entry']:.2f}`
📍 *Current:* `${current_price:.2f}`
📊 *Volume:* `{pos['volume']:.4f}`

{pnl_emoji} *PnL:* `${current_pnl:.2f}` (`{pnl_pct:.2f}%`)

🛑 *SL:* `${pos['sl']:.2f}` (`{dist_sl:.2f}%`)
🎯 *TP:* `${pos['tp']:.2f}` (`{dist_tp:.2f}%`)"""
        await update.message.reply_text(msg, parse_mode='Markdown')
        logging.info(f"[CMD] /pos sent response")
    except Exception as e:
        logging.error(f"[ERROR] /pos: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        except Exception as e2:
            logging.error(f"[ERROR] Failed to send error message: {e2}")

async def close_command(update, context):
    """Handle /close command"""
    try:
        logging.info(f"[CMD] /close received")
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
        msg = f"🔴 *MANUAL CLOSE*\nPrice: ${price:.2f}\nPnL: ${pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}`"
        await update.message.reply_text(msg, parse_mode='Markdown')
        logging.info(f"[CMD] /close sent response")
    except Exception as e:
        logging.error(f"[ERROR] /close: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        except Exception as e2:
            logging.error(f"[ERROR] Failed to send error message: {e2}")

async def shutdown_command(update, context):
    """Handle /shutd command to shutdown the bot"""
    try:
        logging.info(f"[CMD] /shutd received - initiating shutdown")
        msg = "🛑 *Bot Shutdown Initiated*\nThe bot will stop in a few seconds..."
        await update.message.reply_text(msg, parse_mode='Markdown')
        logging.info(f"[CMD] /shutd sent shutdown notification")
        
        # Signal shutdown to the main loop
        global shutdown_event
        if shutdown_event:
            shutdown_event.set()
            logging.info("[CMD] Shutdown event set")
        
    except Exception as e:
        logging.error(f"[ERROR] /shutd: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        except Exception as e2:
            logging.error(f"[ERROR] Failed to send error message: {e2}")

# --- ОСНОВНОЙ ЦИКЛ ---
def run_bot():
    global shutdown_event
    shutdown_event = asyncio.Event()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Register command handlers
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("pos", pos_command))
    app.add_handler(CommandHandler("close", close_command))
    app.add_handler(CommandHandler("shutd", shutdown_command))

    def is_extreme_of_range(df, lookback=48):
        if len(df) < lookback:
            return None
        recent = df.tail(lookback)
        box_high = recent['high'].max()
        box_low = recent['low'].min()
        box_range = box_high - box_low
        if box_range < box_high * 0.01:
            return None
        price = df['close'].iloc[-1]
        top_zone = box_high - box_range * 0.15
        bottom_zone = box_low + box_range * 0.15
        if price >= top_zone:
            return "TOP"
        if price <= bottom_zone:
            return "BOTTOM"
        return None  

    async def trading_loop():
        async with app:
            await app.initialize()
            await app.start()
            logging.info("Bot initialized and starting polling...")
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            logging.info("Bot updater polling started ✓")
            send_telegram(f"🚀 Bot started 7.2.0-filters | Pair: {SYMBOL}")
            
            try:
                while not shutdown_event.is_set():
                    try:
                        stats = load_state()
                        
                        # --- ФИЛЬТР: Сброс дневного лимита ---
                        today = str(date.today())
                        if stats["last_day"] != today:
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

                        # --- ФИЛЬТР: Низкий объем ---
                        df = get_ohlc(SYMBOL, "1h", 100)
                        if df is None:
                            logging.error("OHLC returned None, skipping this cycle")
                            await asyncio.sleep(5)
                            continue
                          # или continue, если внутри цикла
                        vol_24h = df["volumes"].tail(24).sum()
                        if vol_24h < 15_000_000_000:
                            if stats["daily_trades"] >= 1:
                                await asyncio.sleep(600)
                                continue
                        
                         # --- ФИЛЬТР: ОТНОСИТЕЛЬНЫЙ ОБЪЕМ ---
                        ohlc = get_ohlc()
                        if not ohlc or len(ohlc["volumes"]) < 20:
                            logging.info("SKIP - not enough volume data")
                            continue
                                                
                        volumes = ohlc["volumes"]
                        vol_ma = sum(volumes[-20:]) / 20
                                                
                        if volume < vol_ma * 0.55:
                            logging.info(f"SKIP - low relative volume ({volume:.2f} < {vol_ma*0.55:.2f})")
                            continue
                        
                         # --- ФИЛЬТР: ATR (анти-флэт) ---
                        atr = get_atr()
                        if atr == 0:
                            logging.info("SKIP - ATR unavailable")
                            continue
                        
                        atr_pct = atr / price
                        if atr_pct < 0.00045:
                            logging.info(f"SKIP - ATR too low ({atr_pct:.5f})")
                            continue
                            
                        # --- ФИЛЬТР: Тренд ---
                        trend = get_trend()
                        
                        if price <= lower:
                            if TREND_FILTER and trend == "down":
                                logging.info(f"SKIP Long - downtrend")
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
                            msg = f"🟢 *OPEN LONG*\nEntry: ${price:.2f}\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\nVolume: `{volume:.4f}` | Trend: `{trend}`"
                            send_telegram(msg)
                            logging.info(f"[TRADE] OPEN LONG at {price:.2f}")
                                                
                        # ШОРТ
                        elif price >= upper:
                            if TREND_FILTER and trend == "up":
                                logging.info(f"SKIP Short - uptrend")
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
                            msg = f"🔴 *OPEN SHORT*\nEntry: ${price:.2f}\nSL: `${sl:.2f}` | TP: `${tp:.2f}`\nVolume: `{volume:.4f}` | Trend: `{trend}`"
                            send_telegram(msg)
                            logging.info(f"[TRADE] OPEN SHORT at {price:.2f}")
                         
                        # --- ПРОВЕРКА ОТКРЫТОЙ ПОЗИЦИИ ---
                        if stats.get("open_position"):
                            pos = stats["open_position"]
                            ticker = get_ticker()
                            if not ticker:
                                logging.warning("Ticker is None, retrying...")
                                await asyncio.sleep(60)
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
                                wr = (stats["total_wins"]/stats["total_trades"]*100)
                                msg = f"🔴 *SL CLOSE*\nSide: `LONG`\nPrice: `${pos['sl']:.2f}`\nPnL: ${pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                                send_telegram(msg)
                                logging.info(f"[TRADE] SL closed LONG")
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
                                msg = f"🟢 *TP CLOSE*\nSide: `LONG`\nPrice: `${pos['tp']:.2f}`\nPnL: ${pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                                send_telegram(msg)
                                logging.info(f"[TRADE] TP closed LONG")
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
                                msg = f"🔴 *SL CLOSE*\nSide: `SHORT`\nPrice: `${pos['sl']:.2f}`\nPnL: ${pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                                send_telegram(msg)
                                logging.info(f"[TRADE] SL closed SHORT")
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
                                msg = f"🟢 *TP CLOSE*\nSide: `SHORT`\nPrice: `${pos['tp']:.2f}`\nPnL: ${pnl:.2f}\nBalance: `${stats['paper_balance']:.2f}`\nTrades: `{stats['total_trades']}` | WR: `{wr:.0f}%`"
                                send_telegram(msg)
                                logging.info(f"[TRADE] TP closed SHORT")
                                continue
                            
                            await asyncio.sleep(5)
                            continue
                        
                        # --- ПОИСК ВХОДА ---
                        upper, ma, lower, volume = get_bb()
                        if not upper: 
                            await asyncio.sleep(60)
                            continue
                        
                        ticker = get_ticker()
                        if not ticker:
                            logging.warning("Ticker is None, retrying...")
                            await asyncio.sleep(60)
                            continue
                        price = ticker["bid"]
                        

                        
                        await asyncio.sleep(60)
                        
                    except Exception as e:
                        logging.error(f"[ERROR] Loop: {e}", exc_info=True)
                        await asyncio.sleep(60)
            except KeyboardInterrupt:
                logging.info("Bot received stop signal...")
            finally:
                await app.updater.stop_polling()
                await app.stop()
                logging.info("Bot stopped cleanly ✓")
    
    asyncio.run(trading_loop())

if __name__ == "__main__":
    run_bot()


