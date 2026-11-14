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

# --- ФИЛЬТРЫ v7.3.0.1 ---
MIN_VOLUME_FILTER = 8
MIN_ATR_PCT = 0.00045   # порог волатильности (анти-флэт)
TREND_FILTER = True     # фильтр тренда (EMA)
COOLDOWN_HOURS = 4      # пауза после стопа (часы)
MAX_TRADES_PER_DAY = 3  # лимит сделок в день

# ---
# --- ДЕРИВАТИВНЫЕ ДАННЫЕ ---

bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None
shutdown_event = None

def get_derivatives_data(symbol="PF_XBTUSD"):
    """
    Получение Funding Rate и Open Interest с Kraken Futures (публичный API).
    """
    data = {
        "funding_rate": 0.0,
        "funding_rate_pct": 0.0,
        "open_interest": 0.0,
        "status": "ok"
    }
    try:
        url = f"https://futures.kraken.com/derivatives/api/v3/tickers/{symbol}"
        res = requests.get(url, timeout=5)
        if res.status_code != 200:
            logging.warning(f"Kraken Futures API HTTP {res.status_code}")
            data["status"] = "error"
            return data

        payload = res.json()
        if payload.get("result") != "success":
            logging.warning(f"Kraken Futures API error: {payload}")
            data["status"] = "error"
            return data

        ticker = payload.get("ticker", {})
        # На Kraken Futures фандинг начисляется каждый час;
        # умножаем на 8 для привычного 8-часового эквивалента под пороги ±0.03%
        rate_1h = float(ticker.get("fundingRate", 0.0))
        rate_8h = rate_1h * 8.0

        data["funding_rate"] = rate_8h
        data["funding_rate_pct"] = rate_8h * 100.0
        data["open_interest"] = float(ticker.get("openInterest", 0.0))
        return data

    except Exception as e:
        logging.warning(f"Derivatives API warning: {e}")
        data["status"] = "error"
        return data


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
def get_vwap(highs, lows, closes, volumes, periods=20):
    """
    Расчет VWAP за последние periods свечей.
    """
    h_slice = highs[-periods:]
    l_slice = lows[-periods:]
    c_slice = closes[-periods:]
    v_slice = volumes[-periods:]
    typical_price = [(h + l + c) / 3.0 for h, l, c in zip(h_slice, l_slice, c_slice)]
    tp_vol = [tp * v for tp, v in zip(typical_price, v_slice)]
    cumulative_tp_vol = sum(tp_vol)
    cumulative_vol = sum(v_slice)
    return cumulative_tp_vol / cumulative_vol if cumulative_vol != 0 else 0.0

def get_atr(data=None, periods=14):
    """
    Расчет ATR за последние periods свечей.
    Если data не передана, запрашивает свечи с биржи.
    """
    if data is None:
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

def get_trend(data=None):
    """
    Определение тренда через соотношение EMA20 / EMA50 (15m).
    Если data не передана, запрашивает свечи с биржи.
    """
    if data is None:
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

def get_bb(data=None):
    """
    Расчет Bollinger Bands (20, 2) и объемов.
    Возвращает: upper, ma20, lower, avg_vol, last_vol.
    """
    if data is None:
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

