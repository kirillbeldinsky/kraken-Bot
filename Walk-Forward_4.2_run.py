import os
import time
import numpy as np
import pandas as pd
import requests

CSV_FILE = "candles_30d.csv"
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15

# Параметры стратегии v7.4.2-ANTI-IMPULSE
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
MAX_DAILY_LOSS_PCT = 0.02

BB_EXPANSION_MAX = 1.35
IMPULSE_BAR_ATR_MAX = 2.2


def _normalize_time_column(df: pd.DataFrame) -> pd.DataFrame:
    """Приводит колонку времени или индекс к pd.Timestamp (UTC) в столбце 'dt'."""
    out = df.copy()
    candidates = ["timestamp", "time", "open_time", "ts", "datetime", "date"]
    found_col = next((c for c in candidates if c in out.columns), None)

    if found_col is not None:
        s = out[found_col]
        if np.issubdtype(s.dtype, np.number):
            s_num = pd.to_numeric(s, errors="coerce")
            # Если значения в миллисекундах (>= 1e11), переводим в секунды
            unit = "ms" if s_num.dropna().abs().median() >= 1e11 else "s"
            out["dt"] = pd.to_datetime(s_num, unit=unit, utc=True)
        else:
            out["dt"] = pd.to_datetime(s, utc=True, errors="coerce")
    elif isinstance(out.index, pd.DatetimeIndex):
        idx = out.index
        out["dt"] = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    else:
        raise ValueError(
            f"Не найдена колонка времени. Доступные колонки: {list(out.columns)}"
        )

    out = out.dropna(subset=["dt"]).sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
    return out


