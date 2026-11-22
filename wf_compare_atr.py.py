import os
import time
import numpy as np
import pandas as pd
import requests

CSV_FILE = "candles_30d.csv"
KRAKEN_SYMBOL = "XXBTZUSD"
BINANCE_SYMBOL = "BTCUSDT"
TIMEFRAME = 15

# Параметры стратегии v7.4.2-ANTI-IMPULSE
BB_PERIOD = 20
BB_STD = 2.2
ATR_PERIOD = 14
TAKE_PROFIT_PCT = 0.015
RISK_PCT = 0.01
MAX_LEVERAGE = 1.0
MIN_ATR_PCT = 0.00045

FEE_MAKER = 0.0016
FEE_TAKER = 0.0026

COOLDOWN_BARS = 16
MAX_TRADES_PER_DAY = 3
MAX_CONSECUTIVE_STOPS = 2
MAX_CROSS_DAY_STOPS = 3
CROSS_DAY_CB_BARS = 48
MAX_DAILY_LOSS_PCT = 0.02

BB_EXPANSION_MAX = 1.35
IMPULSE_BAR_ATR_MAX = 2.2


def _parse_timestamps(raw_series: pd.Series) -> pd.Series:
    """Надёжно парсит колонку времени (unix sec, unix ms или строку даты/времени) в UTC."""
    numeric = pd.to_numeric(raw_series, errors="coerce")
    non_null = raw_series.dropna()
    if len(non_null) > 0 and numeric.notna().sum() == len(non_null):
        med = float(numeric.dropna().abs().median())
        unit = "ms" if med >= 1e11 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    return pd.to_datetime(raw_series.astype(str).str.strip(), utc=True, errors="coerce")


