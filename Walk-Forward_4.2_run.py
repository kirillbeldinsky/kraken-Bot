import numpy as np
import pandas as pd

CSV_PATH = "candles_30d.csv"

# Параметры v7.4.2-ANTI-IMPULSE
BB_PERIOD = 20
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
CROSS_DAY_CB_BARS = 48        # 12 часов на 15m
MAX_DAILY_LOSS_PCT = 0.02

BB_EXPANSION_MAX = 1.35
IMPULSE_BAR_ATR_MAX = 2.2


def load_candles(path: str = CSV_PATH) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["dt"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["dt", "open", "high", "low", "close", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)]
    df = df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
    df["day"] = df["dt"].dt.strftime("%Y-%m-%d")
    return df


def add_indicators(df: pd.DataFrame, bb_std: float) -> pd.DataFrame:
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


def walk_forward(df: pd.DataFrame, train_bars: int = 960, test_bars: int = 480) -> pd.DataFrame:
    bb_grid = [2.0, 2.2]
    atr_grid = [1.2, 1.5, 1.8]
    warmup = 60
    rows = []
    bal = 1000.0
    w_idx = 0

    for test_start in range(train_bars, len(df) - test_bars + 1, test_bars):
        w_idx += 1
        train_start = max(0, test_start - train_bars)
        test_end = test_start + test_bars

        best_cfg = (2.2, 1.5)
        best_tr_ret = -np.inf

        tr_raw = df.iloc[train_start:test_start].reset_index(drop=True)
        for b_std in bb_grid:
            ind_tr = add_indicators(tr_raw, b_std)
            for a_mult in atr_grid:
                res_tr = sim_slice(ind_tr, warmup, len(ind_tr), atr_mult=a_mult, start_bal=1000.0)
                if res_tr["ret_pct"] > best_tr_ret:
                    best_tr_ret = res_tr["ret_pct"]
                    best_cfg = (b_std, a_mult)

        b_std, a_mult = best_cfg
        wu_start = max(0, test_start - warmup)
        ind_oos = add_indicators(df.iloc[wu_start:test_end].reset_index(drop=True), b_std)
        oos_start = test_start - wu_start

        res_oos = sim_slice(ind_oos, oos_start, len(ind_oos), atr_mult=a_mult, start_bal=bal)
        bal = res_oos["end_bal"]
        wr = (res_oos["wins"] / res_oos["trades"] * 100.0) if res_oos["trades"] > 0 else 0.0

        rows.append({
            "win": w_idx,
            "bb_std": b_std,
            "atr_mult": a_mult,
            "tr_%": round(best_tr_ret, 2),
            "oos_%": round(res_oos["ret_pct"], 2),
            "dd_%": round(res_oos["dd_pct"], 2),
            "trd": res_oos["trades"],
            "wr_%": round(wr, 1),
            "blk": res_oos["blocked"],
            "bal_$": round(bal, 2),
        })
        print(f"Window {w_idx} done -> oos={rows[-1]['oos_%']:+.2f}%, bal=${rows[-1]['bal_$']:.2f}", flush=True)

    return pd.DataFrame(rows)


if __name__ == "__main__":
    print("Loading candles_30d.csv...", flush=True)
    data = load_candles(CSV_PATH)
    print(f"Loaded {len(data)} rows: {data['dt'].iloc[0]} -> {data['dt'].iloc[-1]}", flush=True)
    rep = walk_forward(data)
    print("\n=== WALK-FORWARD v7.4.2-ANTI-IMPULSE ===", flush=True)
    print(rep.to_string(index=False), flush=True)
