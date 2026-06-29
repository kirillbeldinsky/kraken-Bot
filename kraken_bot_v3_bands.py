import requests
import time
import logging
import pandas as pd
import numpy as np
from datetime import date
import json
import os

STATE_FILE = "bot_state.json"

# --- НАСТРОЙКИ СИСТЕМЫ ---
SYMBOL = "XBTUSD"
TIMEFRAME = 15
EMA_PERIOD = 50
REGIME_SMA = 200
TRADE_STAGE_LIMIT = 100
PAPER_BALANCE_START = 1000.0
RISK_PCT = 0.01
PULLBACK_PCT = 0.005
TP_PCT_RANGE = 0.015  # 1.5% тейк для боковика

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    filename="trading_bot.log",
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---
stats = {
    "total_longs": 0,
    "total_shorts": 0,
    "daily_loss_r": 0.0,
    "last_day": date.today(),
    "paper_balance": PAPER_BALANCE_START,
    "open_position": None,
    "last_exit_price": 0.0,
    "current_regime": "range",
}

if os.path.exists(STATE_FILE):
    try:
        saved = json.load(open(STATE_FILE))
        stats.update(saved)
        stats["last_day"] = date.fromisoformat(saved["last_day"])
    except Exception:
        pass

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump({**stats, "last_day": str(stats["last_day"])}, f)

# --- БЛОК 1: DATA AGENT ---
def get_market_data():
    try:
        url = "https://api.kraken.com/0/public/OHLC"
        params = {"pair": SYMBOL, "interval": TIMEFRAME}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            logging.error(f"API error: {data['error']}")
            return []
        pair_key = list(data["result"].keys())[0]
        candles = data["result"][pair_key]
        return [float(c[4]) for c in candles]
    except Exception as e:
        logging.error(f"Data Agent: {e}")
        return []

def get_paper_balance():
    return stats["paper_balance"]

# --- БЛОК 2: SIGNAL AGENT ---
def calculate_ema(prices, period):
    return pd.Series(np.array(prices)).ewm(span=period, adjust=False).mean().iloc[-1]

def detect_regime(prices):
    if len(prices) < REGIME_SMA:
        return "range"
    sma200 = pd.Series(prices).rolling(REGIME_SMA).mean().iloc[-1]
    price = prices[-1]
    if price > sma200 * 1.05:
        return "trend"
    return "range"

def signal_trend(prices):
    if len(prices) < EMA_PERIOD + 1:
        return "HOLD", 0, 0, 0
    ema = float(calculate_ema(prices, EMA_PERIOD))
    prev = float(prices[-1])
    older = float(prices[-2])
    if older <= ema and prev > ema and prev < ema * (1 + PULLBACK_PCT):
        return "BUY", 0, 0, 0
    return "HOLD", 0, 0, 0

def signal_range(prices):
    if len(prices) < 20:
        return "HOLD", 0, 0, 0
    series = pd.Series(prices)
    sma = series.rolling(20).mean().iloc[-1]
    std = series.rolling(20).std().iloc[-1]
    lower = sma - 2*std
    upper = sma + 2*std
    price = prices[-1]
    signal = "HOLD"
    if price < lower:
        signal = "BUY"
    if price > upper:
        signal = "SELL"
    return signal, round(sma,1), round(lower,1), round(upper,1)

def check_signals(prices):
    regime = detect_regime(prices)
    stats["current_regime"] = regime
    if regime == "trend":
        signal, sma, low, high = signal_trend(prices)
        logging.info(f"Режим={regime} | EMA50={calculate_ema(prices, EMA_PERIOD):.1f} | Signal={signal} | Close={prices[-1]:.1f}")
    else:
        signal, sma, low, high = signal_range(prices)
        logging.info(f"Режим={regime} | SMA20={sma} | Low={low} | High={high} | Signal={signal} | Close={prices[-1]:.1f}")
    return signal

# --- БЛОК 3: RISK AGENT ---
def reset_daily_if_needed():
    today = date.today()
    if stats["last_day"] != today:
        stats["daily_loss_r"] = 0.0
        stats["last_day"] = today
        logging.info("Новый день, дневной счетчик сброшен")

