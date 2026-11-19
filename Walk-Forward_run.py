import json
import os
import requests
import numpy as np
import pandas as pd

# --- ПАРАМЕТРЫ ИЗ ВАШЕГО БОТА (v7.3.0.1 / ATR) ---
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15              # 15 минут
FEE_PCT = 0.0026            # комиссия Kraken 0.26%
RISK_PCT = 0.01             # 1% риска на сделку
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5        # множитель ATR для стоп-лосса
TAKE_PROFIT_PCT = 0.025     # 2.5% тейк-профит
MIN_ATR_PCT = 0.00045       # анти-флэт порог ATR/price
TREND_FILTER = True         # запрет контртренда по SMA20/SMA50
COOLDOWN_BARS = 16          # 4 часа = 16 свечей по 15m
MAX_TRADES_PER_DAY = 3      # лимит сделок в сутки
STATE_FILE = "bot_state.json"


def load_bot_state(state_path: str = STATE_FILE) -> dict:
    """Подключение и чтение JSON-файла состояния бота."""
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Ошибка чтения {state_path}: {e}")
    return {}


def get_ohlc(symbol: str = KRAKEN_SYMBOL, interval: int = TIMEFRAME, limit: int = 720) -> dict | None:
    """Загрузка исторических свечей OHLCV с публичного API Kraken."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval={interval}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()

    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    result = data.get("result", {})
    pair_key = next((k for k in result.keys() if k != "last"), None)
    if not pair_key:
        return None

    ohlc_data = result[pair_key][-limit:]
    return {
        "times": [int(c[0]) for c in ohlc_data],
        "open": [float(c[1]) for c in ohlc_data],
        "highs": [float(c[2]) for c in ohlc_data],
        "lows": [float(c[3]) for c in ohlc_data],
        "closes": [float(c[4]) for c in ohlc_data],
        "volumes": [float(c[6]) for c in ohlc_data],
    }


def build_candles_dataframe(ohlc_data: dict, state_path: str = STATE_FILE) -> pd.DataFrame:
    """Очистка данных (Data Sanitation) и сборка DataFrame."""
    df = pd.DataFrame({
        "time": pd.to_datetime(ohlc_data["times"], unit="s"),
        "open": pd.Series(ohlc_data["open"], dtype="float64"),
        "high": pd.Series(ohlc_data["highs"], dtype="float64"),
        "low": pd.Series(ohlc_data["lows"], dtype="float64"),
        "close": pd.Series(ohlc_data["closes"], dtype="float64"),
        "volume": pd.Series(ohlc_data["volumes"], dtype="float64"),
    })

    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)].reset_index(drop=True)

    state = load_bot_state(state_path)
    df.attrs["bot_state"] = state
    return df


def run_bot_strategy_window(
    df: pd.DataFrame,
    num_std: float = 2.0,
    atr_mult: float = ATR_MULTIPLIER,
    tp_pct: float = TAKE_PROFIT_PCT,
    fee_pct: float = FEE_PCT,
    start_balance: float = 1000.0,
) -> dict:
    """
    Симуляция боевой логики бота (BB + VWAP + Trend + ATR SL + TP + Cooldown + Daily Limit)
    на заданном окне свечей без lookahead bias.
    """
    if len(df) < 55:
        return {"return_pct": 0.0, "max_dd_pct": 0.0, "trades": 0, "wins": 0}

    d = df.copy().reset_index(drop=True)

    # Индикаторы (строго по прошлым свечам)
    d["ma20"] = d["close"].rolling(20).mean()
    d["std20"] = d["close"].rolling(20).std(ddof=0)
    d["upper"] = d["ma20"] + num_std * d["std20"]
    d["lower"] = d["ma20"] - num_std * d["std20"]
    d["avg_vol"] = d["volume"].rolling(20).mean()

    # Тренд (как в вашем get_trend)
    d["sma20"] = d["close"].rolling(20).mean()
    d["sma50"] = d["close"].rolling(50).mean()

    # VWAP за 20 свечей
    tp_price = (d["high"] + d["low"] + d["close"]) / 3.0
    d["vwap"] = (tp_price * d["volume"]).rolling(20).sum() / d["volume"].rolling(20).sum().replace(0, np.nan)

    # ATR(14)
    prev_close = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["atr"] = tr.rolling(ATR_PERIOD).mean()

    balance = float(start_balance)
    peak_balance = balance
    max_dd = 0.0

    pos = None  # {"side": "buy"/"sell", "entry": float, "sl": float, "tp": float, "qty": float}
    last_stop_bar = -999
    trades = 0
    wins = 0
    daily_counts = {}

    # Идём по барам: решение о входе принимаем по закрытому бару i-1, исполняем по open бара i
    for i in range(50, len(d)):
        bar = d.iloc[i]
        prev = d.iloc[i - 1]
        day_key = bar["time"].date()
        daily_counts.setdefault(day_key, 0)

        # 1. Проверка открытой позиции по экстремумам текущего закрытого бара
        if pos is not None:
            exit_price = None
            is_stop = False

            if pos["side"] == "buy":
                if bar["low"] <= pos["sl"]:
                    exit_price = pos["sl"]
                    is_stop = True
                elif bar["high"] >= pos["tp"]:
                    exit_price = pos["tp"]
            else:  # sell
                if bar["high"] >= pos["sl"]:
                    exit_price = pos["sl"]
                    is_stop = True
                elif bar["low"] <= pos["tp"]:
                    exit_price = pos["tp"]

            if exit_price is not None:
                gross_pnl = (
                    (exit_price - pos["entry"]) * pos["qty"]
                    if pos["side"] == "buy"
                    else (pos["entry"] - exit_price) * pos["qty"]
                )
                # Учитываем комиссию входа и выхода
                fees = (pos["entry"] + exit_price) * pos["qty"] * fee_pct
                net_pnl = gross_pnl - fees

                balance += net_pnl
                trades += 1
                if net_pnl > 0:
                    wins += 1
                if is_stop:
                    last_stop_bar = i

                peak_balance = max(peak_balance, balance)
                dd_now = (balance - peak_balance) / peak_balance if peak_balance > 0 else 0.0
                max_dd = min(max_dd, dd_now)
                pos = None
            continue

        # 2. Фильтры ограничения входа (по закрытому бару i-1)
        if daily_counts[day_key] >= MAX_TRADES_PER_DAY:
            continue
        if (i - last_stop_bar) < COOLDOWN_BARS:
            continue
        if pd.isna(prev["atr"]) or pd.isna(prev["vwap"]) or pd.isna(prev["upper"]):
            continue
        if prev["volume"] < prev["avg_vol"] * 0.55:
            continue
        if prev["atr"] <= 0 or (prev["atr"] / prev["close"]) < MIN_ATR_PCT:
            continue

        # Определение тренда на баре i-1
        if prev["sma20"] < prev["sma50"] * 0.997:
            trend = "down"
        elif prev["sma20"] > prev["sma50"] * 1.003:
            trend = "up"
        else:
            trend = "range"

        vol_spike = prev["volume"] > prev["avg_vol"]
        entry_price = float(bar["open"])  # вход на открытии следующего бара (без lookahead)
        sl_dist = float(prev["atr"]) * atr_mult
        if sl_dist <= 0 or entry_price <= 0:
            continue

        # 3. Условия входа LONG / SHORT (BB + VWAP-пробой по тренду)
        long_cond = (prev["close"] <= prev["lower"]) or (
            prev["close"] > prev["vwap"] and vol_spike and trend == "up" and prev["close"] < prev["upper"]
        )
        short_cond = (prev["close"] >= prev["upper"]) or (
            prev["close"] < prev["vwap"] and vol_spike and trend == "down" and prev["close"] > prev["lower"]
        )

        if long_cond and not (TREND_FILTER and trend == "down"):
            risk_usd = balance * RISK_PCT
            qty = risk_usd / sl_dist
            pos = {
                "side": "buy",
                "entry": entry_price,
                "sl": entry_price - sl_dist,
                "tp": entry_price * (1.0 + tp_pct),
                "qty": qty,
            }
            daily_counts[day_key] += 1

        elif short_cond and not (TREND_FILTER and trend == "up"):
            risk_usd = balance * RISK_PCT
            qty = risk_usd / sl_dist
            pos = {
                "side": "sell",
                "entry": entry_price,
                "sl": entry_price + sl_dist,
                "tp": entry_price * (1.0 - tp_pct),
                "qty": qty,
            }
            daily_counts[day_key] += 1

    ret_pct = ((balance - start_balance) / start_balance) * 100.0
    return {
        "return_pct": float(ret_pct),
        "max_dd_pct": float(max_dd * 100.0),
        "trades": trades,
        "wins": wins,
    }


def walk_forward_bot(
    df: pd.DataFrame,
    train_bars: int = 240,
    test_bars: int = 96,
    fee_pct: float = FEE_PCT,
) -> pd.DataFrame:
    """Скользящий Walk-Forward с калиб