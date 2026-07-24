import json
from datetime import date

DATA_FILE = "kraken_ohlc_180d.json"

# --- РИСК ---
RISK_PCT = 0.01          # 1% на сделку
STOP_LOSS_PCT = 0.015    # 1.5% стоп
TAKE_PROFIT_PCT = 0.025  # 2.5% тейк, RR ~ 1:1.67
TREND_FILTER = True


def load_ohlc():
    with open(DATA_FILE, "r") as f:
        return json.load(f)


# ========== INDICATORS FROM STATE ==========

def get_bb_from_state(state, period=20, std_factor=2):
    closes = state["closes"]
    if len(closes) < period:
        return None, None, None

    ma = sum(closes[-period:]) / period
    variance = sum((c - ma) ** 2 for c in closes[-period:]) / period
    std = variance ** 0.5

    upper = ma + std_factor * std
    lower = ma - std_factor * std
    return upper, ma, lower


def get_atr_from_state(state, period=14):
    highs = state["highs"]
    lows = state["lows"]
    closes = state["closes"]

    if len(highs) < period + 1:
        return 0

    trs = []
    for i in range(-period, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)

    return sum(trs) / period


def get_trend_from_state(state):
    closes = state["closes"]
    if len(closes) < 50:
        return "range"

    ema20 = sum(closes[-20:]) / 20
    ema50 = sum(closes[-50:]) / 50

    if ema20 < ema50 * 0.9985:
        return "down"
    if ema20 > ema50 * 1.0015:
        return "up"
    return "range"


def get_ohlc_from_state(state):
    return {
        "closes": state["closes"],
        "highs": state["highs"],
        "lows": state["lows"],
        "volumes": state["volumes"],
    }


# ========== SIGNAL LOGIC ==========

def get_signal(candle, state):
    price = candle["close"]
    volume = candle["volume"]

    # --- ATR (анти-флэт) ---
    atr = get_atr_from_state(state)
    if atr == 0:
        return None

    atr_pct = atr / price
    if atr_pct < 0.00045:
        return None

    # --- Relative Volume ---
    ohlc = get_ohlc_from_state(state)
    if len(ohlc["volumes"]) < 20:
        return None

    vol_ma = sum(ohlc["volumes"][-20:]) / 20
    if volume < vol_ma * 0.55:
        return None

    # --- Trend ---
    trend = get_trend_from_state(state)

    # --- Bollinger Bands ---
    upper, ma, lower = get_bb_from_state(state)
    if upper is None or lower is None:
        return None

    # --- LONG ENTRY ---
    if price <= lower:
        if TREND_FILTER and trend == "down":
            return None
        return "long"

    # --- SHORT ENTRY ---
    if price >= upper:
        if TREND_FILTER and trend == "up":
            return None
        return "short"

    return None


# ========== POSITION PROCESSING ==========

def process_position(price, pos):
    if pos["side"] == "buy":
        if price <= pos["sl"]:
            pnl = (pos["sl"] - pos["entry"]) * pos["volume"]
            return ("close_long", pnl)
        if price >= pos["tp"]:
            pnl = (pos["tp"] - pos["entry"]) * pos["volume"]
            return ("close_long", pnl)

    if pos["side"] == "sell":
        if price >= pos["sl"]:
            pnl = (pos["entry"] - pos["sl"]) * pos["volume"]
            return ("close_short", pnl)
        if price <= pos["tp"]:
            pnl = (pos["entry"] - pos["tp"]) * pos["volume"]
            return ("close_short", pnl)

    return None


# ========== FULL BACKTEST ==========

def run_full_backtest():
    ohlc = load_ohlc()

    closes, highs, lows, volumes = [], [], [], []

    stats = {
        "paper_balance": 10000.0,
        "open_position": None,
        "daily_trades": 0,
        "last_day": "",
        "last_stop_time": 0,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0,
    }

    for candle in ohlc:
        ts = int(candle[0])
        open_ = float(candle[1])
        high = float(candle[2])
        low = float(candle[3])
        close = float(candle[4])
        volume = float(candle[5])

        closes.append(close)
        highs.append(high)
        lows.append(low)
        volumes.append(volume)

        today = str(date.fromtimestamp(ts))
        if stats["last_day"] != today:
            stats["last_day"] = today
            stats["daily_trades"] = 0

        state = {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
            "ts": ts,
            "stats": stats,
        }

        # проверка открытой позиции
        if stats["open_position"]:
            pos = stats["open_position"]
            result = process_position(close, pos)
            if result:
                action, pnl = result
                stats["paper_balance"] += pnl
                stats["total_trades"] += 1
                if pnl > 0:
                    stats["total_wins"] += 1
                stats["total_pnl"] += pnl
                stats["open_position"] = None
            continue

        # поиск входа
        signal = get_signal(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            state,
        )

        if signal == "long":
            entry = close
            sl = entry * (1 - STOP_LOSS_PCT)
            tp = entry * (1 + TAKE_PROFIT_PCT)
            risk_amount = stats["paper_balance"] * RISK_PCT
            vol = risk_amount / (entry - sl)
            stats["open_position"] = {
                "side": "buy",
                "entry": entry,
                "volume": vol,
                "sl": sl,
                "tp": tp,
            }
            stats["daily_trades"] += 1

        elif signal == "short":
            entry = close
            sl = entry * (1 + STOP_LOSS_PCT)
            tp = entry * (1 - TAKE_PROFIT_PCT)
            risk_amount = stats["paper_balance"] * RISK_PCT
            vol = risk_amount / (sl - entry)
            stats["open_position"] = {
                "side": "sell",
                "entry": entry,
                "volume": vol,
                "sl": sl,
                "tp": tp,
            }
            stats["daily_trades"] += 1

    print("\n===== RESULTS =====")
    print(f"Balance: {stats['paper_balance']:.2f}")
    print(f"Total PnL: {stats['total_pnl']:.2f}")
    print(f"Trades: {stats['total_trades']}")
    print(f"Wins: {stats['total_wins']}")
    if stats["total_trades"] > 0:
        print(f"WR: {stats['total_wins'] / stats['total_trades'] * 100:.1f}%")


if __name__ == "__main__":
    run_full_backtest()


