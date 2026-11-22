import os
import time
import numpy as np
import pandas as pd
import requests

CSV_FILE = "candles_30d.csv"

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


def load_dataset(csv_path: str = CSV_FILE) -> pd.DataFrame:
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        end_ms = int(time.time() * 1000)
        rows = []
        cur_end = end_ms
        for _ in range(3):
            resp = requests.get(
                "https://api.binance.us/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1000, "endTime": cur_end},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            rows = batch + rows
            cur_end = int(batch[0][0]) - 1
        rows = rows[-2880:]
        df = pd.DataFrame(
            {
                "time": [pd.to_datetime(int(r[0]), unit="ms", utc=True) for r in rows],
                "open": [float(r[1]) for r in rows],
                "high": [float(r[2]) for r in rows],
                "low": [float(r[3]) for r in rows],
                "close": [float(r[4]) for r in rows],
                "volume": [float(r[5]) for r in rows],
            }
        )

    time_col = next((c for c in ["time", "timestamp", "open_time", "datetime", "date"] if c in df.columns), None)
    if time_col is None:
        raise ValueError(f"Не найдена колонка времени. Колонки: {list(df.columns)}")

    s = df[time_col]
    if np.issubdtype(s.dtype, np.number):
        s_num = pd.to_numeric(s, errors="coerce")
        unit = "ms" if s_num.dropna().abs().median() >= 1e11 else "s"
        df["dt"] = pd.to_datetime(s_num, unit=unit, utc=True)
    else:
        df["dt"] = pd.to_datetime(s, utc=True, errors="coerce")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["dt", "open", "high", "low", "close", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)]
    df = df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
    df["day"] = df["dt"].dt.strftime("%Y-%m-%d")
    return df


def add_indicators(df: pd.DataFrame, bb_std: float = BB_STD) -> pd.DataFrame:
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


def sim_slice(
    d: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    atr_mult: float,
    start_bal: float = 1000.0,
) -> dict:
    bal = float(start_bal)
    peak = bal
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

                bal += net
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

                peak = max(peak, bal)
                if peak > 0:
                    max_dd = min(max_dd, (bal - peak) / peak)
                pos = None
            continue

        if consecutive_stops >= MAX_CROSS_DAY_STOPS and (i - last_stop_idx) < CROSS_DAY_CB_BARS:
            continue
        if daily_stops >= MAX_CONSECUTIVE_STOPS:
            continue
        if daily_pnl <= -(bal * MAX_DAILY_LOSS_PCT):
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

        qty = min((bal * RISK_PCT) / sl_dist, (bal * MAX_LEVERAGE) / entry_p)
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

    ret_pct = ((bal - start_bal) / start_bal) * 100.0
    return {
        "ret_pct": float(ret_pct),
        "dd_pct": float(max_dd * 100.0),
        "trades": trades,
        "wins": wins,
        "blocked": blocked,
        "end_bal": float(bal),
    }


def run_wf_fixed_atr(
    df: pd.DataFrame,
    atr_mult: float,
    train_bars: int = 960,
    test_bars: int = 480,
) -> pd.DataFrame:
    warmup = 60
    bal = 1000.0
    rows = []
    w_num = 0

    for test_start in range(train_bars, len(df) - test_bars + 1, test_bars):
        w_num += 1
        train_start = max(0, test_start - train_bars)
        test_end = test_start + test_bars

        tr_df = add_indicators(df.iloc[train_start:test_start].reset_index(drop=True), BB_STD)
        res_tr = sim_slice(tr_df, warmup, len(tr_df), atr_mult=atr_mult, start_bal=1000.0)

        wu_start = max(0, test_start - warmup)
        oos_df = add_indicators(df.iloc[wu_start:test_end].reset_index(drop=True), BB_STD)
        oos_start = test_start - wu_start
        res_oos = sim_slice(oos_df, oos_start, len(oos_df), atr_mult=atr_mult, start_bal=bal)

        bal = res_oos["end_bal"]
        wr = (res_oos["wins"] / res_oos["trades"] * 100.0) if res_oos["trades"] > 0 else 0.0

        rows.append(
            {
                "win": w_num,
                "atr_mult": atr_mult,
                "tr_%": round(res_tr["ret_pct"], 2),
                "oos_%": round(res_oos["ret_pct"], 2),
                "dd_%": round(res_oos["dd_pct"], 2),
                "trd": res_oos["trades"],
                "wr_%": round(wr, 1),
                "blk": res_oos["blocked"],
                "bal_$": round(bal, 2),
            }
        )

    return pd.DataFrame(rows)


def summarize(rep: pd.DataFrame, label: str) -> dict:
    tot_trades = int(rep["trd"].sum()) if not rep.empty else 0
    end_bal = float(rep["bal_$"].iloc[-1]) if not rep.empty else 1000.0
    worst_dd = float(rep["dd_%"].min()) if not rep.empty else 0.0
    avg_wr = float(rep["wr_%"].mean()) if not rep.empty else 0.0
    return {
        "mode": label,
        "end_bal_$": round(end_bal, 2),
        "net_ret_%": round(((end_bal - 1000.0) / 1000.0) * 100.0, 2),
        "worst_dd_%": round(worst_dd, 2),
        "trades": tot_trades,
        "avg_wr_%": round(avg_wr, 1),
    }


if __name__ == "__main__":
    print("[1/3] Loading dataset...", flush=True)
    data = load_dataset(CSV_FILE)
    print(f"[2/3] Loaded {len(data)} candles ({data['dt'].iloc[0]} -> {data['dt'].iloc[-1]})", flush=True)

    print("[3/3] Running Walk-Forward for ATR_MULTIPLIER = 1.5 and 1.8...", flush=True)
    rep_15 = run_wf_fixed_atr(data, atr_mult=1.5)
    rep_18 = run_wf_fixed_atr(data, atr_mult=1.8)

    print("\n=== WALK-FORWARD: ATR_MULTIPLIER = 1.5 ===", flush=True)
    print(rep_15.to_string(index=False), flush=True)

    print("\n=== WALK-FORWARD: ATR_MULTIPLIER = 1.8 ===", flush=True)
    print(rep_18.to_string(index=False), flush=True)

    comp = pd.DataFrame([
        summarize(rep_15, "ATR_MULT = 1.5"),
        summarize(rep_18, "ATR_MULT = 1.8"),
    ])
    print("\n=== COMPARISON SUMMARY (1.8 vs 1.5) ===", flush=True)
    print(comp.to_string(index=False), flush=True)
