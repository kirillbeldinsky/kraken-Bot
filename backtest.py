import time
import json
import requests

PAIR = "XBTUSDT"   # у Kraken BTC = XBT
INTERVAL = 15      # 15-минутные свечи
DAYS = 180         # полгода
DATA_FILE = "kraken_ohlc_180d.json"


def fetch_ohlc():
    """Скачать OHLC с Kraken за последние DAYS дней и сохранить в файл."""
    since = int(time.time()) - DAYS * 24 * 60 * 60

    url = (
        "https://api.kraken.com/0/public/OHLC"
        f"?pair={PAIR}&interval={INTERVAL}&since={since}"
    )

    print(f"[INFO] Requesting OHLC from Kraken: {PAIR}, {INTERVAL}m, {DAYS} days...")
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data and data["error"]:
        raise RuntimeError(f"Kraken API error: {data['error']}")

    ohlc = data["result"][PAIR]

    print(f"[INFO] Received {len(ohlc)} candles")
    with open(DATA_FILE, "w") as f:
        json.dump(ohlc, f, indent=2)

    print(f"[INFO] Saved candles to {DATA_FILE}")


def load_ohlc():
    """Загрузить OHLC из файла."""
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def trend_from_ema(ema20, ema50):
    """Твой тренд-фильтр."""
    if ema20 < ema50 * 0.9985:
        return "down"
    if ema20 > ema50 * 1.0015:
        return "up"
    return "range"


def run_backtest():
    """Прогнать тренд-фильтр по истории за полгода."""
    ohlc = load_ohlc()
    closes = []

    print(f"[INFO] Starting backtest on {len(ohlc)} candles...")

    for candle in ohlc:
        ts = int(candle[0])
        close = float(candle[4])

        closes.append(close)

        # нужно минимум 50 закрытий для EMA50
        if len(closes) < 50:
            continue

        ema20 = sum(closes[-20:]) / 20
        ema50 = sum(closes[-50:]) / 50

        trend = trend_from_ema(ema20, ema50)

        # простой лог — можно заменить на logging
        print(
            f"ts={ts}  close={close:.2f}  "
            f"ema20={ema20:.2f}  ema50={ema50:.2f}  trend={trend}"
        )

    print("[INFO] Backtest finished.")


if __name__ == "__main__":
    # 1) один раз качаешь историю:
    #    fetch_ohlc()
    #
    # 2) потом сколько угодно раз гоняешь бэктест:
    #    run_backtest()

    # раскомментируй по очереди:

    # fetch_ohlc()
    run_backtest()