def manage_risks(signal, current_price):
    reset_daily_if_needed()
    if signal == "HOLD":
        return None
    if stats["open_position"] is not None:
        logging.info("Пропуск входа | причина: позиция уже открыта")
        return None
    if stats["daily_loss_r"] >= 2.0:
        logging.info("Пропуск входа | дневной лимит 2R достигнут")
        return None
    
    balance = get_paper_balance()
    risk_usd = balance * RISK_PCT
    
    if signal == "BUY":
        if stats["last_exit_price"] > 0 and current_price > stats["last_exit_price"] * 1.002:
            logging.info(f"Пропуск BUY | выше прошлого выхода {stats['last_exit_price']}")
            return None
        sl = current_price * 0.985
        vol = risk_usd / max(current_price - sl, 1)
        return {"side": "buy", "volume": round(vol,4), "stop_loss": round(sl,2), "entry": current_price}
    
    if signal == "SELL":
        if stats["last_exit_price"] > 0 and current_price < stats["last_exit_price"] * 0.998:
            logging.info(f"Пропуск SELL | ниже прошлого выхода {stats['last_exit_price']}")
            return None
        sl = current_price * 1.015  # стоп выше на 1.5% для шорта
        vol = risk_usd / max(sl - current_price, 1)
        return {"side": "sell", "volume": round(vol,4), "stop_loss": round(sl,2), "entry": current_price}
    
    return None

# --- EXIT ---
def check_exit(current_price):
    pos = stats["open_position"]
    if not pos:
        return
    entry, sl, vol, side = pos["entry"], pos["sl"], pos["volume"], pos["side"]
    
    # Стоп-лосс
    stop_hit = current_price <= sl if side == "buy" else current_price >= sl
    if stop_hit:
        pnl = (current_price - entry) * vol if side == "buy" else (entry - current_price) * vol
        stats["paper_balance"] += pnl
        stats["daily_loss_r"] += abs(pnl) / (PAPER_BALANCE_START * RISK_PCT)
        stats["last_exit_price"] = current_price
        logging.info(f"[CLOSE] STOP {side.upper()} PnL=${pnl:.2f}")
        stats["open_position"] = None
        save_state()
        return
    
    # Тейк-профит для range: +-1.5%
    if stats["current_regime"] == "range":
        tp = entry * (1 + TP_PCT_RANGE) if side == "buy" else entry * (1 - TP_PCT_RANGE)
        tp_hit = current_price >= tp if side == "buy" else current_price <= tp
        if tp_hit:
            pnl = (current_price - entry) * vol if side == "buy" else (entry - current_price) * vol
            stats["paper_balance"] += pnl
            stats["last_exit_price"] = current_price
            logging.info(f"[CLOSE] TP {side.upper()} PnL=${pnl:.2f}")
            stats["open_position"] = None
            save_state()
            return
        
    # Трейлинг-стоп
    if side == "buy" and current_price > entry * 1.005:
        new_sl = current_price * 0.985
        if new_sl > sl:
            pos["sl"] = round(new_sl,2)
            logging.info(f"[TRAIL] BUY Новый стоп={pos['sl']}")
            save_state()
    elif side == "sell" and current_price < entry * 0.995:
        new_sl = current_price * 1.015
        if new_sl < sl:
            pos["sl"] = round(new_sl,2)
            logging.info(f"[TRAIL] SELL Новый стоп={pos['sl']}")
            save_state()

# --- EXECUTION ---
def execute_order(order):
    if not order or stats["open_position"]:
        return
    side = order["side"]
    if side == "buy":
        stats["total_longs"] += 1
    else:
        stats["total_shorts"] += 1
    stats["open_position"] = {
        "side": side,
        "entry": order["entry"],
        "sl": order["stop_loss"],
        "volume": order["volume"]
    }
    logging.info(f"[EXEC] {side.upper()} {order['volume']} @ {order['entry']} | SL={order['stop_loss']}")
    save_state()

# --- MAIN ---
def run_trading_bot():
    logging.info("=== Старт бота v3: шорты + TP + логи полос ===")
    while True:
        prices = get_market_data()
        if prices:
            cp = prices[-1]
            check_exit(cp)
            sig = check_signals(prices)
            ord = manage_risks(sig, cp)
            execute_order(ord)
        time.sleep(900)

if __name__ == "__main__":
    run_trading_bot()
