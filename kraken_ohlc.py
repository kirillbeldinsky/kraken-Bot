import requests
import pandas as pd
import csv

class KrakenOHLC:
    BASE_URL = "https://api.kraken.com/0/public/OHLC"

    def __init__(self, pair="XBTUSD"):
        self.pair = pair

    def get_ohlc(self, interval=15):
        """
        Fetch OHLC data from Kraken REST API and return a clean pandas DataFrame.
        """

        params = {
            "pair": self.pair,
            "interval": interval
        }

        response = requests.get(self.BASE_URL, params=params)
        data = response.json()

        if "error" in data and data["error"]:
            raise Exception(f"Kraken API error: {data['error']}")

        raw = data["result"][self.pair]

        # Convert to DataFrame
        df = pd.DataFrame(raw, columns=[
            "time", "open", "high", "low", "close",
            "vwap", "volume", "count"
        ])

        # Convert timestamps
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time")

        # Sort timestamps (fixes -15T issue)
        df = df.sort_index()

        # Convert numeric columns
        numeric_cols = ["open", "high", "low", "close", "vwap", "volume"]
        df[numeric_cols] = df[numeric_cols].astype(float)

        # Assign correct frequency
        df = df.asfreq(f"{interval}min")
        
        #  Сохранение CSV в той же папке, где лежит модуль 
        df.to_csv('/home/agent/kraken-Bot/kraken_ohlc_data.csv')
       # module_dir = os.path.dirname(os.path.abspath(__file__))
       # save_path = os.path.join(module_dir, "kraken_ohlc_data.csv")

        df.to_csv(save_path)

        return df

