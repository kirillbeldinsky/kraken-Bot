#!/usr/bin/env python3
# kraken_bot_v6.4.1.py
# Version: 6.4.1
# Changes:
# 1. Защита от проскальзывания: буфер 0.1% на вход, SL расширен до 1.6%
# 2. Фильтр спреда <0.1% через Ticker
# 3. Реальный режим торговли через kraken_request (TRADE_MODE=live)
# 4. Вход по mid-price, не по close свечи
# 5. TP фиксируется при входе
# 6. PAPER симуляция: случайное проскальзывание 0.05-0.15%
# 7. Комиссия Kraken 0.16% учитывается в PnL
# 8. Фильтр объёма (последние 3 свечи >=80% от среднего 20)
# 9. Cooldown 4 бара после убыточной сделки
# 10. Логирование пропусков раз в INTERVAL, не каждую минуту

import os, time, json, logging, requests, hmac, hashlib, base64, math, random
from datetime import datetime, timedelta
from dotenv import load_dotenv
from urllib.parse import urlencode

VERSION = "6.4.1-log-throttle"
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
DAILY_LOSS_LIMIT_R = 2.0
STATS_INTERVAL_TICKS = 4

TRADE_MODE = os.getenv("TRADE_MODE", "paper")
SLIPPAGE_PCT = 0.001
SL_PCT_BASE = 0.015
SL_PCT = SL_PCT_BASE + SLIPPAGE_PCT
SPREAD_MAX_PCT = 0.001

FEE_PCT = 0.0016
VOLUME_THRESHOLD = 0.8
COOLDOWN_BARS = 4

