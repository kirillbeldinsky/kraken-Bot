import requests

def debug_kraken(KRAKEN_SYMBOL, TIMEFRAME):
    url = f"https://api.kraken.com/0/public/OHLC?pair={KRAKEN_SYMBOL}&interval={TIMEFRAME}"
    r = requests.get(url, timeout=10)
    data = r.json()

    print("RAW:", data)
    print("RESULT KEYS:", list(data.get("result", {}).keys()))

debug_kraken("BTCUSDT", 5)
