import os
import time
import json
import sqlite3
import threading
import logging
import requests
import signal
import sys
import csv
from datetime import datetime, timezone
from queue import Queue
from flask import Flask, request, abort
import hmac
import hashlib
import base64

# --- 1. LOGGING CONFIGURATION ---
LOG_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(LOG_DIR, exist_ok=True)
ERROR_LOG_FILE = os.path.join(LOG_DIR, "error_trades.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ERROR_LOG_FILE, encoding='utf-8')
    ]
)

app = Flask(__name__)

# --- 2. ENVIRONMENT VARIABLES & SECRETS ---
API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_PASSPHRASE")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise RuntimeError("Critical Security Error: WEBHOOK_SECRET environment variable is missing.")

ID_INSTANCE = os.getenv("GREEN_API_ID_INSTANCE")
API_TOKEN = os.getenv("GREEN_API_TOKEN")
MY_PHONE_CHAT_ID = os.getenv("MY_PHONE_CHAT_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not all([API_KEY, API_SECRET, API_PASSPHRASE, ID_INSTANCE, API_TOKEN, MY_PHONE_CHAT_ID]):
    raise RuntimeError("Missing required environment variables. Please check your config/environment settings.")

# --- 3. STRATEGY, RISK & FEATURE PARAMETERS ---
MA_SHORT = 9
MA_LONG = 21
EMA_TREND_PERIOD = 200
EMA_TREND_1H_PERIOD = 50
ADX_PERIOD = 14
ADX_THRESHOLD = 25.0
ATR_PERIOD = 14
RSI_PERIOD = 14
RSI_LOWER = 40
RSI_UPPER = 65

RISK_PER_TRADE_PCT = 0.02
MAX_SLIPPAGE_PCT = 0.01
MAX_POSITION_PCT_CAP = 0.20
ATR_SL_MULTIPLIER = 1.5
ATR_TP1_MULTIPLIER = 1.5
ATR_TP2_MULTIPLIER = 3.0
MAX_DAILY_LOSS_PCT = 0.05
MAX_TRADES_PER_DAY = 3
COOLDOWN_SECONDS = 1800
MIN_TRADE_USDT = 5
LOOP_INTERVAL_SECONDS = 900
FEE_RATE = 0.001

MAX_ALLOWED_SPREAD_PCT = 0.0015
MIN_ORDERBOOK_DEPTH_USDT = 50000

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DB_FILE = os.path.join(DATA_DIR, "bot_database.db")
CSV_JOURNAL_FILE = os.path.join(DATA_DIR, "trade_journal.csv")
EQUITY_CURVE_FILE = os.path.join(DATA_DIR, "equity_curve.csv")

trading_lock = threading.Lock()
shutdown_flag = threading.Event()
background_thread_ref = None

# --- WHATSAPP QUEUE & WORKER ---
whatsapp_queue = Queue()

def whatsapp_worker():
    while not shutdown_flag.is_set():
        try:
            task = whatsapp_queue.get(timeout=1)
            if task is None:
                break
            message_text, chat_id = task
            target_chat = chat_id or MY_PHONE_CHAT_ID
            url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
            payload = {"chatId": target_chat, "message": message_text}

            backoff = 2
            success = False
            for attempt in range(4):
                try:
                    res = requests.post(url, data=json.dumps(payload), timeout=10)
                    if res.status_code in [200, 201]:
                        success = True
                        break
                    elif res.status_code == 429:
                        time.sleep(int(res.headers.get("Retry-After", 5)))
                    else:
                        time.sleep(backoff ** attempt)
                except Exception:
                    time.sleep(backoff ** attempt)

            if not success:
                logging.error(f"WhatsApp queue message failed permanently after retries: {message_text[:30]}...")
            whatsapp_queue.task_done()
        except Exception:
            continue

wa_thread = threading.Thread(target=whatsapp_worker, daemon=True)
wa_thread.start()

def send_whatsapp_message(message_text, chat_id=None):
    whatsapp_queue.put((message_text, chat_id))

# --- GROQ AI CHAT ---
def get_ai_reply(user_message: str) -> str:
    if not GROQ_API_KEY:
        return "AI chat disabled. Server එකේ GROQ_API_KEY environment variable එක set කරන්න."

    system_prompt = (
        "You are a helpful assistant integrated with a KuCoin BTC-USDT trading bot. "
        "You can speak both Sinhala and English. Always reply in the same language the user is using. "
        "If the user writes in Sinhala, reply in Sinhala. If in English, reply in English. "
        "You may mix both if the user mixes. Be friendly, clear and concise. "
        "This bot uses technical indicators (SMA, EMA, MACD, RSI, ADX, ATR). "
        "If the user wants to run a trading cycle, tell them to type: trade / status / cycle / run. "
        "Do not give financial advice. Keep answers safe and helpful."
    )

    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.7,
            "max_tokens": 1024
        }
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        if res.status_code == 200:
            data = res.json()
            return data["choices"][0]["message"]["content"].strip()
        else:
            logging.error(f"Groq API error: {res.status_code} - {res.text[:200]}")
            return "සමාවෙන්න, දැන් AI එකට reply කරන්න බැරි වුණා. / Sorry, AI is temporarily unavailable."
    except Exception as e:
        logging.error(f"Groq request failed: {e}")
        return "සමාවෙන්න, දෝෂයක් ආවා. / Sorry, something went wrong."

