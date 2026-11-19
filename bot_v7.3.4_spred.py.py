import asyncio
import json
import logging
import math
import os
import time
from datetime import date

import requests
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler

# --- CONFIG ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", 0))
STATE_FILE = "bot_state_spread.json"

# --- НАСТРОЙКИ СПРЕДА (PAPER) ---
LEG_1_SYMBOL = "XXBTZUSD"
LEG_1_NAME = "BTC/USD"
LEG_2_SYMBOL = "XETHZUSD"
LEG_2_NAME = "ETH/USD"

TIMEFRAME = 15              # 15 минут
LIMIT = 100
PAPER_BALANCE_START = 1000.0
BASE_LEG_NOTIONAL_USD = 500.0
FEE_PCT = 0.0026            # комиссия на каждую сторону/ногу

# --- ПАРАМЕТРЫ СПРЕДА И ATR-ВЫРАВНИВАНИЯ ---
WINDOW_Z = 20
ENTRY_Z = 2.0
EXIT_Z = 0.5
STOP_Z = 3.5
MAX_SPREAD_LOSS_USD = 25.0
ATR_PERIOD = 14
ATR_WEIGHT_STRENGTH = 0.5   # 0.0 = чистый равный USD, 1.0 = полное выравнивание по ATR%

# --- СОХРАНЁННЫЕ ФИЛЬТРЫ ---
MIN_VOLUME_FILTER = 8
MIN_ATR_PCT = 0.00045
TREND_FILTER = True
COOLDOWN_HOURS = 4
MAX_TRADES_PER_DAY = 3

# --- ДЕРИВАТИВНЫЕ ФИЛЬТРЫ ---
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
        "open_spread": None,
        "last_stop_time": 0,
        "daily_trades": 0,
        "last_day": str(date.today()),
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
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")
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
def get_ohlc(symbol=LEG_1_SYMBOL, interval=TIMEFRAME, limit=LIMIT):
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("error"):
            logging.error(f"Kraken API error ({symbol}): {data['error']}")
            return None
        result = data.get("result", {})
        pair_key = next((k for k in result.keys() if k != "last"), None)
        if not pair_key:
            return None
        ohlc_data = result[pair_key][-limit:]
        if not ohlc_data:
            return None
        return {
            "closes": [float(c[4]) for c in ohlc_data],
            "highs": [float(c[2]) for c in ohlc_data],
            "lows": [float(c[3]) for c in ohlc_data],
            "volumes": [float(c[6]) for c in ohlc_data],
        }
    except Exception as e:
        logging.error(f"Error fetching OHLC data ({symbol}): {e}")
        return None


def get_ticker(symbol=LEG_1_SYMBOL):
    try:
        resp = requests.get(
            f"https://api.kraken.com/0/public/Ticker?pair={symbol}",
            timeout=10,
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
            "last": float(ticker["c"][0]),
        }
    except Exception as e:
        logging.error(f"get_ticker error ({symbol}): {e}")
        return None


# --- ДЕРИВАТИВНЫЕ ДАННЫЕ ---
def get_derivatives_data(symbol="BTCUSDT"):
    data = {
        "funding_rate": 0.0,
        "funding_rate_pct": 0.0,
        "open_interest": 0.0,
        "status": "ok",
    }
    try:
        f_url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
        f_res = requests.get(f_url, timeout=5)
        if f_res.status_code == 200:
            f_json = f_res.json()
            if f_json and isinstance(f_json, list):
                rate = float(f_json[0].get("fundingRate", 0.0))
                data["funding_rate"] = rate
                data["funding_rate_pct"] = rate * 100.0

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


