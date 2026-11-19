import json
import os
import time
import requests
import numpy as np
import pandas as pd

KRAKEN_SYMBOL = "XXBTZUSD"
BINANCE_SYMBOL = "BTCUSDT"
TIMEFRAME = 15
FEE_MAKER = 0.0016              # 0.16% Maker (лимитный вход и TP)
FEE_TAKER = 0.0026              # 0.26% Taker (рыночный SL)
RISK_PCT = 0.01
MAX_LEVERAGE = 1.0
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5
TAKE_PROFIT_PCT = 0.015
MIN_ATR_PCT = 0.00045
TREND_FILTER = True
COOLDOWN_BARS = 16
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_STOPS = 2       # Daily Circuit Breaker
MAX_DAILY_LOSS_PCT = 0.02
LIMIT_FILL_SLIPPAGE = 0.0       # Offset = 0.0 (по лучшему bid/ask)

# --- ПАРАМЕТРЫ ФИЛЬТРА РАСШИРЕНИЯ ВОЛАТИЛЬНОСТИ (АНТИ-ИМПУЛЬС) ---
BB_EXPANSION_MAX = 1.35         # ширина BB не должна превышать среднюю за 10 баров более чем на 35%
IMPULSE_BAR_ATR_MAX = 2.2       # размах свечи (high - low) не должен превышать 2.2 * ATR

STATE_FILE = "bot_state.json"
CACHE_CSV = "candles_30d.csv"


def load_bot_state(state_path: str = STATE_FILE) -> dict:
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Read error {state_path}: {e}")
    return {}