logging.basicConfig(filename="trading_bot.log", level=logging.INFO, format="%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

def get_kraken_signature(urlpath, data, secret):
	postdata = urlencode(data)
	encoded = (str(data['nonce']) + postdata).encode()
	message = urlpath.encode() + hashlib.sha256(encoded).digest()
	mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
	return base64.b64encode(mac.digest()).decode()

def kraken_request(uri_path, data):
	headers = {'API-Key': API_KEY, 'API-Sign': get_kraken_signature(uri_path, data, API_SECRET)}
	return requests.post(BASE_URL + uri_path, headers=headers, data=data).json()

def load_state():
	try:
		with open("bot_state.json", "r") as f:
			return json.load(f)
	except:
		return {"total_longs":0,"total_shorts":0,"daily_loss_r":0.0,"last_day":"","paper_balance":1000.0,"open_position":None,"last_exit_price":0,"current_regime":"range","dynamic_tp_pct":MAX_TP_PCT,"tick_counter":0,"total_trades":0,"total_wins":0,"total_pnl":0.0,"last_loss_time":None}

def save_state(state):
	with open("bot_state.json","w") as f:
		json.dump(state,f,indent=2)

def get_ohlc():
	resp = requests.get(f"{BASE_URL}/0/public/OHLC?pair={PAIR}&interval={INTERVAL}")
	ohlc = resp.json()["result"].get("XXBTZUSD") or resp.json()["result"].get("XBTUSD")
	prices = [float(x[4]) for x in ohlc]
	volumes = [float(x[6]) for x in ohlc]
	return prices, volumes

def get_ticker():
	t = requests.get(f"{BASE_URL}/0/public/Ticker?pair={PAIR}").json()["result"]
	d = t.get("XXBTZUSD") or t.get("XBTUSD")
	bid, ask = float(d["b"][0]), float(d["a"][0])
	mid = (bid+ask)/2
	return {"bid":bid,"ask":ask,"mid":mid,"spread_pct":(ask-bid)/mid}

def calculate_ema(p,n):
	k=2/(n+1); e=p[0]
	for x in p[1:]: e=x*k+e*(1-k)
	return e

def calculate_sma(p,n): return sum(p[-n:])/n

def calculate_bands(p,period=20):
	sma=calculate_sma(p,period)
	std=(sum((x-sma)**2 for x in p[-period:])/period)**0.5
	return sma,sma+2*std,sma-2*std

def detect_regime(p):
	s=calculate_sma(p,SMA_PERIOD); c=p[-1]
	if c>s*1.05: return "trend_up"
	if c<s*0.95: return "trend_down"
	return "range"

def signal_range(p):
	sma,u,l=calculate_bands(p); c=p[-1]; w=(u-l)/sma
	if w<MIN_BAND_WIDTH_PCT:
		logging.info(f"FILTER:LOW_VOL Width={w*100:.2f}%"); return "HOLD"
	return "BUY" if c<l else "SELL" if c>u else "HOLD"

def signal_trend(p):
	e=calculate_ema(p,EMA_PERIOD); c,pr,ol=p[-1],p[-2],p[-3]
	if ol<=e and pr>e and pr<e*(1+PULLBACK_PCT): return "BUY"
	if ol>=e and pr<e and pr>e*(1-PULLBACK_PCT): return "SELL"
	return "HOLD"

def manage_risks(sig,mp):
	bal=stats["paper_balance"]; risk=bal*0.01
	entry=mp*(1+SLIPPAGE_PCT) if sig=="BUY" else mp*(1-SLIPPAGE_PCT)
	vol=round(risk/(entry*SL_PCT),8)
	sl=entry*(1-SL_PCT) if sig=="BUY" else entry*(1+SL_PCT)
	tp=entry*(1+stats["dynamic_tp_pct"]) if sig=="BUY" else entry*(1-stats["dynamic_tp_pct"])
	return {"side":"buy" if sig=="BUY" else "sell","volume":vol,"stop_loss":round(sl,2),"entry":round(entry,2),"take_profit":round(tp,2),"limit_price":round(entry,1)}

def execute_order(o):
	side,vol,sl,entry,tp,lim=o["side"],o["volume"],o["stop_loss"],o["entry"],o["take_profit"],o["limit_price"]
	if TRADE_MODE=="live":
		data={"nonce":str(int(time.time()*1000)),"ordertype":"limit","type":side,"volume":str(vol),"pair":PAIR,"price":str(lim),"oflags":"post"}
		r=kraken_request("/0/private/AddOrder",data)
		if r.get("error"): logging.error(f"LIVE FAIL {r['error']}"); return
		logging.info(f"LIVE {side.upper()} {vol}@{lim}")
	else:
		slip=random.uniform(0.0005,0.0015)
		entry=entry*(1+slip) if side=="buy" else entry*(1-slip)
		sl=entry*(1-SL_PCT) if side=="buy" else entry*(1+SL_PCT)
		tp=entry*(1+stats["dynamic_tp_pct"]) if side=="buy" else entry*(1-stats["dynamic_tp_pct"])
		logging.info(f"PAPER {side.upper()} {vol}@{entry:.2f} slip{slip*100:.3f}% SL{sl:.2f} TP{tp:.2f}")
	stats["open_position"]={"side":side,"entry":round(entry,2),"sl":round(sl,2),"volume":vol,"tp":round(tp,2)}
	if side=="buy": stats["total_longs"]+=1
	else: stats["total_shorts"]+=1

def check_exit(mp):
	pos=stats["open_position"]
	if not pos: return False
	e,sl,v,s,tp=pos["entry"],pos["sl"],pos["volume"],pos["side"],pos["tp"]
	slh=mp<=sl if s=="buy" else mp>=sl
	tph=mp>=tp if s=="buy" else mp<=tp
	if slh or tph:
		ex=sl if slh else tp
		if TRADE_MODE=="paper":
			es=random.uniform(0.0005,0.0015)
			ex=ex*(1-es) if s=="buy" else ex*(1+es)
		pnl=(ex-e)*v if s=="buy" else (e-ex)*v
		fees=(e*v+ex*v)*FEE_PCT
		pnl-=fees
		stats["paper_balance"]+=pnl; stats["total_pnl"]+=pnl; stats["total_trades"]+=1
		if pnl>0: stats["total_wins"]+=1
		else: stats["last_loss_time"]=datetime.now().isoformat()
		stats["daily_loss_r"]+=abs(pnl)/(stats["paper_balance"]*0.01) if pnl<0 else 0
		stats["last_exit_price"]=ex; stats["open_position"]=None
		logging.info(f"[CLOSE] {'STOP' if slh else 'TP'} {s.upper()} @{ex:.2f} PnL${pnl:.2f} fee${fees:.2f}")
		save_state(stats); return True
	ns=e*1.005 if s=="buy" else e*0.995
	if (s=="buy" and mp>e*1.005 and ns>sl) or (s=="sell" and mp<e*0.995 and ns<sl):
		pos["sl"]=round(ns,2); logging.info(f"[TRAIL] {s.upper()} SL{ns}"); save_state(stats)
	return False

def log_stats():
	t=stats["total_trades"]; w=stats["total_wins"]; wr=(w/t*100) if t else 0
	logging.info(f"[STATS] ${stats['paper_balance']:.2f} | {t} trades | {wr:.0f}% | PnL${stats['total_pnl']:.2f} | {stats['daily_loss_r']:.2f}R")

stats=load_state()
logging.info(f"=== Старт v{VERSION} ===")

while True:
	try:
		today=datetime.now().strftime("%Y-%m-%d")
		if stats["last_day"]!=today: stats["daily_loss_r"]=0; stats["last_day"]=today; save_state(stats)
		if stats["daily_loss_r"]>=DAILY_LOSS_LIMIT_R: logging.info("DAILY_LIMIT"); time.sleep(3600); continue

		prices,volumes=get_ohlc(); ticker=get_ticker()
		if len(prices)<SMA_PERIOD: time.sleep(INTERVAL*60); continue

		if ticker["spread_pct"]>SPREAD_MAX_PCT:
			logging.info(f"SKIP spread {ticker['spread_pct']*100:.3f}%"); time.sleep(INTERVAL*60); continue

		if len(volumes)>=20:
			av=sum(volumes[-20:])/20; rv=sum(volumes[-3:])/3
			if rv<av*VOLUME_THRESHOLD:
				logging.info(f"SKIP vol {rv:.1f}<{av*VOLUME_THRESHOLD:.1f}"); time.sleep(INTERVAL*60); continue

		if stats.get("last_loss_time"):
			ll=datetime.fromisoformat(stats["last_loss_time"])
			if datetime.now()-ll<timedelta(minutes=INTERVAL*COOLDOWN_BARS):
				logging.info("COOLDOWN"); time.sleep(INTERVAL*60); continue

		regime=detect_regime(prices); stats["current_regime"]=regime
		sma,u,l=calculate_bands(prices); bw=(u-l)/sma
		stats["dynamic_tp_pct"]=max(MIN_TP_PCT,min(MAX_TP_PCT,bw*0.75))
		cur=ticker["mid"]; pos=stats["open_position"]
		stats["tick_counter"]=stats.get("tick_counter",0)+1
		if stats["tick_counter"]>=STATS_INTERVAL_TICKS: log_stats(); stats["tick_counter"]=0; save_state(stats)

		if pos: check_exit(cur)
		else:
			sig=signal_range(prices) if regime=="range" else signal_trend(prices)
			if sig!="HOLD" and abs(cur-stats["last_exit_price"])>stats["last_exit_price"]*0.002:
				execute_order(manage_risks(sig,cur)); save_state(stats)

		time.sleep(INTERVAL*60)
	except Exception as e: logging.error(f"Ошибка {e}"); time.sleep(60)
