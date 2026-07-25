import os
import pandas as pd
import requests


class KrakenOHLC:
    BASE_URL = "https://api.kraken.com/0/public/OHLC"

    def __init__(self, pair="XBTUSD"):
        self.pair = pair

    def get_ohlc(self, interval=15):
        """Fetch OHLC data from Kraken REST API and save to CSV."""
        params = {"pair": self.pair, "interval": interval}

        response = requests.get(self.BASE_URL, params=params)
        data = response.json()

        if "error" in data and data["error"]:
            raise Exception(f"Kraken API error: {data['error']}")

        # 1. Исправление: Kraken возвращает уникальный ключ пары (например, XXBTZUSD)
        result = data["result"]
        pair_key = [k for k in result.keys() if k != "last"][0]
        raw = result[pair_key]

        # 2. Преобразование в DataFrame
        df = pd.DataFrame(
            raw,
            columns=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "vwap",
                "volume",
                "count",
            ],
        )

        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time")
        df = df.sort_index()

        numeric_cols = ["open", "high", "low", "close", "vwap", "volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)

        # 3. Сохранение DataFrame в CSV рядом с файлом скрипта
        module_dir = os.path.dirname(os.path.abspath(__file__))
        save_path = os.path.join(module_dir, "kraken_ohlc_data.csv")

        # index=True сохранит колонку 'time' (так как она сделана индексом)
        df.to_csv(save_path, index=True)

        return df