def fetch_30d_ohlc(days: int = 30, interval_min: int = 15) -> pd.DataFrame:
    total_bars = days * 24 * (60 // interval_min)
    end_ms = int(time.time() * 1000)
    step_ms = interval_min * 60 * 1000
    endpoints = [
        "https://api.binance.com/api/v3/klines",
        "https://api.binance.us/api/v3/klines",
    ]
    for base_url in endpoints:
        all_rows = []
        cur_end = end_ms
        try:
            while len(all_rows) < total_bars:
                batch_limit = min(1000, total_bars - len(all_rows))
                params = {
                    "symbol": BINANCE_SYMBOL,
                    "interval": f"{interval_min}m",
                    "limit": batch_limit,
                    "endTime": cur_end,
                }
                r = requests.get(base_url, params=params, timeout=15)
                r.raise_for_status()
                raw = r.json()
                if not raw or not isinstance(raw, list):
                    break
                all_rows = raw + all_rows
                cur_end = int(raw[0][0]) - step_ms
                time.sleep(0.15)
            if len(all_rows) >= 720:
                df = pd.DataFrame({
                    "time": pd.to_datetime([int(c[0]) // 1000 for c in all_rows], unit="s"),
                    "open": [float(c[1]) for c in all_rows],
                    "high": [float(c[2]) for c in all_rows],
                    "low": [float(c[3]) for c in all_rows],
                    "close": [float(c[4]) for c in all_rows],
                    "volume": [float(c[5]) for c in all_rows],
                })
                return df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
        except Exception as e:
            print(f"[INFO] Endpoint {base_url} unavailable ({e}), trying fallback...")

    url = f"https://api.kraken.com/0/public/OHLC?pair={KRAKEN_SYMBOL}&interval={interval_min}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    result = data.get("result", {})
    pair_key = next((k for k in result.keys() if k != "last"), None)
    ohlc_data = result[pair_key][-720:]
    return pd.DataFrame({
        "time": pd.to_datetime([int(c[0]) for c in ohlc_data], unit="s"),
        "open": [float(c[1]) for c in ohlc_data],
        "high": [float(c[2]) for c in ohlc_data],
        "low": [float(c[3]) for c in ohlc_data],
        "close": [float(c[4]) for c in ohlc_data],
        "volume": [float(c[6]) for c in ohlc_data],
    })


def prepare_indicators(df: pd.DataFrame, state_path: str = STATE_FILE) -> pd.DataFrame:
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)].reset_index(drop=True)

    df["ma20"] = df["close"].rolling(20).mean()
    df["std20"] = df["close"].rolling(20).std(ddof=0)
    df["std20_ma10"] = df["std20"].rolling(10).mean()
    df["avg_vol"] = df["volume"].rolling(20).mean()
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma50"] = df["close"].rolling(50).mean()

    tp_price = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap"] = (tp_price * df["volume"]).rolling(20).sum() / df["volume"].rolling(20).sum().replace(0, np.nan)

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(ATR_PERIOD).mean()
    df["bar_range"] = df["high"] - df["low"]

    df.attrs["bot_state"] = load_bot_state(state_path)
    return df


def run_bot_strategy_window(
    df: pd.DataFrame,
    num_std: float = 2.2,
    atr_mult: float = ATR_MULTIPLIER,
    tp_pct: float = TAKE_PROFIT_PCT,
    start_balance: float = 1000.0,
    carry_pos: dict | None = None,
    use_anti_impulse: bool = True,
) -> dict:
    if len(df) < 5:
        return {
            "return_pct": 0.0,
            "max_dd_pct": 0.0,
            "trades": 0,
            "wins": 0,
            "blocked_impulse": 0,
            "end_balance": start_balance,
            "open_pos": carry_pos,
        }

    d = df.copy().reset_index(drop=True)
    d["upper"] = d["ma20"] + num_std * d["std20"]
    d["lower"] = d["ma20"] - num_std * d["std20"]

    balance = float(start_balance)
    peak_balance = balance
    max_dd = 0.0
    pos = dict(carry_pos) if carry_pos is not None else None
    last_stop_bar = -999
    trades = 0
    wins = 0
    blocked_impulse = 0

    cur_day = None
    day_trades = 0
    day_pnl = 0.0
    day_stop_streak = 0
    day_start_bal = balance

    for i in range(1, len(d)):
        bar = d.iloc[i]
        prev = d.iloc[i - 1]
        bar_day = bar["time"].date()

        if bar_day != cur_day:
            cur_day = bar_day
            day_trades = 0
            day_pnl = 0.0
            day_stop_streak = 0
            day_start_bal = balance

        if pos is not None:
            exit_price = None
            is_stop = False
            if pos["side"] == "buy":
                if bar["low"] <= pos["sl"]:
                    exit_price = pos["sl"]
                    is_stop = True
                elif bar["high"] >= pos["tp"]:
                    exit_price = pos["tp"]
            else:
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
                entry_fee = pos["entry"] * pos["qty"] * FEE_MAKER
                exit_fee = exit_price * pos["qty"] * (FEE_TAKER if is_stop else FEE_MAKER)
                net_pnl = gross_pnl - (entry_fee + exit_fee)

                balance += net_pnl
                day_pnl += net_pnl
                trades += 1
                if net_pnl > 0:
                    wins += 1
                    day_stop_streak = 0
                if is_stop:
                    last_stop_bar = i
                    day_stop_streak += 1

                peak_balance = max(peak_balance, balance)
                dd_now = (balance - peak_balance) / peak_balance if peak_balance > 0 else 0.0
                max_dd = min(max_dd, dd_now)
                pos = None
            continue

        # Daily Circuit Breaker
        if day_stop_streak >= MAX_CONSECUTIVE_STOPS:
            continue
        if day_pnl <= -(day_start_bal * MAX_DAILY_LOSS_PCT):
            continue
        if day_trades >= MAX_TRADES_PER_DAY:
            continue
        if (i - last_stop_bar) < COOLDOWN_BARS:
            continue
        if pd.isna(prev["atr"]) or pd.isna(prev["vwap"]) or pd.isna(prev["upper"]) or pd.isna(prev["sma50"]):
            continue
        if prev["volume"] < prev["avg_vol"] * 0.55:
            continue
        if prev["atr"] <= 0 or (prev["atr"] / prev["close"]) < MIN_ATR_PCT:
            continue

        if prev["sma20"] < prev["sma50"] * 0.997:
            trend = "down"
        elif prev["sma20"] > prev["sma50"] * 1.003:
            trend = "up"
        else:
            trend = "range"

        # Проверка расширения волатильности (Анти-импульс)
        vol_expanding = False
        if use_anti_impulse and not pd.isna(prev["std20_ma10"]) and prev["std20_ma10"] > 0:
            bb_expanding = prev["std20"] > prev["std20_ma10"] * BB_EXPANSION_MAX
            impulse_candle = prev["bar_range"] > prev["atr"] * IMPULSE_BAR_ATR_MAX
            vol_expanding = bb_expanding or impulse_candle

        vol_spike = prev["volume"] > prev["avg_vol"]
        sl_dist = float(prev["atr"]) * atr_mult
        if sl_dist <= 0 or bar["open"] <= 0:
            continue

        bb_long = prev["close"] <= prev["lower"]
        bb_short = prev["close"] >= prev["upper"]
        vwap_long = prev["close"] > prev["vwap"] and vol_spike and trend == "up" and prev["close"] < prev["upper"]
        vwap_short = prev["close"] < prev["vwap"] and vol_spike and trend == "down" and prev["close"] > prev["lower"]

        # Блокируем только контртрендовые отскоки от BB во время взрыва волатильности
        if vol_expanding and (bb_long or bb_short) and not (vwap_long or vwap_short):
            blocked_impulse += 1
            continue

        long_cond = (bb_long and not vol_expanding) or vwap_long
        short_cond = (bb_short and not vol_expanding) or vwap_short

        if long_cond and not (TREND_FILTER and trend == "down"):
            limit_price = float(bar["open"]) * (1.0 - LIMIT_FILL_SLIPPAGE)
            if bar["low"] > limit_price:
                continue
            entry_price = limit_price
            raw_qty = (balance * RISK_PCT) / sl_dist
            max_qty = (balance * MAX_LEVERAGE) / entry_price
            qty = min(raw_qty, max_qty)
            pos = {
                "side": "buy",
                "entry": entry_price,
                "sl": entry_price - sl_dist,
                "tp": entry_price * (1.0 + tp_pct),
                "qty": qty,
            }
            day_trades += 1

        elif short_cond and not (TREND_FILTER and trend == "up"):
            limit_price = float(bar["open"]) * (1.0 + LIMIT_FILL_SLIPPAGE)
            if bar["high"] < limit_price:
                continue
            entry_price = limit_price
            raw_qty = (balance * RISK_PCT) / sl_dist
            max_qty = (balance * MAX_LEVERAGE) / entry_price
            qty = min(raw_qty, max_qty)
            pos = {
                "side": "sell",
                "entry": entry_price,
                "sl": entry_price + sl_dist,
                "tp": entry_price * (1.0 - tp_pct),
                "qty": qty,
            }
            day_trades += 1

    ret_pct = ((balance - start_balance) / start_balance) * 100.0
    return {
        "return_pct": float(ret_pct),
        "max_dd_pct": float(max_dd * 100.0),
        "trades": trades,
        "wins": wins,
        "blocked_impulse": blocked_impulse,
        "end_balance": float(balance),
        "open_pos": pos,
    }


def walk_forward_bot(
    df: pd.DataFrame,
    train_bars: int = 960,
    test_bars: int = 480,
    use_anti_impulse: bool = True,
) -> pd.DataFrame:
    candidates = [
        (2.0, 1.5),
        (2.2, 1.5),
        (2.2, 1.8),
    ]
    rows = []
    oos_balance = 1000.0
    oos_open_pos = None

    for start in range(50, len(df) - train_bars - test_bars + 1, test_bars):
        train_df = df.iloc[start : start + train_bars]
        test_df = df.iloc[start + train_bars : start + train_bars + test_bars]

        best_cfg = (2.2, 1.5)
        best_train_ret = -np.inf
        for std_k, atr_m in candidates:
            res_tr = run_bot_strategy_window(
                train_df,
                num_std=std_k,
                atr_mult=atr_m,
                use_anti_impulse=use_anti_impulse,
            )
            if res_tr["return_pct"] > best_train_ret:
                best_train_ret = res_tr["return_pct"]
                best_cfg = (std_k, atr_m)

        std_k, atr_m = best_cfg
        res_oos = run_bot_strategy_window(
            test_df,
            num_std=std_k,
            atr_mult=atr_m,
            start_balance=oos_balance,
            carry_pos=oos_open_pos,
            use_anti_impulse=use_anti_impulse,
        )
        oos_balance = res_oos["end_balance"]
        oos_open_pos = res_oos["open_pos"]
        wr_oos = (res_oos["wins"] / res_oos["trades"] * 100.0) if res_oos["trades"] > 0 else 0.0

        rows.append({
            "window": len(rows) + 1,
            "bb_std": std_k,
            "atr_mult": atr_m,
            "train_ret_%": round(best_train_ret, 2),
            "oos_ret_%": round(res_oos["return_pct"], 2),
            "oos_dd_%": round(res_oos["max_dd_pct"], 2),
            "trades": res_oos["trades"],
            "blocked": res_oos["blocked_impulse"],
            "WR_%": round(wr_oos, 1),
            "bal_$": round(oos_balance, 2),
        })

    return pd.DataFrame(rows)


if __name__ == "__main__":
    if os.path.exists(CACHE_CSV):
        raw_df = pd.read_csv(CACHE_CSV)
        raw_df["time"] = pd.to_datetime(raw_df["time"])
    else:
        raw_df = fetch_30d_ohlc(days=30, interval_min=TIMEFRAME)
        raw_df.to_csv(CACHE_CSV, index=False)

    df_candles = prepare_indicators(raw_df, state_path=STATE_FILE)
    state = df_candles.attrs.get("bot_state", {})
    TAKE_PROFIT_PCT = float(state.get("dynamic_tp_pct", TAKE_PROFIT_PCT))

    print(f"Loaded candles: {len(df_candles)} | TP={TAKE_PROFIT_PCT*100:.2f}% | Anti-Impulse Filter Test")
    tr_b, te_b = (960, 480) if len(df_candles) >= 2000 else (240, 96)

    rep_prev = walk_forward_bot(df_candles, tr_b, te_b, use_anti_impulse=False)
    print("\n--- 1. v7.4.1 BASELINE (Anti-Impulse = OFF) ---")
    print(rep_prev.to_string(index=False))

    rep_new = walk_forward_bot(df_candles, tr_b, te_b, use_anti_impulse=True)
    print("\n--- 2. v7.4.2 WITH VOLATILITY EXPANSION FILTER (Anti-Impulse = ON) ---")
    print(rep_new.to_string(index=False))