# --- ИНДИКАТОРЫ И СПРЕД ---
def calc_atr_from_ohlc(ohlc, periods=ATR_PERIOD):
    if not ohlc or len(ohlc.get("closes", [])) < periods + 1:
        return 0.0
    highs = ohlc["highs"]
    lows = ohlc["lows"]
    closes = ohlc["closes"]
    tr_list = []
    for i in range(-periods, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    return sum(tr_list) / len(tr_list) if tr_list else 0.0


def get_trend_from_ohlc(ohlc):
    if not ohlc or len(ohlc.get("closes", [])) < 50:
        return "range"
    closes = ohlc["closes"]
    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50
    if ema20 < ema50 * 0.997:
        return "down"
    if ema20 > ema50 * 1.003:
        return "up"
    return "range"


def compute_spread_snapshot():
    t1 = get_ticker(LEG_1_SYMBOL)
    t2 = get_ticker(LEG_2_SYMBOL)
    d1 = get_ohlc(LEG_1_SYMBOL, TIMEFRAME, LIMIT)
    d2 = get_ohlc(LEG_2_SYMBOL, TIMEFRAME, LIMIT)
    if not t1 or not t2 or not d1 or not d2:
        return None

    c1_all, c2_all = d1["closes"], d2["closes"]
    n = min(len(c1_all), len(c2_all))
    if n < WINDOW_Z or t1["last"] <= 0 or t2["last"] <= 0:
        return None

    c1, c2 = c1_all[-n:], c2_all[-n:]
    spread_hist = [math.log(a) - math.log(b) for a, b in zip(c1, c2) if a > 0 and b > 0]
    if len(spread_hist) < WINDOW_Z:
        return None

    recent = spread_hist[-WINDOW_Z:]
    mean_sp = sum(recent) / WINDOW_Z
    std_sp = (sum((x - mean_sp) ** 2 for x in recent) / WINDOW_Z) ** 0.5
    curr_spread = math.log(t1["last"]) - math.log(t2["last"])
    z_score = (curr_spread - mean_sp) / std_sp if std_sp > 0 else 0.0

    atr1 = calc_atr_from_ohlc(d1, ATR_PERIOD)
    atr2 = calc_atr_from_ohlc(d2, ATR_PERIOD)
    atr1_pct = atr1 / t1["last"] if t1["last"] > 0 else 0.0
    atr2_pct = atr2 / t2["last"] if t2["last"] > 0 else 0.0

    avg_vol1 = sum(d1["volumes"][-20:]) / 20 if len(d1["volumes"]) >= 20 else 0.0
    last_vol1 = d1["volumes"][-1] if d1["volumes"] else 0.0
    trend1 = get_trend_from_ohlc(d1)

    return {
        "t1": t1,
        "t2": t2,
        "spread": curr_spread,
        "mean_spread": mean_sp,
        "z_score": z_score,
        "atr1_pct": atr1_pct,
        "atr2_pct": atr2_pct,
        "avg_vol1": avg_vol1,
        "last_vol1": last_vol1,
        "trend1": trend1,
    }


def calc_atr_notionals(snap, base_usd=BASE_LEG_NOTIONAL_USD):
    """Выравнивает номинал ног с учётом относительной волатильности ATR%."""
    a1 = snap["atr1_pct"]
    a2 = snap["atr2_pct"]
    if a1 > 0 and a2 > 0:
        ratio = a1 / a2
        # Ограничиваем крайние перекосы коэффициента волатильности
        ratio = max(0.5, min(2.0, ratio))
        factor = ratio ** ATR_WEIGHT_STRENGTH
        return base_usd, base_usd * factor
    return base_usd, base_usd


def calc_leg_pnl(leg, ticker, fee_pct=FEE_PCT):
    if leg["side"] == "buy":
        exit_price = ticker["bid"]
        gross_pnl = (exit_price - leg["entry"]) * leg["volume"]
    else:
        exit_price = ticker["ask"]
        gross_pnl = (leg["entry"] - exit_price) * leg["volume"]
    entry_fee = leg["entry"] * leg["volume"] * fee_pct
    exit_fee = exit_price * leg["volume"] * fee_pct
    return gross_pnl - entry_fee - exit_fee, exit_price


def close_spread_position(stats, reason, snap=None):
    sp = stats.get("open_spread")
    if not sp:
        return None
    if snap is None:
        t1 = get_ticker(LEG_1_SYMBOL)
        t2 = get_ticker(LEG_2_SYMBOL)
        if not t1 or not t2:
            return None
    else:
        t1, t2 = snap["t1"], snap["t2"]

    pnl1, _ = calc_leg_pnl(sp["leg_1"], t1)
    pnl2, _ = calc_leg_pnl(sp["leg_2"], t2)
    total_pnl = pnl1 + pnl2

    stats["paper_balance"] += total_pnl
    stats["total_trades"] += 1
    stats["total_wins"] += 1 if total_pnl > 0 else 0
    stats["total_pnl"] += total_pnl
    if total_pnl < 0 or "STOP" in reason:
        stats["last_stop_time"] = time.time()
    stats["open_spread"] = None
    save_state(stats)
    return {
        "reason": reason,
        "pnl1": pnl1,
        "pnl2": pnl2,
        "total_pnl": total_pnl,
        "balance": stats["paper_balance"],
    }


# --- TELEGRAM COMMANDS ---
async def stats_command(update: Update, context):
    try:
        stats = load_state()
        wr = (stats["total_wins"] / stats["total_trades"] * 100) if stats["total_trades"] else 0
        snap = compute_spread_snapshot()
        z_str = f"{snap['z_score']:+.2f}" if snap else "N/A"
        regime = snap["trend1"] if snap else "N/A"
        daily_r = stats.get("daily_trades", 0)
        roi = (
            ((stats["paper_balance"] - PAPER_BALANCE_START) / PAPER_BALANCE_START * 100)
            if PAPER_BALANCE_START > 0
            else 0.0
        )
        deriv = get_derivatives_data()
        funding_str = f"{deriv['funding_rate_pct']:+.4f}%" if deriv["status"] == "ok" else "N/A"
        oi_str = f"{deriv['open_interest']:,.0f} BTC" if deriv["status"] == "ok" else "N/A"

        msg = (
            f"📊 *Stats {LEG_1_NAME}/{LEG_2_NAME} v7.4.0-SPREAD (PAPER)*\n\n"
            f"💰 *Balance:* `${stats['paper_balance']:.2f}`\n"
            f"📈 *Total Trades:* `{stats['total_trades']}`\n"
            f"🎯 *Win Rate:* `{wr:.1f}%`\n"
            f"💵 *Total PnL:* `${stats['total_pnl']:.2f}`\n"
            f"📊 *ROI:* `{roi:.2f}%`\n"
            f"📐 *Current Z-Score:* `{z_str}`\n"
            f"📈 *Market Regime:* `{regime}`\n"
            f"🔄 *Today's Trades:* `{daily_r}/{MAX_TRADES_PER_DAY}`\n"
            f"⚡ *Funding Rate:* `{funding_str}`\n"
            f"🏦 *Open Interest:* `{oi_str}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"[ERROR] /stats: {e}", exc_info=True)


async def pos_command(update: Update, context):
    try:
        stats = load_state()
        sp = stats.get("open_spread")
        if not sp:
            await update.message.reply_text("❌ No open spread position")
            return
        snap = compute_spread_snapshot()
        if not snap:
            await update.message.reply_text("❌ Error fetching market data from Kraken API")
            return

        pnl1, px1 = calc_leg_pnl(sp["leg_1"], snap["t1"])
        pnl2, px2 = calc_leg_pnl(sp["leg_2"], snap["t2"])
        total_pnl = pnl1 + pnl2

        msg = (
            f"⚖️ *Spread Position ({sp['direction']})*\n"
            f"Z-Score: `{snap['z_score']:+.2f}` (Entry: `{sp['entry_z']:+.2f}`)\n\n"
            f"Leg 1 ({LEG_1_NAME}, `{sp['leg_1']['side'].upper()}`):\n"
            f"Entry `${sp['leg_1']['entry']:.2f}` → Now `${px1:.2f}` | PnL: `${pnl1:+.2f}`\n\n"
            f"Leg 2 ({LEG_2_NAME}, `{sp['leg_2']['side'].upper()}`):\n"
            f"Entry `${sp['leg_2']['entry']:.2f}` → Now `${px2:.2f}` | PnL: `${pnl2:+.2f}`\n\n"
            f"💵 *Total Spread PnL:* `${total_pnl:+.2f}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"[ERROR] /pos: {e}", exc_info=True)


async def close_command(update: Update, context):
    try:
        stats = load_state()
        if not stats.get("open_spread"):
            await update.message.reply_text("❌ No open spread position")
            return
        res = close_spread_position(stats, "MANUAL CLOSE")
        if not res:
            await update.message.reply_text("❌ Error fetching price from Kraken API")
            return
        msg = (
            f"🔴 *MANUAL SPREAD CLOSE*\n"
            f"Leg 1 PnL: `${res['pnl1']:+.2f}` | Leg 2 PnL: `${res['pnl2']:+.2f}`\n"
            f"*Total PnL:* `${res['total_pnl']:+.2f}`\n"
            f"*Balance:* `${res['balance']:.2f}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"[ERROR] /close: {e}", exc_info=True)


async def shutdown_command(update: Update, context):
    try:
        msg = "🛑 *Bot Shutdown Initiated*\nThe bot will stop in a few seconds..."
        await update.message.reply_text(msg, parse_mode="Markdown")
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
                f"🚀 *Bot started v7.4.0-SPREAD (PAPER)* | Legs: `{LEG_1_NAME}` & `{LEG_2_NAME}`"
            )

            try:
                while not shutdown_event.is_set():
                    try:
                        stats = load_state()

                        today = str(date.today())
                        if stats.get("last_day") != today:
                            stats["last_day"] = today
                            stats["daily_trades"] = 0
                            save_state(stats)

                        # 1. Проверка открытого спреда
                        if stats.get("open_spread"):
                            snap = compute_spread_snapshot()
                            if not snap:
                                await asyncio.sleep(5)
                                continue

                            sp = stats["open_spread"]
                            pnl1, _ = calc_leg_pnl(sp["leg_1"], snap["t1"])
                            pnl2, _ = calc_leg_pnl(sp["leg_2"], snap["t2"])
                            total_pnl = pnl1 + pnl2
                            z = snap["z_score"]

                            reason = None
                            if abs(z) <= EXIT_Z:
                                reason = f"MEAN REVERT (|z|={abs(z):.2f} <= {EXIT_Z})"
                            elif abs(z) >= STOP_Z:
                                reason = f"Z STOP (|z|={abs(z):.2f} >= {STOP_Z})"
                            elif total_pnl <= -abs(MAX_SPREAD_LOSS_USD):
                                reason = f"MAX LOSS STOP (${total_pnl:.2f})"

                            if reason:
                                res = close_spread_position(stats, reason, snap)
                                if res:
                                    send_telegram(
                                        f"🔔 *SPREAD CLOSE* — {res['reason']}\n"
                                        f"Leg 1: `${res['pnl1']:+.2f}` | Leg 2: `${res['pnl2']:+.2f}`\n"
                                        f"*Total PnL:* `${res['total_pnl']:+.2f}` | *Balance:* `${res['balance']:.2f}`"
                                    )
                                continue

                            await asyncio.sleep(10)
                            continue

                        # 2. Фильтры лимита сделок и кулдауна
                        if stats.get("daily_trades", 0) >= MAX_TRADES_PER_DAY:
                            await asyncio.sleep(60)
                            continue

                        if time.time() - stats.get("last_stop_time", 0) < COOLDOWN_HOURS * 3600:
                            await asyncio.sleep(60)
                            continue

                        # 3. Рыночные данные и фильтры
                        snap = compute_spread_snapshot()
                        if not snap:
                            await asyncio.sleep(30)
                            continue

                        price1 = snap["t1"]["last"]
                        if snap["avg_vol1"] > 0 and snap["last_vol1"] < snap["avg_vol1"] * 0.55:
                            logging.info("SKIP: low relative volume")
                            await asyncio.sleep(60)
                            continue

                        if snap["atr1_pct"] < MIN_ATR_PCT:
                            logging.info("SKIP: ATR too low (flat market)")
                            await asyncio.sleep(60)
                            continue

                        deriv = get_derivatives_data()
                        funding_rate = deriv.get("funding_rate", 0.0)
                        z = snap["z_score"]
                        trend = snap["trend1"]

                        # 4. Вход в спред при достижении порога |z| >= ENTRY_Z
                        direction = None
                        if z <= -ENTRY_Z:
                            if TREND_FILTER and trend == "down":
                                logging.info("SKIP 1L_2S: downtrend")
                                await asyncio.sleep(60)
                                continue
                            if funding_rate > MAX_FUNDING_LONG:
                                logging.info("SKIP 1L_2S: funding too high")
                                await asyncio.sleep(60)
                                continue
                            direction = "1L_2S"  # Long Leg 1 + Short Leg 2

                        elif z >= ENTRY_Z:
                            if TREND_FILTER and trend == "up":
                                logging.info("SKIP 1S_2L: uptrend")
                                await asyncio.sleep(60)
                                continue
                            if funding_rate < MIN_FUNDING_SHORT:
                                logging.info("SKIP 1S_2L: funding too low")
                                await asyncio.sleep(60)
                                continue
                            direction = "1S_2L"  # Short Leg 1 + Long Leg 2

                        if direction:
                            usd1, usd2 = calc_atr_notionals(snap)
                            if direction == "1L_2S":
                                s1, px1 = "buy", snap["t1"]["ask"]
                                s2, px2 = "sell", snap["t2"]["bid"]
                            else:
                                s1, px1 = "sell", snap["t1"]["bid"]
                                s2, px2 = "buy", snap["t2"]["ask"]

                            vol1 = usd1 / px1
                            vol2 = usd2 / px2

                            stats["open_spread"] = {
                                "direction": direction,
                                "entry_z": z,
                                "entry_spread": snap["spread"],
                                "leg_1": {
                                    "symbol": LEG_1_SYMBOL,
                                    "side": s1,
                                    "entry": px1,
                                    "volume": vol1,
                                    "notional_usd": usd1,
                                },
                                "leg_2": {
                                    "symbol": LEG_2_SYMBOL,
                                    "side": s2,
                                    "entry": px2,
                                    "volume": vol2,
                                    "notional_usd": usd2,
                                },
                            }
                            stats["daily_trades"] = stats.get("daily_trades", 0) + 1
                            save_state(stats)

                            send_telegram(
                                f"⚖️ *OPEN SPREAD ({direction})*\n"
                                f"Z-Score: `{z:+.2f}` | Funding: `{funding_rate*100:+.4f}%`\n"
                                f"Leg 1 ({LEG_1_NAME}): `{s1.upper()}` @ `${px1:.2f}` (`${usd1:.0f}`)\n"
                                f"Leg 2 ({LEG_2_NAME}): `{s2.upper()}` @ `${px2:.2f}` (`${usd2:.0f}`)"
                            )
                            logging.info(f"[SPREAD] OPEN {direction} at z={z:+.2f}")

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