# --- 4. PERSISTENT STORAGE ---
def init_db():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with sqlite3.connect(DB_FILE, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trade_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    action TEXT,
                    price REAL,
                    size_or_amount REAL,
                    reason TEXT,
                    order_id TEXT
                )
            ''')
            conn.commit()

            cursor.execute("SELECT value FROM bot_state WHERE key = 'state_json'")
            row = cursor.fetchone()
            default_state = {
                "in_position": False,
                "entry_price": 0.0,
                "position_size": 0.0,
                "initial_position_size": 0.0,
                "dynamic_sl": 0.0,
                "dynamic_tp": 0.0,
                "tp1_hit": False,
                "highest_price_since_entry": 0.0,
                "break_even_activated": False,
                "day_realized_pnl": 0.0,
                "starting_equity_today": 0.0,
                "trading_halted_today": False,
                "trades_today_count": 0,
                "last_trade_time": 0.0,
                "last_candle_timestamp": 0,
                "last_reset_date": time.strftime("%Y-%m-%d"),
                "last_heartbeat_time": 0.0,
                "last_news_check_date": "",
                "cached_news_events": []
            }

            if not row:
                cursor.execute("INSERT INTO bot_state (key, value) VALUES ('state_json', ?)", (json.dumps(default_state),))
                conn.commit()
            else:
                existing_state = json.loads(row[0])
                if "cached_news_events" not in existing_state:
                    existing_state["cached_news_events"] = []
                    cursor.execute("REPLACE INTO bot_state (key, value) VALUES ('state_json', ?)", (json.dumps(existing_state),))
                    conn.commit()
    except Exception as e:
        logging.exception(f"Database initialization failed: {e}")

init_db()

def get_state():
    try:
        with sqlite3.connect(DB_FILE, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_state WHERE key = 'state_json'")
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        logging.exception(f"Failed to retrieve state from DB: {e}")
    return {
        "in_position": False, "entry_price": 0.0, "position_size": 0.0,
        "initial_position_size": 0.0, "dynamic_sl": 0.0, "dynamic_tp": 0.0,
        "tp1_hit": False, "highest_price_since_entry": 0.0, "break_even_activated": False,
        "day_realized_pnl": 0.0, "starting_equity_today": 0.0, "trading_halted_today": False,
        "trades_today_count": 0, "last_trade_time": 0.0, "last_candle_timestamp": 0,
        "last_reset_date": time.strftime("%Y-%m-%d"), "last_heartbeat_time": 0.0,
        "last_news_check_date": "", "cached_news_events": []
    }

def save_state(state):
    try:
        with sqlite3.connect(DB_FILE, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("REPLACE INTO bot_state (key, value) VALUES ('state_json', ?)", (json.dumps(state),))
            conn.commit()
    except Exception as e:
        logging.exception(f"Failed to save state to DB: {e}")

def log_trade(action, price, size_or_amount, reason, order_id):
    try:
        with sqlite3.connect(DB_FILE, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO trade_logs (timestamp, action, price, size_or_amount, reason, order_id) VALUES (?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), action, price, size_or_amount, reason, order_id)
            )
            conn.commit()

        file_exists = os.path.isfile(CSV_JOURNAL_FILE)
        with open(CSV_JOURNAL_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Action", "Price", "SizeOrAmount", "Reason", "OrderID"])
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), action, price, size_or_amount, reason, order_id])
    except Exception as e:
        logging.exception(f"Failed to write trade log: {e}")

def log_equity_curve(equity):
    try:
        file_exists = os.path.isfile(EQUITY_CURVE_FILE)
        with open(EQUITY_CURVE_FILE, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "TotalEquity"])
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), equity])
    except Exception as e:
        logging.exception(f"Failed to log equity curve: {e}")

def check_daily_reset(current_equity):
    state = get_state()
    today = time.strftime("%Y-%m-%d")
    if state.get("last_reset_date") != today:
        state["day_realized_pnl"] = 0.0
        state["trading_halted_today"] = False
        state["trades_today_count"] = 0
        state["starting_equity_today"] = current_equity
        state["last_reset_date"] = today
        save_state(state)
    elif state.get("starting_equity_today", 0.0) == 0.0:
        state["starting_equity_today"] = current_equity
        save_state(state)

def check_daily_loss_limit(equity):
    state = get_state()
    check_daily_reset(equity)
    if state.get("trading_halted_today", False):
        return True
    starting_equity = state.get("starting_equity_today", equity)
    max_loss_allowed = starting_equity * MAX_DAILY_LOSS_PCT
    if state.get("day_realized_pnl", 0.0) <= -max_loss_allowed:
        state["trading_halted_today"] = True
        save_state(state)
        return True
    return False

# --- 5. NEWS FILTER & LIQUIDITY CHECK (FAIL-CLOSED) ---
def fetch_economic_calendar_events():
    state = get_state()
    today_str = time.strftime("%Y-%m-%d")
    if state.get("last_news_check_date") == today_str and state.get("cached_news_events"):
        return state.get("cached_news_events", [])

    events = []
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for item in data:
                impact = item.get("impact", "")
                title = item.get("title", "").lower()
                date_str = item.get("date", "")
                if impact == "High" and any(k in title for k in ["fomc", "cpi", "non-farm", "ppi", "rate decision", "fed"]):
                    events.append(date_str)
            state["cached_news_events"] = events
            state["last_news_check_date"] = today_str
            save_state(state)
            return events
        else:
            return None
    except Exception as e:
        logging.error(f"Failed to fetch economic calendar: {e}")
        return None

def is_news_time_restricted():
    try:
        events = fetch_economic_calendar_events()
        if events is None:
            logging.warning("News API failed. Failing closed (pausing trading).")
            return True

        now_utc = datetime.now(timezone.utc)
        for event_date_str in events:
            try:
                event_time = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                time_diff_minutes = (now_utc - event_time).total_seconds() / 60.0
                if -30 <= time_diff_minutes <= 30:
                    return True
            except Exception:
                continue
        return False
    except Exception as e:
        logging.error(f"News filter evaluation error: {e}")
        return True

def check_market_liquidity_and_spread():
    try:
        url = "https://api.kucoin.com/api/v1/market/orderbook/level2_20?symbol=BTC-USDT"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", {})
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids or not asks:
                return False
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            spread_pct = (best_ask - best_bid) / best_bid
            if spread_pct > MAX_ALLOWED_SPREAD_PCT:
                return False
            bid_depth = sum(float(b[0]) * float(b[1]) for b in bids[:5])
            ask_depth = sum(float(a[0]) * float(a[1]) for a in asks[:5])
            if bid_depth < MIN_ORDERBOOK_DEPTH_USDT or ask_depth < MIN_ORDERBOOK_DEPTH_USDT:
                return False
            return True
        return False
    except Exception as e:
        logging.error(f"Market liquidity check error: {e}")
        return False

# --- 6. API REQUESTS ---
def api_request_with_retry(method, url, headers=None, data=None, max_retries=4):
    backoff_factor = 2
    for attempt in range(max_retries):
        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, data=data, timeout=10)
            else:
                return None

            if response.status_code == 429:
                time.sleep(int(response.headers.get("Retry-After", 5)))
                continue
            if response.status_code >= 500:
                time.sleep(backoff_factor ** attempt)
                continue
            if response.status_code in [200, 201]:
                return response
        except requests.exceptions.RequestException:
            pass

        if attempt < max_retries - 1:
            time.sleep(backoff_factor ** attempt)
    return None

def get_kucoin_signature(endpoint, method, body=""):
    try:
        header_timestamp = str(int(time.time() * 1000))
        str_to_sign = header_timestamp + method.upper() + endpoint + body
        signature = base64.b64encode(
            hmac.new(API_SECRET.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')
        passphrase_encoded = base64.b64encode(
            hmac.new(API_SECRET.encode('utf-8'), API_PASSPHRASE.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')

        return {
            'KC-API-KEY': API_KEY,
            'KC-API-SIGN': signature,
            'KC-API-TIMESTAMP': header_timestamp,
            'KC-API-PASSPHRASE': passphrase_encoded,
            'KC-API-KEY-VERSION': '2',
            'Content-Type': 'application/json'
        }
    except Exception as e:
        logging.error(f"Signature generation error: {e}")
        return None

def get_price():
    try:
        url = "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT"
        res = api_request_with_retry("GET", url)
        if res:
            data = res.json()
            if data.get("code") == "200000":
                val = float(data.get("data", {}).get("price", 0))
                if val > 0:
                    return val
    except Exception as e:
        logging.error(f"Failed to fetch price: {e}")
    return None

def get_balances():
    try:
        endpoint = "/api/v1/accounts"
        headers = get_kucoin_signature(endpoint, "GET")
        if not headers:
            return None, None
        res = api_request_with_retry("GET", f"https://api.kucoin.com{endpoint}?type=trade", headers=headers)
        if res:
            data = res.json()
            if data.get("code") == "200000":
                usdt_bal, btc_bal = 0.0, 0.0
                for acc in data.get("data", []):
                    if acc.get("currency") == "USDT":
                        usdt_bal = float(acc.get("available", 0))
                    elif acc.get("currency") == "BTC":
                        btc_bal = float(acc.get("available", 0))
                return usdt_bal, btc_bal
    except Exception as e:
        logging.error(f"Failed to fetch balances: {e}")
    return None, None

def get_klines(timeframe="15min", limit=250):
    try:
        url = f"https://api.kucoin.com/api/v1/market/candles?symbol=BTC-USDT&type={timeframe}"
        res = api_request_with_retry("GET", url)
        if res:
            data = res.json()
            if data.get("code") == "200000":
                raw_candles = data.get("data", [])
                closed_candles = raw_candles[1:limit+1] if len(raw_candles) > 1 else []
                candles = list(reversed(closed_candles))
                timestamps = [int(c[0]) for c in candles]
                opens = [float(c[1]) for c in candles]
                highs = [float(c[3]) for c in candles]
                lows = [float(c[4]) for c in candles]
                closes = [float(c[2]) for c in candles]
                volumes = [float(c[5]) for c in candles]
                return timestamps, opens, highs, lows, closes, volumes
    except Exception as e:
        logging.error(f"Failed to fetch klines for {timeframe}: {e}")
    return [], [], [], [], [], []

def get_order_details(order_id):
    try:
        endpoint = f"/api/v1/orders/{order_id}"
        headers = get_kucoin_signature(endpoint, "GET")
        if not headers:
            return None
        res = api_request_with_retry("GET", f"https://api.kucoin.com{endpoint}", headers=headers)
        if res:
            data = res.json()
            if data.get("code") == "200000":
                return data.get("data", {})
    except Exception as e:
        logging.error(f"Failed to get order details for {order_id}: {e}")
    return None

def get_order_by_client_oid(client_oid):
    try:
        endpoint = f"/api/v1/orders/client-order/{client_oid}"
        headers = get_kucoin_signature(
