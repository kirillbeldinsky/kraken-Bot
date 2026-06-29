import os, time, json, logging, requests, hmac, hashlib, base64, math
from datetime import datetime, timedelta
from dotenv import load_dotenv
from urllib.parse import urlencode

load_dotenv()
API_KEY = os.getenv("KRAKEN_API_KEY")
API_SECRET = os.getenv("KRAKEN_API_SECRET")
BASE_URL = "https://api.kraken.com"

PAIR = "XBTUSD"
INTERVAL = 15
EMA_PERIOD = 50
SMA_PERIOD = 200
PULLBACK_PCT = 0.02
MIN_BAND_WIDTH_PCT = 0.015
MAX_TP_PCT = 0.015
MIN_TP_PCT = 0.007
VOLUME_PCT = 0.01
DAILY_LOSS_LIMIT_R = 2.0
PARTIAL_CLOSE_PCT = 0.5

logging.basicConfig(
    filename="trading_bot.log",
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def get_kraken_signature(urlpath, data, secret):
    postdata = urlencode(data)
    encoded = (str(data['nonce']) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    sigdigest = base64.b64encode(mac.digest())
    return sigdigest.decode()

def kraken_request(uri_path, data):
    headers = {'API-Key': API_KEY, 'API-Sign': get_kraken_signature(uri_path, data, API_SECRET)}
    resp = requests.post(BASE_URL + uri_path, headers=headers, data=data)
    return resp.json()

def load_state():
    try:
        with open("bot_state.json", "r") as f:
            return json.load(f)
    except:
        return {
            "total_longs": 0, "total_shorts": 0, "daily_loss_r": 0.0,
            "last_day": "", "paper_balance": 1000.0, "open_position": None,
            "last_exit_price": 0, "current_regime": "range", "dynamic_tp_pct": MAX_TP_PCT
        }

def save_state(state):
    with open("bot_state.json", "w") as f:
        json.dump(state, f, indent=2)

def get_ohlc():
    resp = requests.get(f"{BASE_URL}/0/public/OHLC?pair={PAIR}&interval={INTERVAL}")
    data = resp.json()["result"]
    ohlc = data["XXBTZUSD"] if "XXBTZUSD" in data else data["XBTUSD"]
    return [float(x[4]) for x in ohlc]

def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def calculate_sma(prices, period):
    return sum(prices[-period:]) / period

def calculate_bands(prices, period=20):
    sma = calculate_sma(prices, period)
    std = (sum([(p - sma) ** 2 for p in prices[-period:]]) / period) ** 0.5
    return sma, sma + 2 * std, sma - 2 * std

def detect_regime(prices):
    sma200 = calculate_sma(prices, SMA_PERIOD)
    current = prices[-1]
    if current > sma200 * 1.05: return "trend_up"
    if current < sma200 * 0.95: return "trend_down"
    return "range"

def signal_range(prices):
    sma, upper, lower = calculate_bands(prices)
    current = prices[-1]
    band_width_pct = (upper - lower) / sma
    if band_width_pct < MIN_BAND_WIDTH_PCT:
        logging.info(f"FILTER:LOW_VOL Width={band_width_pct*100:.2f}%")
        return "HOLD"
    if current < lower: return "BUY"
    if current > upper: return "SELL"
    return "HOLD"

def signal_trend(prices):
    ema = calculate_ema(prices, EMA_PERIOD)
    current, prev, older = prices[-1], prices[-2], prices[-3]
    if older <= ema and prev > ema and prev < ema * (1 + PULLBACK_PCT): return "BUY"
    if older >= ema and prev < ema and prev > ema * (1 - PULLBACK_PCT): return "SELL"
    return "HOLD"

def manage_risks(signal, current_price):
    balance = stats["paper_balance"]
    risk_amount = balance * 0.01
    vol = round(risk_amount / (current_price * 0.015), 8)
    sl = current_price * 0.985 if signal == "BUY" else current_price * 1.015
    tp = current_price * (1 + stats["dynamic_tp_pct"]) if signal == "BUY" else current_price * (1 - stats["dynamic_tp_pct"])
    return {
        "side": "buy" if signal == "BUY" else "sell",
        "volume": vol,
        "stop_loss": round(sl, 2),
        "entry": current_price,
        "take_profit": round(tp, 2)
    }

def execute_order(order):
    side, vol, sl, entry, tp = order["side"], order["volume"], order["stop_loss"], order["entry"], order["take_profit"]
    logging.info(f" {side.upper()} {vol} @ {entry} | SL={sl} | TP={tp}")
    stats["open_position"] = {
        "side": side, "entry": entry, "sl": sl, "volume": vol, "tp": tp
    }
    if side == "buy": stats["total_longs"] += 1
    else: stats["total_shorts"] += 1

def check_exit(current_price):
    pos = stats["open_position"]
    if not pos: return False
    entry, sl, vol, side, tp = pos["entry"], pos["sl"], pos["volume"], pos["side"], pos["tp"]

    sl_hit = current_price <= sl if side == "buy" else current_price >= sl
    tp_hit = current_price >= tp if side == "buy" else current_price <= tp

    if sl_hit or tp_hit:
        exit_price = sl if sl_hit else tp
        pnl = (exit_price - entry) * vol if side == "buy" else (entry - exit_price) * vol
        stats["paper_balance"] += pnl
        stats["daily_loss_r"] += abs(pnl) / (stats["paper_balance"] * 0.01)
        stats["last_exit_price"] = exit_price
        stats["open_position"] = None
        reason = "STOP" if sl_hit else "TP"
        logging.info(f"[CLOSE] {reason} {side.upper()} PnL=${pnl:.2f}")
        save_state(stats)
        return True

    # Трейлинг
    new_sl = entry * 1.005 if side == "buy" else entry * 0.995
    if (side == "buy" and current_price > entry * 1.005 and new_sl > sl) or        (side == "sell" and current_price < entry * 0.995 and new_sl < sl):
        pos["sl"] = round(new_sl, 2)
        logging.info(f"[TRAIL] {side.upper()} Новый стоп={pos['sl']}")
        save_state(stats)
    return False

stats = load_state()
logging.info(f"=== Старт бота v6: TP фиксируется при входе ===")

while True:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        if stats["last_day"]!= today:
            stats["daily_loss_r"] = 0.0
            stats["last_day"] = today
            save_state(stats)

        if stats["daily_loss_r"] >= DAILY_LOSS_LIMIT_R:
            logging.info(f"DAILY_LIMIT: {stats['daily_loss_r']:.2f}R >= {DAILY_LOSS_LIMIT_R}R. Стоп на сегодня.")
            time.sleep(3600)
            continue

        prices = get_ohlc()
        if len(prices) < SMA_PERIOD:
            time.sleep(60)
            continue

        regime = detect_regime(prices)
        stats["current_regime"] = regime
        sma, upper, lower = calculate_bands(prices)
        band_width_pct = (upper - lower) / sma

        # Динамический TP считаем каждый тик, но используем только при входе
        stats["dynamic_tp_pct"] = max(MIN_TP_PCT, min(MAX_TP_PCT, band_width_pct * 0.75))

        current = prices[-1]
        pos = stats["open_position"]

        if pos:
            check_exit(current)
            pnl = (current - pos["entry"]) * pos["volume"] if pos["side"] == "buy" else (pos["entry"] - current) * pos["volume"]
            logging.info(f"POS={pos['side'].upper()}@{pos['entry']} PnL=${pnl:.2f} SL={pos['sl']} TP={pos['tp']}")
        else:
            if regime == "range":
                signal = signal_range(prices)
            else:
                signal = signal_trend(prices)

            if signal!= "HOLD" and abs(current - stats["last_exit_price"]) > stats["last_exit_price"] * 0.002:
                order = manage_risks(signal, current)
                execute_order(order)
                save_state(stats)
            else:
                logging.info(f"Режим={regime} | SMA20={sma:.1f} | Low={lower:.1f} | High={upper:.1f} | Width={band_width_pct*100:.2f}% | Signal={signal} | Close={current}")

        time.sleep(INTERVAL * 60)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        time.sleep(60)