def get_trend_mtf(symbol=KRAKEN_SYMBOL, interval=60):
    """
    Определение глобального тренда на старшем таймфрейме (1h).
    """
    data = get_ohlc(symbol=symbol, interval=interval, limit=100)
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

        msg = f"""📊 *Stats {SYMBOL} v7.3.3*

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
            send_telegram(f"🚀 *Bot started v7.3.3 with VWAP & Derivatives* | Pair: `{SYMBOL}`")

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

                         # =====================================================
                        # 2. ПРОВЕРКА ОТКРЫТОЙ ПОЗИЦИИ (SL / TP)
                        # =====================================================
                        if stats.get("open_position"):
                            pos = stats["open_position"]
                            ticker = get_ticker()
                            if not ticker:
                                await asyncio.sleep(5)
                                continue

                            bid, ask = ticker["bid"], ticker["ask"]
                            side = pos["side"]
                            entry = pos["entry"]
                            vol = pos["volume"]
                            
                            close_reason = None
                            exit_price = 0.0

                            # --- ПРОВЕРКА LONG (выход через Sell по цене BID) ---
                            if side == "buy":
                                if bid <= pos["sl"]:
                                    close_reason = "SL"
                                    exit_price = bid  # Реальная цена пробоя (учет проскальзывания)
                                elif bid >= pos["tp"]:
                                    close_reason = "TP"
                                    exit_price = pos["tp"]

                            # --- ПРОВЕРКА SHORT (выход через Buy по цене ASK) ---
                            elif side == "sell":
                                if ask >= pos["sl"]:
                                    close_reason = "SL"
                                    exit_price = ask  # Реальная цена пробоя (учет проскальзывания)
                                elif ask <= pos["tp"]:
                                    close_reason = "TP"
                                    exit_price = pos["tp"]

                            # --- ИСПОЛНЕНИЕ ЗАКРЫТИЯ ---
                            if close_reason:
                                # 1. Расчет грязного PnL
                                if side == "buy":
                                    gross_pnl = (exit_price - entry) * vol
                                else:
                                    gross_pnl = (entry - exit_price) * vol

                                # 2. Честный расчет комиссии со всего оборота (вход + выход)
                                total_turnover = (entry + exit_price) * vol
                                fee = total_turnover * FEE_PCT
                                net_pnl = gross_pnl - fee

                                # 3. Обновление статистики
                                stats["paper_balance"] += net_pnl
                                stats["total_trades"] += 1
                                if net_pnl > 0:
                                    stats["total_wins"] += 1
                                stats["total_pnl"] += net_pnl
                                stats["open_position"] = None

                                # Если выбило по SL — включаем кулдаун
                                if close_reason == "SL":
                                    stats["last_stop_time"] = time.time()

                                save_state(stats)

                                # 4. Уведомление в Telegram
                                icon = "🟢" if net_pnl > 0 else "🔴"
                                side_str = "LONG" if side == "buy" else "SHORT"
                                msg = (
                                    f"{icon} *{close_reason} CLOSE ({side_str})*\n"
                                    f"Exit Price: `${exit_price:.2f}` (Target: `${pos[close_reason.lower()]:.2f}`)\n"
                                    f"Gross PnL: `${gross_pnl:+.2f}` | Fee: `${fee:.2f}`\n"
                                    f"Net PnL: `${net_pnl:+.2f}`\n"
                                    f"Balance: `${stats['paper_balance']:.2f}`"
                                )
                                send_telegram(msg)
                                logging.info(f"[TRADE] {close_reason} CLOSE {side_str} at {exit_price:.2f}, Net PnL: {net_pnl:.2f}")
                                continue

                            # Если позиция удерживается, спим 5 секунд до следующей проверки тикера
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

                        ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME)
                        if not ohlc or len(ohlc["closes"]) < 20:
                            await asyncio.sleep(30)
                            continue

                        upper, ma, lower, avg_vol, last_vol = get_bb()
                        if upper == 0:
                            await asyncio.sleep(30)
                            continue

                        # Фильтр относительного объема свечи
                        if last_vol < avg_vol * 0.55:
                            logging.info(f"SKIP: low relative volume ({last_vol:.2f} < {avg_vol * 0.55:.2f})")
                            await asyncio.sleep(60)
                            continue

                        # vwap_volume_filter
                        vwap = get_vwap(ohlc['highs'], ohlc['lows'], ohlc['closes'], ohlc['volumes'])
                        vol_profile_check = last_vol > avg_vol  # Условие активности объемов

                        # Фильтр ATR (анти-флэт)
                        atr = get_atr()
                        if atr == 0 or (atr / price) < MIN_ATR_PCT:
                            logging.info("SKIP: ATR too low (flat market)")
                            await asyncio.sleep(60)
                            continue

                        # Трендовые фильтры (локальный 15m и глобальный 1h)
                        trend = get_trend()
                        mtf_trend = get_trend_mtf(interval=60)  # <-- ИСПРАВЛЕНИЕ: объявление переменной

                        # 5. ДЕРИВАТИВНЫЙ ФИЛЬТР (Funding Rate & Open Interest)
                        deriv = get_derivatives_data()
                        funding_rate = deriv.get("funding_rate", 0.0)

                       # =====================================================
                        # 6. СИГНАЛЫ НА ВХОД (LONG)
                        # =====================================================
                        
                        # Базовые фильтры активности свечи 15m
                        vol_ok = last_vol >= avg_vol * 0.55
                        atr_ok = atr > 0 and (atr / price) >= MIN_ATR_PCT

                        # 1. Условия триггеров:
                        # А) Отскок от нижней полосы Боллинджера (при подтвержденном объеме и волатильности)
                        bounce_signal = (price <= lower) and vol_ok and atr_ok

                        # Б) Подтвержденный импульс выше VWAP по локальному аптренду (внутри канала)
                        breakout_signal = (
                            price > vwap 
                            and vol_profile_check 
                            and trend == "up" 
                            and price < upper
                        )

                        # 2. Проверка входа:
                        if bounce_signal or breakout_signal:
                            # 1) Защита от торговли против тренда (локального 15m или старшего 1h)
                            if TREND_FILTER and (trend == "down" or mtf_trend == "down"):
                                logging.info(
                                    f"SKIP Long: downtrend active (15m: {trend}, 1h: {mtf_trend})"
                                )
                                await asyncio.sleep(60)
                                continue

                            # 2) Защита от перегрева по ставке финансирования Binance
                            if funding_rate > MAX_FUNDING_LONG:
                                logging.info(
                                    f"SKIP Long: funding too high ({funding_rate * 100:.4f}% > {MAX_FUNDING_LONG * 100:.2f}%)"
                                )
                                await asyncio.sleep(60)
                                continue

                            # 3) Расчет позиции и риск-менеджмент
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

                            signal_type = "BOUNCE" if bounce_signal else "BREAKOUT"
                            msg = (
                                f"🟢 *OPEN LONG ({signal_type})*\n"
                                f"Entry: `${price:.2f}` | VWAP: `${vwap:.2f}`\n"
                                f"SL: `${sl:.2f}` | TP: `${tp:.2f}`\n"
                                f"Volume: `{volume:.4f}` | Trend: `{trend}` (1h: `{mtf_trend}`)\n"
                                f"Funding: `{funding_rate * 100:+.4f}%`"
                            )
                            send_telegram(msg)
                            logging.info(f"[TRADE] OPEN LONG ({signal_type}) at {price:.2f}")

# =====================================================
                        # 6. СИГНАЛЫ НА ВХОД (SHORT)
                        # =====================================================
                        
                        # 1. Условия триггеров:
                        # А) Отскок от верхней полосы Боллинджера (при подтвержденном объеме и волатильности)
                        bounce_short_signal = (price >= upper) and vol_ok and atr_ok

                        # Б) Подтвержденный импульс ниже VWAP по локальному даунтренду (внутри канала)
                        breakdown_short_signal = (
                            price < vwap 
                            and vol_profile_check 
                            and trend == "down" 
                            and price > lower
                        )

                        # 2. Проверка входа:
                        if bounce_short_signal or breakdown_short_signal:
                            # 1) Защита от торговли против восходящего тренда (15m или 1h)
                            if TREND_FILTER and (trend == "up" or mtf_trend == "up"):
                                logging.info(
                                    f"SKIP Short: uptrend active (15m: {trend}, 1h: {mtf_trend})"
                                )
                                await asyncio.sleep(60)
                                continue

                            # 2) Защита от шорт-сквиза при отрицательном фандинге (перегруз шортов)
                            if funding_rate < MIN_FUNDING_SHORT:
                                logging.info(
                                    f"SKIP Short: funding too low ({funding_rate * 100:.4f}% < {MIN_FUNDING_SHORT * 100:.2f}%)"
                                )
                                await asyncio.sleep(60)
                                continue

                            # 3) Расчет позиции и риск-менеджмент для SHORT
                            risk_amount = stats["paper_balance"] * RISK_PCT
                            sl = price * (1 + STOP_LOSS_PCT)  # Стоп-лосс ВЫШЕ цены входа
                            tp = price * (1 - TAKE_PROFIT_PCT)  # Тейк-профит НИЖЕ цены входа
                            volume = risk_amount / (sl - price)  # Делитель (sl - price) > 0

                            stats["open_position"] = {
                                "side": "sell",
                                "entry": price,
                                "volume": volume,
                                "sl": sl,
                                "tp": tp
                            }
                            stats["daily_trades"] = stats.get("daily_trades", 0) + 1
                            save_state(stats)

                            signal_type = "BOUNCE" if bounce_short_signal else "BREAKDOWN"
                            msg = (
                                f"🔴 *OPEN SHORT ({signal_type})*\n"
                                f"Entry: `${price:.2f}` | VWAP: `${vwap:.2f}`\n"
                                f"SL: `${sl:.2f}` | TP: `${tp:.2f}`\n"
                                f"Volume: `{volume:.4f}` | Trend: `{trend}` (1h: `{mtf_trend}`)\n"
                                f"Funding: `{funding_rate * 100:+.4f}%`"
                            )
                            send_telegram(msg)
                            logging.info(f"[TRADE] OPEN SHORT ({signal_type}) at {price:.2f}")

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
