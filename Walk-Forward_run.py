import json
import os
import requests
import numpy as np
import pandas as pd

KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15
FEE_PCT = 0.0026
RISK_PCT = 0.01
MAX_LEVERAGE = 1.0
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5
TAKE_PROFIT_PCT = 0.015
MIN_ATR_PCT = 0.00045
TREND_FILTER = True
COOLDOWN_BARS = 16
MAX_TRADES_PER_DAY = 3
STATE_FILE = "bot_state.json"


def load_bot_state(state_path: str = STATE_FILE) -> dict:
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Read error {state_path}: {e}")
    return {}


def get_ohlc(symbol: str = KRAKEN_SYMBOL, interval: int = TIMEFRAME, limit: int = 720):
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


def prepare_indicators(ohlc_data: dict, state_path: str = STATE_FILE) -> pd.DataFrame:
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

    # Расчёт скользящих индикаторов один раз по всему ряду строго назад (без lookahead)
    df["ma20"] = df["close"].rolling(20).mean()
    df["std20"] = df["close"].rolling(20).std(ddof=0)
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

    df.attrs["bot_state"] = load_bot_state(state_path)
    return df


def run_bot_strategy_window(
    df: pd.DataFrame,
    num_std: float = 2.0,
    atr_mult: float = ATR_MULTIPLIER,
    tp_pct: float = TAKE_PROFIT_PCT,
    fee_pct: float = FEE_PCT,
    start_balance: float = 1000.0,
) -> dict:
    if len(df) < 5:
        return {"return_pct": 0.0, "max_dd_pct": 0.0, "trades": 0, "wins": 0}

    d = df.copy().reset_index(drop=True)
    d["upper"] = d["ma20"] + num_std * d["std20"]
    d["lower"] = d["ma20"] - num_std * d["std20"]

    balance = float(start_balance)
    peak_balance = balance
    max_dd = 0.0
    pos = None
    last_stop_bar = -999
    trades = 0
    wins = 0
    daily_counts = {}

    # Старт с 1-го бара окна (индикаторы уже прогреты на истории до окна)
    for i in range(1, len(d)):
        bar = d.iloc[i]
        prev = d.iloc[i - 1]
        day_key = bar["time"].date()
        daily_counts.setdefault(day_key, 0)

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

        if daily_counts[day_key] >= MAX_TRADES_PER_DAY:
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

        vol_spike = prev["volume"] > prev["avg_vol"]
        entry_price = float(bar["open"])
        sl_dist = float(prev["atr"]) * atr_mult
        if sl_dist <= 0 or entry_price <= 0:
            continue

        long_cond = (prev["close"] <= prev["lower"]) or (
            prev["close"] > prev["vwap"] and vol_spike and trend == "up" and prev["close"] < prev["upper"]
        )
        short_cond = (prev["close"] >= prev["upper"]) or (
            prev["close"] < prev["vwap"] and vol_spike and trend == "down" and prev["close"] > prev["lower"]
        )

        if long_cond and not (TREND_FILTER and trend == "down"):
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
            daily_counts[day_key] += 1
        elif short_cond and not (TREND_FILTER and trend == "up"):
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
    candidates = [
        (2.0, 1.2),
        (2.0, 1.5),
        (2.0, 1.8),
        (2.2, 1.5),
    ]
    rows = []

    # Начинаем с индекса 50, чтобы индикаторы в 1-м train-окне уже были рассчитаны
    for start in range(50, len(df) - train_bars - test_bars + 1, test_bars):
        train_df = df.iloc[start : start + train_bars]
        test_df = df.iloc[start + train_bars : start + train_bars + test_bars]

        best_cfg = (2.0, 1.5)
        best_train_ret = -np.inf
        for std_k, atr_m in candidates:
            res_tr = run_bot_strategy_window(train_df, num_std=std_k, atr_mult=atr_m, fee_pct=fee_pct)
            if res_tr["return_pct"] > best_train_ret:
                best_train_ret = res_tr["return_pct"]
                best_cfg = (std_k, atr_m)

        std_k, atr_m = best_cfg
        res_oos = run_bot_strategy_window(test_df, num_std=std_k, atr_mult=atr_m, fee_pct=fee_pct)
        wr_oos = (res_oos["wins"] / res_oos["trades"] * 100.0) if res_oos["trades"] > 0 else 0.0

        rows.append({
            "window": len(rows) + 1,
            "bb_std": std_k,
            "atr_mult": atr_m,
            "train_ret_%": round(best_train_ret, 2),
            "oos_ret_%": round(res_oos["return_pct"], 2),
            "oos_dd_%": round(res_oos["max_dd_pct"], 2),
            "oos_trades": res_oos["trades"],
            "oos_WR_%": round(wr_oos, 1),
        })

    return pd.DataFrame(rows)


if __name__ == "__main__":
    raw_ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME, limit=720)
    df_candles = prepare_indicators(raw_ohlc, state_path=STATE_FILE)

    state = df_candles.attrs.get("bot_state", {})
    tp_from_state = float(state.get("dynamic_tp_pct", TAKE_PROFIT_PCT))
    TAKE_PROFIT_PCT = tp_from_state

    print(f"Loaded candles: {len(df_candles)} | TP={TAKE_PROFIT_PCT*100:.2f}% | MaxLev={MAX_LEVERAGE}x")
    print(
        f"JSON ({STATE_FILE}): balance=${state.get('paper_balance', 0):.2f} | "
        f"trades={state.get('total_trades', 0)} | wins={state.get('total_wins', 0)} | "
        f"PnL=${state.get('total_pnl', 0):.2f}"
    )

    wf_report = walk_forward_bot(df_candles, train_bars=240, test_bars=96, fee_pct=FEE_PCT)
    print("\n--- WALK-FORWARD BOT STRATEGY REPORT (v2) ---")
    print(wf_report.to_string(index=False))