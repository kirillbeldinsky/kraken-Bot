from datetime import datetime
import pandas as pd
import requests


class KrakenOHLC:
    BASE_URL = "https://api.kraken.com/0/public/OHLC"

    def __init__(self, pair="XBTUSD"):
        self.pair = pair

    def get_ohlc(self, interval=15):
        """Получает данные с Kraken API и выводит их в консоль."""
        params = {"pair": self.pair, "interval": interval}

        response = requests.get(self.BASE_URL, params=params)
        data = response.json()

        if "error" in data and data["error"]:
            raise Exception(f"Kraken API error: {data['error']}")

        # Kraken возвращает динамеческий ключ пары (например, XXBTZUSD)
        result = data["result"]
        pair_key = [k for k in result.keys() if k != "last"][0]
        raw = result[pair_key]

        # Преобразуем в DataFrame
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
        df = df.set_index("time").sort_index()

        numeric_cols = ["open", "high", "low", "close", "vwap", "volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)

        # Вывод текущего системного времени и последней полученной свечи в консоль
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Данные успешно получены для {self.pair}:")
        print("-" * 50)
        print(df.tail(2))  # Выводит самую последнюю свечу
        print("-" * 50)

        return df


# --- Проверка работы ---
if __name__ == "__main__":
    bot = KrakenOHLC(pair="XBTUSD")
    df = bot.get_ohlc(interval=15)
