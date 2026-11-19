import json
import os
import numpy as np
import pandas as pd

STATE_FILE = "bot_state.json"


def load_bot_state(state_path: str = STATE_FILE) -> dict:
    """Загружает текущее состояние из JSON-файла бота (если файл существует)."""
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[WARN] Не удалось прочитать {state_path}: {e}")
    return {}


def build_candles_dataframe(
    ohlc_data: dict,
    state_path: str = STATE_FILE,
    save_csv_path: str | None = None,
) -> pd.DataFrame:
    """
    Превращает словарь OHLC (из get_ohlc()) в очищенный DataFrame
    с колонками open/high/low/close/volume и метаданными из bot_state.json.
    """
    if not ohlc_data:
        raise ValueError("ohlc_data пуст — нечего собирать в DataFrame.")

    closes = ohlc_data.get("closes", [])
    highs = ohlc_data.get("highs", closes)
    lows = ohlc_data.get("lows", closes)
    volumes = ohlc_data.get("volumes", [0.0] * len(closes))

    n = min(len(closes), len(highs), len(lows), len(volumes))
    if n == 0:
        raise ValueError("В ohlc_data нет свечей для формирования DataFrame.")

    df = pd.DataFrame({
        "open": pd.Series(closes[:n], dtype="float64").shift(1).fillna(pd.Series(closes[:n], dtype="float64")),
        "high": pd.Series(highs[:n], dtype="float64"),
        "low": pd.Series(lows[:n], dtype="float64"),
        "close": pd.Series(closes[:n], dtype="float64"),
        "volume": pd.Series(volumes[:n], dtype="float64"),
    })

    # Базовая очистка данных (Data Sanitation): убираем NaN и заведомо битые нулевые цены
    df = df.dropna(subset=["close", "high", "low", "volume"]).copy()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"]) & (df["volume"] >= 0)].reset_index(drop=True)

    # Прикрепляем контекст из bot_state.json (если файл уже создан на сервере)
    state = load_bot_state(state_path)
    df.attrs["bot_state"] = {
        "paper_balance": float(state.get("paper_balance", 1000.0)),
        "total_trades": int(state.get("total_trades", 0)),
        "total_wins": int(state.get("total_wins", 0)),
        "total_pnl": float(state.get("total_pnl", 0.0)),
        "last_day": state.get("last_day"),
    }

    if save_csv_path:
        df.to_csv(save_csv_path, index=False)

    return df