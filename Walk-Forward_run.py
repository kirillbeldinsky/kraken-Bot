import json
import os
import requests
import numpy as np
import pandas as pd

# --- НАСТРОЙКИ ---
KRAKEN_SYMBOL = "XXBTZUSD"
TIMEFRAME = 15          # 15 минут
FEE_PCT = 0.0026        # комиссия Kraken 0.26%
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

    df = df.dropna(subset=["close", "high", "low", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)].reset_index(drop=True)

    state = load_bot_state(state_path)
    df.attrs["bot_state"] = state
    return df


def run_bb_backtest_window(
    df: pd.DataFrame,
    bb_window: int = 20,
    num_std: float = 2.0,
    fee_pct: float = FEE_PCT,
) -> dict:
    """Бэктест по полосам Боллинджера + объёму на одном окне без lookahead bias."""
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)

    ma = close.rolling(window=bb_window, min_periods=bb_window).mean()
    std = close.rolling(window=bb_window, min_periods=bb_window).std().replace(0, np.nan)
    upper = ma + num_std * std
    lower = ma - num_std * std
    avg_vol = vol.rolling(window=bb_window, min_periods=bb_window).mean()

    vol_ok = vol >= (avg_vol * 0.55)
    raw_signal = np.where(
        (close <= lower) & vol_ok,
        1.0,
        np.where((close >= upper) & vol_ok, -1.0, 0.0),
    )

    # Сдвиг на 1 шаг вперёд, чтобы исключить подглядывание в закрытие текущего бара
    pos = pd.Series(raw_signal, index=df.index).shift(1).fillna(0.0)
    bar_ret = close.pct_change().fillna(0.0)
    trades_changed = pos.diff().abs().fillna(pos.abs())

    net_ret = pos * bar_ret - trades_changed * fee_pct
    equity = (1.0 + net_ret).cumprod()
    peak = equity.cummax()
    dd = (equity - peak) / peak

    return {
        "return_pct": float((equity.iloc[-1] - 1.0) * 100.0) if len(equity) else 0.0,
        "max_dd_pct": float(dd.min() * 100.0) if len(dd) else 0.0,
        "signals": int((trades_changed > 0).sum()),
    }


def walk_forward_bb(
    df: pd.DataFrame,
    train_bars: int = 120,
    test_bars: int = 40,
    fee_pct: float = FEE_PCT,
) -> pd.DataFrame:
    """Прогон по скользящим окнам Walk-Forward."""
    grid_std = [1.8, 2.0, 2.2]
    rows = []

    for start in range(0, len(df) - train_bars - test_bars + 1, test_bars):
        train_df = df.iloc[start : start + train_bars]
        test_df = df.iloc[start + train_bars : start + train_bars + test_bars]

        best_std = 2.0
        best_train_ret = -np.inf
        for k in grid_std:
            res_tr = run_bb_backtest_window(train_df, num_std=k, fee_pct=fee_pct)
            if res_tr["return_pct"] > best_train_ret:
                best_train_ret = res_tr["return_pct"]
                best_std = k

        res_oos = run_bb_backtest_window(test_df, num_std=best_std, fee_pct=fee_pct)
        rows.append({
            "window": len(rows) + 1,
            "chosen_std": best_std,
            "train_ret_%": round(best_train_ret, 2),
            "oos_ret_%": round(res_oos["return_pct"], 2),
            "oos_max_dd_%": round(res_oos["max_dd_pct"], 2),
            "oos_signals": res_oos["signals"],
        })

    return pd.DataFrame(rows)


if __name__ == "__main__":
    raw_ohlc = get_ohlc(KRAKEN_SYMBOL, TIMEFRAME, limit=720)
    df_candles = build_candles_dataframe(raw_ohlc, state_path=STATE_FILE)

    print(f"Загружено свечей: {len(df_candles)}")
    print("Состояние из JSON:", df_candles.attrs.get("bot_state"))

    wf_report = walk_forward_bb(df_candles, train_bars=120, test_bars=40, fee_pct=FEE_PCT)
    print("\n--- WALK-FORWARD REPORT ---")
    print(wf_report.to_string(index=False))