def fetch_15m_candles(limit_bars: int = 2880) -> pd.DataFrame:
    """Загружает исторические 15m свечи с публичных API (с резервными эндпоинтами)."""
    end_ms = int(time.time() * 1000)

    for base_url in ("https://api.binance.us/api/v3/klines", "https://api.binance.com/api/v3/klines"):
        rows = []
        cur_end = end_ms
        try:
            while len(rows) < limit_bars:
                batch_limit = min(1000, limit_bars - len(rows))
                resp = requests.get(
                    base_url,
                    params={
                        "symbol": "BTCUSDT",
                        "interval": "15m",
                        "limit": batch_limit,
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
                if len(batch) < batch_limit:
                    break
                time.sleep(0.1)

            if len(rows) >= 200:
                df = pd.DataFrame(
                    {
                        "open_time": [int(r[0]) // 1000 for r in rows[-limit_bars:]],
                        "open": [float(r[1]) for r in rows[-limit_bars:]],
                        "high": [float(r[2]) for r in rows[-limit_bars:]],
                        "low": [float(r[3]) for r in rows[-limit_bars:]],
                        "close": [float(r[4]) for r in rows[-limit_bars:]],
                        "volume": [float(r[5]) for r in rows[-limit_bars:]],
                    }
                )
                return _normalize_time_column(df)
        except Exception as exc:
            print(f"[INFO] {base_url} недоступен: {exc}")

    # Резервный источник: Kraken OHLC
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
    df = pd.DataFrame(
        {
            "open_time": [int(c[0]) for c in ohlc],
            "open": [float(c[1]) for c in ohlc],
            "high": [float(c[2]) for c in ohlc],
            "low": [float(c[3]) for c in ohlc],
            "close": [float(c[4]) for c in ohlc],
            "volume": [float(c[6]) for c in ohlc],
        }
    )
    return _normalize_time_column(df)


def load_dataset(csv_path: str = CSV_FILE, fallback_bars: int = 2880) -> pd.DataFrame:
    if os.path.exists(csv_path):
        raw = pd.read_csv(csv_path)
        df = _normalize_time_column(raw)
    else:
        df = fetch_15m_candles(limit_bars=fallback_bars)
        df.to_csv(csv_path, index=False)

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)].reset_index(drop=True)
    df["day"] = df["dt"].dt.strftime("%Y-%m-%d")
    return df


def compute_indicators(df: pd.DataFrame, bb_std: float) -> pd.DataFrame:
    """Считает индикаторы и фильтр анти-импульса строго по прошлым барам."""
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
    pv_sum = (tp * v).rolling(20, min_periods=20).sum()
    v_sum = v.rolling(20, min_periods=20).sum().replace(0, np.nan)
    d["vwap"] = pv_sum / v_sum

    prev_c = c.shift(1)
    tr = pd.concat(
        [
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()

    std10_avg = std20.rolling(10, min_periods=10).mean()
    bb_expanding = (std10_avg > 0) & (std20 > std10_avg * BB_EXPANSION_MAX)
    impulse_bar = (h - l) > (d["atr"] * IMPULSE_BAR_ATR_MAX)
    d["vol_expanding"] = (bb_expanding | impulse_bar).fillna(False)
    return d


def simulate_slice(
    ind_df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    atr_mult: float,
    start_balance: float = 1000.0,
) -> dict:
    """Симулирует торговлю на срезе [start_idx:end_idx) с сигналом по закрытому бару i-1."""
    balance = float(start_balance)
    peak = balance
    max_dd = 0.0

    pos = None
    last_stop_idx = -999999
    cur_day = None
    daily_trades = 0
    daily_pnl = 0.0
    daily_stops = 0

    trades = 0
    wins = 0
    blocked_impulse = 0

    lo = max(1, start_idx)
    hi = min(len(ind_df), end_idx)

    for i in range(lo, hi):
        bar = ind_df.iloc[i]
        prev = ind_df.iloc[i - 1]
        day = bar["day"]

        if day != cur_day:
            cur_day = day
            daily_trades = 0
            daily_pnl = 0.0
            daily_stops = 0

        # 1. Выход по SL / TP на текущем баре
        if pos is not None:
            exit_price = None
            is_stop = False

            if pos["side"] == "buy":
                if bar["low"] <= pos["sl"]:
                    exit_price, is_stop = pos["sl"], True
                elif bar["high"] >= pos["tp"]:
                    exit_price, is_stop = pos["tp"], False
            else:
                if bar["high"] >= pos["sl"]:
                    exit_price, is_stop = pos["sl"], True
                elif bar["low"] <= pos["tp"]:
                    exit_price, is_stop = pos["tp"], False

            if exit_price is not None:
                qty = pos["qty"]
                entry_p = pos["entry"]
                gross = (exit_price - entry_p) * qty if pos["side"] == "buy" else (entry_p - exit_p) * qty
                exit_fee = FEE_TAKER if is_stop else FEE_MAKER
                net_pnl = gross - (entry_p * qty * FEE_MAKER + exit_price * qty * exit_fee)

                balance += net_pnl
                daily_pnl += net_pnl
                trades += 1
                if net_pnl > 0:
                    wins += 1
                    daily_stops = 0
                if is_stop:
                    last_stop_idx = i
                    daily_stops += 1

                peak = max(peak, balance)
                if peak > 0:
                    max_dd = min(max_dd, (balance - peak) / peak)
                pos = None
            continue

        # 2. Предохранители и лимиты
        if daily_stops >= MAX_CONSECUTIVE_STOPS:
            continue
        if daily_pnl <= -(balance * MAX_DAILY_LOSS_PCT):
            continue
        if daily_trades >= MAX_TRADES_PER_DAY:
            continue
        if (i - last_stop_idx) < COOLDOWN_BARS:
            continue

        # 3. Проверка условий по закрытому бару prev (i - 1)
        close_p = float(prev["close"])
        atr_val = float(prev["atr"])
        lower_val = float(prev["lower"])
        upper_val = float(prev["upper"])
        vwap_val = float(prev["vwap"])
        avg_vol = float(prev["avg_vol"])
        vol_val = float(prev["volume"])
        trend = str(prev["trend"])
        vol_exp = bool(prev["vol_expanding"])

        if np.isnan(atr_val) or np.isnan(lower_val) or np.isnan(upper_val) or np.isnan(vwap_val) or atr_val <= 0:
            continue
        if vol_val < avg_vol * 0.55 or (atr_val / close_p) < MIN_ATR_PCT:
            continue

        vol_spike = vol_val > avg_vol
        bb_long = (close_p <= lower_val) and not (trend == "range" and close_p < lower_val * 0.996)
        bb_short = (close_p >= upper_val) and not (trend == "range" and close_p > upper_val * 1.004)
        vwap_long = close_p > vwap_val and vol_spike and trend == "up" and close_p < upper_val
        vwap_short = close_p < vwap_val and vol_spike and trend == "down" and close_p > lower_val

        if vol_exp and (bb_long or bb_short) and not (vwap_long or vwap_short):
            blocked_impulse += 1
            continue

        long_sig = (bb_long and not vol_exp) or vwap_long
        short_sig = (bb_short and not vol_exp) or vwap_short

        # 4. Вход на открытии текущего бара bar
        entry_p = float(bar["open"])
        sl_dist = atr_val * atr_mult
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
        "blocked": blocked_impulse,
        "end_balance": float(balance),
    }


def run_walk_forward(
    df: pd.DataFrame,
    train_bars: int = 960,
    test_bars: int = 480,
) -> pd.DataFrame:
    """Запускает скользящий Walk-Forward без утечки индикаторов из train в test."""
    if len(df) < train_bars + test_bars:
        # Авто-подбор размеров окон для коротких выгрузок
        test_bars = max(60, len(df) // 4)
        train_bars = max(80, test_bars * 2)

    bb_grid = [2.0, 2.2]
    atr_grid = [1.2, 1.5, 1.8]
    warmup = 60

    rows = []
    running_balance = 1000.0
    win_num = 0

    for test_start in range(train_bars, len(df) - test_bars + 1, test_bars):
        win_num += 1
        train_start = max(0, test_start - train_bars)
        test_end = test_start + test_bars

        best_cfg = (2.2, 1.5)
        best_train_ret = -np.inf

        train_raw = df.iloc[train_start:test_start].reset_index(drop=True)
        for b_std in bb_grid:
            ind_tr = compute_indicators(train_raw, b_std)
            for a_mult in atr_grid:
                res_tr = simulate_slice(ind_tr, warmup, len(ind_tr), atr_mult=a_mult, start_balance=1000.0)
                if res_tr["return_pct"] > best_train_ret:
                    best_train_ret = res_tr["return_pct"]
                    best_cfg = (b_std, a_mult)

        b_std, a_mult = best_cfg
        oos_raw_start = max(0, test_start - warmup)
        ind_oos = compute_indicators(df.iloc[oos_raw_start:test_end].reset_index(drop=True), b_std)
        oos_start_idx = test_start - oos_raw_start

        res_oos = simulate_slice(
            ind_oos,
            start_idx=oos_start_idx,
            end_idx=len(ind_oos),
            atr_mult=a_mult,
            start_balance=running_balance,
        )
        running_balance = res_oos["end_balance"]
        wr_pct = (res_oos["wins"] / res_oos["trades"] * 100.0) if res_oos["trades"] > 0 else 0.0

        rows.append(
            {
                "window": win_num,
                "bb_std": b_std,
                "atr_mult": a_mult,
                "train_ret_%": round(best_train_ret, 2),
                "oos_ret_%": round(res_oos["return_pct"], 2),
                "oos_dd_%": round(res_oos["max_dd_pct"], 2),
                "oos_trades": res_oos["trades"],
                "oos_wr_%": round(wr_pct, 1),
                "blocked": res_oos["blocked"],
                "balance_$": round(running_balance, 2),
            }
        )

    return pd.DataFrame(rows)


if __name__ == "__main__":
    data = load_dataset(CSV_FILE)
    report = run_walk_forward(data)
    print(f"Candles: {len(data)} ({data['dt'].iloc[0]} -> {data['dt'].iloc[-1]})")
    print("\n=== WALK-FORWARD REPORT (v7.4.2-ANTI-IMPULSE) ===")
    if report.empty:
        print("Недостаточно данных для формирования окон Walk-Forward.")
    else:
        print(report.to_string(index=False))