def _fetch_candles(limit_bars: int = 2880) -> pd.DataFrame:
    """Загружает исторические 15m свечи с Binance или резервно с Kraken."""
    end_ms = int(time.time() * 1000)
    for url in ("https://api.binance.us/api/v3/klines", "https://api.binance.com/api/v3/klines"):
        rows = []
        cur_end = end_ms
        try:
            while len(rows) < limit_bars:
                batch_size = min(1000, limit_bars - len(rows))
                resp = requests.get(
                    url,
                    params={
                        "symbol": BINANCE_SYMBOL,
                        "interval": "15m",
                        "limit": batch_size,
                        "endTime": cur_end,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                rows = batch + rows
                cur_end = int(batch[0][0]) - 1
                if len(batch) < batch_size:
                    break
                time.sleep(0.1)

            if len(rows) >= 200:
                sliced = rows[-limit_bars:]
                return pd.DataFrame(
                    {
                        "time": [int(r[0]) // 1000 for r in sliced],
                        "open": [float(r[1]) for r in sliced],
                        "high": [float(r[2]) for r in sliced],
                        "low": [float(r[3]) for r in sliced],
                        "close": [float(r[4]) for r in sliced],
                        "volume": [float(r[5]) for r in sliced],
                    }
                )
        except Exception as exc:
            print(f"[INFO] {url} недоступен: {exc}", flush=True)

    resp = requests.get(
        f"https://api.kraken.com/0/public/OHLC?pair={KRAKEN_SYMBOL}&interval={TIMEFRAME}",
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(f"Kraken API error: {payload['error']}")
    result = payload.get("result", {})
    pair_key = next((k for k in result.keys() if k != "last"), None)
    if not pair_key:
        raise RuntimeError("Не удалось определить пару в ответе Kraken OHLC.")

    ohlc = result[pair_key][-min(720, limit_bars) :]
    return pd.DataFrame(
        {
            "time": [int(c[0]) for c in ohlc],
            "open": [float(c[1]) for c in ohlc],
            "high": [float(c[2]) for c in ohlc],
            "low": [float(c[3]) for c in ohlc],
            "close": [float(c[4]) for c in ohlc],
            "volume": [float(c[6]) for c in ohlc],
        }
    )


def load_candles(csv_path: str = CSV_FILE) -> pd.DataFrame:
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        df = _fetch_candles(limit_bars=2880)
        df.to_csv(csv_path, index=False)

    time_col = next(
        (c for c in ["time", "timestamp", "open_time", "ts", "datetime", "date"] if c in df.columns),
        None,
    )
    if time_col is not None:
        df["dt"] = _parse_timestamps(df[time_col])
    elif isinstance(df.index, pd.DatetimeIndex):
        idx = df.index
        df["dt"] = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    else:
        raise ValueError(f"Не найдена колонка времени. Доступные колонки: {list(df.columns)}")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["dt", "open", "high", "low", "close", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)]
    df = df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
    df["day"] = df["dt"].dt.strftime("%Y-%m-%d")
    return df


def compute_features(df: pd.DataFrame, bb_std: float = BB_STD) -> pd.DataFrame:
    """Считает индикаторы и фильтр анти-импульса строго по прошлым свечам."""
    d = df.copy()
    c = d["close"]
    h = d["high"]
    l = d["low"]
    v = d["volume"]

    ma20 = c.rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
    std20 = c.rolling(BB_PERIOD, min_periods=BB_PERIOD).std(ddof=0)
    d["upper"] = ma20 + bb_std * std20
    d["lower"] = ma20 - bb_std * std20
    d["avg_vol"] = v.rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()

    sma20 = c.rolling(20, min_periods=20).mean()
    sma50 = c.rolling(50, min_periods=50).mean()
    d["trend"] = np.where(
        sma20 < sma50 * 0.997,
        "down",
        np.where(sma20 > sma50 * 1.003, "up", "range"),
    )

    tp = (h + l + c) / 3.0
    v_sum = v.rolling(20, min_periods=20).sum().replace(0, np.nan)
    d["vwap"] = (tp * v).rolling(20, min_periods=20).sum() / v_sum

    prev_c = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()

    avg_std10 = std20.rolling(10, min_periods=10).mean()
    bb_exp = (avg_std10 > 0) & (std20 > avg_std10 * BB_EXPANSION_MAX)
    impulse_bar = (h - l) > (d["atr"] * IMPULSE_BAR_ATR_MAX)
    d["vol_expanding"] = (bb_exp | impulse_bar).fillna(False)
    return d


def simulate_window(
    d: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    atr_mult: float,
    start_balance: float = 1000.0,
) -> dict:
    balance = float(start_balance)
    peak = balance
    max_dd = 0.0

    pos = None
    last_stop_idx = -999999
    consecutive_stops = 0
    cur_day = None
    daily_trades = 0
    daily_pnl = 0.0
    daily_stops = 0

    trades = 0
    wins = 0
    blocked = 0

    for i in range(max(1, start_idx), min(len(d), end_idx)):
        bar = d.iloc[i]
        prev = d.iloc[i - 1]
        day = bar["day"]

        if day != cur_day:
            cur_day = day
            daily_trades = 0
            daily_pnl = 0.0
            daily_stops = 0

        if pos is not None:
            exit_p = None
            is_stop = False
            if pos["side"] == "buy":
                if bar["low"] <= pos["sl"]:
                    exit_p, is_stop = pos["sl"], True
                elif bar["high"] >= pos["tp"]:
                    exit_p, is_stop = pos["tp"], False
            else:
                if bar["high"] >= pos["sl"]:
                    exit_p, is_stop = pos["sl"], True
                elif bar["low"] <= pos["tp"]:
                    exit_p, is_stop = pos["tp"], False

            if exit_p is not None:
                qty = pos["qty"]
                entry_p = pos["entry"]
                gross = (exit_p - entry_p) * qty if pos["side"] == "buy" else (entry_p - exit_p) * qty
                exit_fee = FEE_TAKER if is_stop else FEE_MAKER
                net = gross - (entry_p * qty * FEE_MAKER + exit_p * qty * exit_fee)

                balance += net
                daily_pnl += net
                trades += 1
                if net > 0:
                    wins += 1
                    daily_stops = 0
                    consecutive_stops = 0
                if is_stop:
                    last_stop_idx = i
                    daily_stops += 1
                    consecutive_stops += 1

                peak = max(peak, balance)
                if peak > 0:
                    max_dd = min(max_dd, (balance - peak) / peak)
                pos = None
            continue

        if consecutive_stops >= MAX_CROSS_DAY_STOPS and (i - last_stop_idx) < CROSS_DAY_CB_BARS:
            continue
        if daily_stops >= MAX_CONSECUTIVE_STOPS:
            continue
        if daily_pnl <= -(balance * MAX_DAILY_LOSS_PCT):
            continue
        if daily_trades >= MAX_TRADES_PER_DAY:
            continue
        if (i - last_stop_idx) < COOLDOWN_BARS:
            continue

        c_p = float(prev["close"])
        atr_v = float(prev["atr"])
        lo_v = float(prev["lower"])
        hi_v = float(prev["upper"])
        vwap_v = float(prev["vwap"])
        avg_v = float(prev["avg_vol"])
        vol_v = float(prev["volume"])
        trend = str(prev["trend"])
        vol_exp = bool(prev["vol_expanding"])

        if np.isnan(atr_v) or np.isnan(lo_v) or np.isnan(hi_v) or np.isnan(vwap_v) or atr_v <= 0:
            continue
        if vol_v < avg_v * 0.55 or (atr_v / c_p) < MIN_ATR_PCT:
            continue

        vol_spike = vol_v > avg_v
        bb_long = (c_p <= lo_v) and not (trend == "range" and c_p < lo_v * 0.996)
        bb_short = (c_p >= hi_v) and not (trend == "range" and c_p > hi_v * 1.004)
        vwap_long = c_p > vwap_v and vol_spike and trend == "up" and c_p < hi_v
        vwap_short = c_p < vwap_v and vol_spike and trend == "down" and c_p > lo_v

        if vol_exp and (bb_long or bb_short) and not (vwap_long or vwap_short):
            blocked += 1
            continue

        long_sig = (bb_long and not vol_exp) or vwap_long
        short_sig = (bb_short and not vol_exp) or vwap_short

        entry_p = float(bar["open"])
        sl_dist = atr_v * atr_mult
        if sl_dist <= 0 or entry_p <= sl_dist:
            continue

        qty = min((balance * RISK_PCT) / sl_dist, (balance * MAX_LEVERAGE) / entry_p)
        if qty <= 0:
            continue

        if long_sig and trend != "down":
            pos = {
                "side": "buy",
                "entry": entry_p,
                "qty": qty,
                "sl": entry_p - sl_dist,
                "tp": entry_p * (1.0 + TAKE_PROFIT_PCT),
            }
            daily_trades += 1
        elif short_sig and trend != "up":
            pos = {
                "side": "sell",
                "entry": entry_p,
                "qty": qty,
                "sl": entry_p + sl_dist,
                "tp": entry_p * (1.0 - TAKE_PROFIT_PCT),
            }
            daily_trades += 1

    ret_pct = ((balance - start_balance) / start_balance) * 100.0
    return {
        "return_pct": float(ret_pct),
        "max_dd_pct": float(max_dd * 100.0),
        "trades": trades,
        "wins": wins,
        "blocked": blocked,
        "end_balance": float(balance),
    }


def run_walk_forward(
    df: pd.DataFrame,
    atr_mult: float,
    train_bars: int = 960,
    test_bars: int = 480,
) -> pd.DataFrame:
    if len(df) < train_bars + test_bars:
        test_bars = max(60, len(df) // 4)
        train_bars = max(80, test_bars * 2)

    warmup = 60
    bal = 1000.0
    rows = []
    w_num = 0

    for test_start in range(train_bars, len(df) - test_bars + 1, test_bars):
        w_num += 1
        train_start = max(0, test_start - train_bars)
        test_end = test_start + test_bars

        tr_df = compute_features(df.iloc[train_start:test_start].reset_index(drop=True), BB_STD)
        res_tr = simulate_window(tr_df, warmup, len(tr_df), atr_mult=atr_mult, start_balance=1000.0)

        wu_start = max(0, test_start - warmup)
        oos_df = compute_features(df.iloc[wu_start:test_end].reset_index(drop=True), BB_STD)
        oos_start = test_start - wu_start
        res_oos = simulate_window(oos_df, oos_start, len(oos_df), atr_mult=atr_mult, start_balance=bal)

        bal = res_oos["end_balance"]
        wr = (res_oos["wins"] / res_oos["trades"] * 100.0) if res_oos["trades"] > 0 else 0.0

        rows.append(
            {
                "win": w_num,
                "atr_mult": atr_mult,
                "tr_%": round(res_tr["return_pct"], 2),
                "oos_%": round(res_oos["return_pct"], 2),
                "dd_%": round(res_oos["max_dd_pct"], 2),
                "trd": res_oos["trades"],
                "wr_%": round(wr, 1),
                "blk": res_oos["blocked"],
                "bal_$": round(bal, 2),
            }
        )

    return pd.DataFrame(rows)


def _summary_row(rep: pd.DataFrame, label: str) -> dict:
    if rep.empty:
        return {
            "mode": label,
            "end_bal_$": 1000.0,
            "net_ret_%": 0.0,
            "worst_dd_%": 0.0,
            "trades": 0,
            "avg_wr_%": 0.0,
        }
    end_bal = float(rep["bal_$"].iloc[-1])
    return {
        "mode": label,
        "end_bal_$": round(end_bal, 2),
        "net_ret_%": round(((end_bal - 1000.0) / 1000.0) * 100.0, 2),
        "worst_dd_%": round(float(rep["dd_%"].min()), 2),
        "trades": int(rep["trd"].sum()),
        "avg_wr_%": round(float(rep["wr_%"].mean()), 1),
    }


if __name__ == "__main__":
    print("[1/3] Loading dataset...", flush=True)
    data = load_candles(CSV_FILE)
    print(f"[2/3] Loaded {len(data)} rows ({data['dt'].iloc[0]} -> {data['dt'].iloc[-1]})", flush=True)

    print("[3/3] Running Walk-Forward for ATR_MULTIPLIER = 1.5 and 1.8...", flush=True)
    rep_15 = run_walk_forward(data, atr_mult=1.5)
    rep_18 = run_walk_forward(data, atr_mult=1.8)

    print("\n=== WALK-FORWARD: ATR_MULTIPLIER = 1.5 ===", flush=True)
    print(rep_15.to_string(index=False), flush=True)

    print("\n=== WALK-FORWARD: ATR_MULTIPLIER = 1.8 ===", flush=True)
    print(rep_18.to_string(index=False), flush=True)

    comp = pd.DataFrame([
        _summary_row(rep_15, "ATR_MULT = 1.5"),
        _summary_row(rep_18, "ATR_MULT = 1.8"),
    ])
    print("\n=== COMPARISON SUMMARY (1.8 vs 1.5) ===", flush=True)
    print(comp.to_string(index=False), flush=True)