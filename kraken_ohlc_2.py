
import csv
import os
import pandas as pd
import requests


class KrakenOHLC:
    BASE_URL = "https://api.kraken.com/0/public/OHLC"

    def __init__(self, pair="XBTUSD"):
        self.pair = pair

    def get_ohlc(self, interval=15):
        params = {"pair": self.pair, "interval": interval}

        response = requests.get(self.BASE_URL, params=params)
        data = response.json()

        if "error" in data and data["error"]:
            raise Exception(f"Kraken API error: {data['error']}")

        # Получаем данные массива
        result = data["result"]
        pair_key = [k for k in result.keys() if k != "last"][0]
        raw = result[pair_key]

        module_dir = os.path.dirname(os.path.abspath(__file__))
        save_path = os.path.join(module_dir, "data.csv")

        # Дописываем сырые строки из ответа API в конец CSV
        with open(save_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # Записываем последнюю свечу из списка raw (одна строка)
            writer.writerow(raw[-1])

            # ИЛИ если нужно записать ВСЕ свечи:
            # writer.writerows(raw)

        return data
