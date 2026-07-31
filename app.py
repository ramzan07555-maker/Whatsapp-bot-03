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

# ඔබ ලබා දුන් අංකය හරියටම මෙහි යොදා ඇත
MY_PHONE_CHAT_ID = "966572686730@c.us"

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
            # නිවැරදි කළ Green API URL එක මෙහි යොදා ඇත
            url = f"https://7107.api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
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

# --- API REQUESTS ---
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
        headers = get_kucoin_signature(endpoint, "GET")
        if not headers:
            return None
        res = api_request_with_retry("GET", f"https://api.kucoin.com{endpoint}", headers=headers)
        if res:
            data = res.json()
            if data.get("code") == "200000":
                return data.get("data", {})
    except Exception as e:
        logging.error(f"Failed to get order by client OID {client_oid}: {e}")
    return None

# --- STATE RECONCILIATION LOGIC ---
def reconcile_state_with_exchange():
    try:
        _, btc_bal = get_balances()
        if btc_bal is None:
            return

        state = get_state()
        in_pos = state.get("in_position", False)
        dust_threshold = 0.00001

        if btc_bal > dust_threshold and not in_pos:
            price = get_price() or 0.0
            state["in_position"] = True
            state["position_size"] = btc_bal
            state["initial_position_size"] = btc_bal
            if state.get("entry_price", 0.0) == 0.0:
                state["entry_price"] = price
            if state.get("highest_price_since_entry", 0.0) == 0.0:
                state["highest_price_since_entry"] = price
            save_state(state)
            send_whatsapp_message(f"🔄 **State Reconciled**: Detected {btc_bal} BTC on exchange. Bot state updated to IN POSITION.")

        elif btc_bal <= dust_threshold and in_pos:
            state["in_position"] = False
            state["entry_price"] = 0.0
            state["position_size"] = 0.0
            state["initial_position_size"] = 0.0
            state["dynamic_sl"] = 0.0
            state["dynamic_tp"] = 0.0
            state["tp1_hit"] = False
            state["highest_price_since_entry"] = 0.0
            state["break_even_activated"] = False
            save_state(state)
            send_whatsapp_message("🔄 **State Reconciled**: 0 BTC found on exchange. Bot state updated to OUT OF POSITION.")
    except Exception as e:
        logging.error(f"Error during state reconciliation: {e}")

# --- ORDER EXECUTION ---
def place_market_buy_with_slippage_check(amount_usdt, expected_price):
    for attempt in range(3):
        client_oid = f"buy_{int(time.time() * 1000)}_{attempt}"
        try:
            endpoint = "/api/v1/orders"
            body_dict = {
                "clientOid": client_oid,
                "side": "buy",
                "symbol": "BTC-USDT",
                "type": "market",
                "funds": str(amount_usdt)
            }
            body_str = json.dumps(body_dict)
            headers = get_kucoin_signature(endpoint, "POST", body_str)
            if not headers:
                continue
            res = api_request_with_retry("POST", f"https://api.kucoin.com{endpoint}", headers=headers, data=body_str)
            if res:
                res_data = res.json()
                if res_data.get("code") == "200000":
                    order_id = res_data.get('data', {}).get('orderId')
                    for _ in range(5):
                        time.sleep(1.0)
                        details = get_order_details(order_id)
                        if details:
                            deal_size = float(details.get("dealSize", 0) or 0)
                            deal_funds = float(details.get("dealFunds", 0) or 0)
                            is_done = details.get("isDone", False)
                            if is_done or deal_size > 0:
                                filled_price = deal_funds / deal_size if deal_size > 0 else expected_price
                                slippage = abs(filled_price - expected_price) / expected_price
                                if slippage > MAX_SLIPPAGE_PCT:
                                    logging.warning(f"Slippage violation on BUY! Filled: {filled_price}, Expected: {expected_price}")
                                return order_id, deal_size, deal_funds, is_done

            fallback = get_order_by_client_oid(client_oid)
            if fallback and fallback.get("id"):
                deal_size = float(fallback.get("dealSize", 0) or 0)
                deal_funds = float(fallback.get("dealFunds", 0) or 0)
                return fallback.get("id"), deal_size, deal_funds, fallback.get("isDone", False)
        except Exception as e:
            logging.error(f"Market buy execution error on attempt {attempt+1}: {e}")
        time.sleep(2.0)
    return None, 0.0, 0.0, False

def place_market_sell_with_slippage_check(size, expected_price):
    for attempt in range(3):
        client_oid = f"sell_{int(time.time() * 1000)}_{attempt}"
        try:
            endpoint = "/api/v1/orders"
            body_dict = {
                "clientOid": client_oid,
                "side": "sell",
                "symbol": "BTC-USDT",
                "type": "market",
                "size": str(size)
            }
            body_str = json.dumps(body_dict)
            headers = get_kucoin_signature(endpoint, "POST", body_str)
            if not headers:
                continue
            res = api_request_with_retry("POST", f"https://api.kucoin.com{endpoint}", headers=headers, data=body_str)
            if res:
                res_data = res.json()
                if res_data.get("code") == "200000":
                    order_id = res_data.get('data', {}).get('orderId')
                    for _ in range(5):
                        time.sleep(1.0)
                        details = get_order_details(order_id)
                        if details:
                            deal_size = float(details.get("dealSize", 0) or 0)
                            deal_funds = float(details.get("dealFunds", 0) or 0)
                            is_done = details.get("isDone", False)
                            if is_done or deal_size > 0:
                                filled_price = deal_funds / deal_size if deal_size > 0 else expected_price
                                slippage = abs(filled_price - expected_price) / expected_price
                                if slippage > MAX_SLIPPAGE_PCT:
                                    logging.warning(f"Slippage violation on SELL! Filled: {filled_price}, Expected: {expected_price}")
                                return order_id, deal_size, deal_funds, is_done

            fallback = get_order_by_client_oid(client_oid)
            if fallback and fallback.get("id"):
                deal_size = float(fallback.get("dealSize", 0) or 0)
                deal_funds = float(fallback.get("dealFunds", 0) or 0)
                return fallback.get("id"), deal_size, deal_funds, fallback.get("isDone", False)
        except Exception as e:
            logging.error(f"Market sell execution error on attempt {attempt+1}: {e}")
        time.sleep(2.0)
    return None, 0.0, 0.0, False

# --- TECHNICAL INDICATORS ---
def calculate_ema(data, period):
    if not data:
        return 0.0
    if len(data) < period:
        return sum(data) / len(data)
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_sma(data, period):
    if not data:
        return 0.0
    if len(data) < period:
        return sum(data) / len(data)
    return sum(data[-period:]) / period

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if not tr_list:
        return 0.0
    atr = sum(tr_list[:min(period, len(tr_list))]) / min(period, len(tr_list))
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def calculate_true_macd(closes, fast=12, slow=26, signal_period=9):
    min_required = slow + signal_period
    if not closes or len(closes) < min_required:
        return 0.0, 0.0, 0.0, 0.0

    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)

    ema_fast = sum(closes[:fast]) / fast
    ema_slow = sum(closes[:slow]) / slow

    macd_line_series = []
    for i in range(len(closes)):
        if i >= fast:
            ema_fast = (closes[i] - ema_fast) * k_fast + ema_fast
        if i >= slow:
            ema_slow = (closes[i] - ema_slow) * k_slow + ema_slow
            if i >= slow - 1:
                macd_line_series.append(ema_fast - ema_slow)

    if len(macd_line_series) < signal_period:
        return 0.0, 0.0, 0.0, 0.0

    k_signal = 2 / (signal_period + 1)
    signal_series = []
    sig_ema = sum(macd_line_series[:signal_period]) / signal_period

    for i in range(len(macd_line_series)):
        if i < signal_period:
            signal_series.append(sig_ema)
        else:
            sig_ema = (macd_line_series[i] - sig_ema) * k_signal + sig_ema
            signal_series.append(sig_ema)

    current_macd = macd_line_series[-1]
    prev_macd = macd_line_series[-2] if len(macd_line_series) > 1 else current_macd
    current_signal = signal_series[-1]
    prev_signal = signal_series[-2] if len(signal_series) > 1 else current_signal

    return current_macd, current_signal, prev_macd, prev_signal

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return 20.0
    try:
        tr_list, plus_dm_list, minus_dm_list = [], [], []
        for i in range(1, len(closes)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            tr_list.append(tr)
            up_move = highs[i] - highs[i-1]
            down_move = lows[i-1] - lows[i]
            plus_dm_list.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm_list.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        atr_smooth = sum(tr_list[:period])
        plus_smooth = sum(plus_dm_list[:period])
        minus_smooth = sum(minus_dm_list[:period])
        dx_list = []
        for i in range(period, len(tr_list)):
            atr_smooth = atr_smooth - (atr_smooth / period) + tr_list[i]
            plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm_list[i]
            minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm_list[i]
            plus_di = 100 * (plus_smooth / atr_smooth) if atr_smooth > 0 else 0
            minus_di = 100 * (minus_smooth / atr_smooth) if atr_smooth > 0 else 0
            di_sum = plus_di + minus_di
            dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
            dx_list.append(dx)

        if not dx_list:
            return 20.0
        adx = sum(dx_list[:period]) / period
        for dx in dx_list[period:]:
            adx = ((adx * (period - 1)) + dx) / period
        return adx
    except Exception as e:
        logging.error(f"ADX calculation error: {e}")
        return 20.0

def generate_signal():
    timestamps, o15, h15, l15, c15, v15 = get_klines("15min", 250)
    _, _, _, _, c1h, _ = get_klines("1hour", 100)

    if len(c15) < max(MA_LONG, EMA_TREND_PERIOD) or len(c1h) < EMA_TREND_1H_PERIOD:
        return "HOLD", {"reason": "Insufficient history"}, 0

    latest_candle_time = timestamps[-1] if timestamps else 0

    prev_short = calculate_sma(c15[:-1], MA_SHORT)
    curr_short = calculate_sma(c15, MA_SHORT)
    prev_long = calculate_sma(c15[:-1], MA_LONG)
    curr_long = calculate_sma(c15, MA_LONG)

    ema_trend_15m = calculate_ema(c15, EMA_TREND_PERIOD)
    ema_trend_1h = calculate_ema(c1h, EMA_TREND_1H_PERIOD)

    rsi = calculate_rsi(c15, RSI_PERIOD)
    adx = calculate_adx(h15, l15, c15, ADX_PERIOD)
    atr = calculate_atr(h15, l15, c15, ATR_PERIOD)

    vol_sma_20 = calculate_sma(v15, 20)
    volume_ok = v15[-1] > vol_sma_20 if v15 else False

    curr_macd, curr_signal, prev_macd, prev_signal = calculate_true_macd(c15)

    is_bullish_crossover = (prev_short <= prev_long) and (curr_short > curr_long)
    is_bearish_crossover = (prev_short >= prev_long) and (curr_short < curr_long)

    macd_bullish_crossover = (prev_macd <= prev_signal) and (curr_macd > curr_signal)
    macd_bearish_crossover = (prev_macd >= prev_signal) and (curr_macd < curr_signal)

    trend_up = (c15[-1] > ema_trend_15m) and (c1h[-1] > ema_trend_1h)
    trend_down = (c15[-1] < ema_trend_15m) and (c1h[-1] < ema_trend_1H_PERIOD if 'EMA_TREND_1H_PERIOD' in globals() else c1h[-1] < ema_trend_1h)

    adx_ok = adx > ADX_THRESHOLD
    rsi_buy_ok = (RSI_LOWER <= rsi <= RSI_UPPER)
    rsi_sell_ok = (35 <= rsi <= 60)

    if is_bullish_crossover and trend_up and macd_bullish_crossover and adx_ok and rsi_buy_ok and volume_ok:
        return "BUY", {"reason": "BUY: Enhanced filters passed", "atr": atr}, latest_candle_time

    elif is_bearish_crossover and trend_down and macd_bearish_crossover and rsi_sell_ok:
        return "SELL", {"reason": "SELL: Bearish confirmation & RSI filter passed", "atr": atr}, latest_candle_time

    return "HOLD", {"reason": "No condition met", "atr": atr}, latest_candle_time

# --- METRICS ---
def calculate_trading_metrics():
    try:
        if not os.path.isfile(CSV_JOURNAL_FILE):
            return "No trade records found yet."

        pnl_list = []
        equity_list = []

        if os.path.isfile(EQUITY_CURVE_FILE):
            with open(EQUITY_CURVE_FILE, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        equity_list.append(float(row["TotalEquity"]))
                    except:
                        pass

        with open(CSV_JOURNAL_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            open_position = None
            for row in reader:
                action = row["Action"]
                price = float(row["Price"])
                size = float(row["SizeOrAmount"])

                if "BUY" in action:
                    open_position = {"price": price, "size": size}
                elif any(tag in action for tag in ["SELL", "TP", "STOPLOSS", "PARTIAL"]) and open_position:
                    entry_p = open_position["price"]
                    closed_size = size
                    trade_pnl = (price - entry_p) * closed_size - ((price * closed_size + entry_p * closed_size) * FEE_RATE)
                    pnl_list.append(trade_pnl)

                    if "PARTIAL" in action:
                        open_position["size"] -= closed_size
                        if open_position["size"] <= 0:
                            open_position = None
                    else:
                        open_position = None

        total_trades = len(pnl_list)
        if total_trades == 0:
            return "📊 **Performance Metrics**\nTrades: 0 | Win Rate: 0%"

        winning_trades = [p for p in pnl_list if p > 0]
        losing_trades = [p for p in pnl_list if p <= 0]

        win_rate = (len(winning_trades) / total_trades) * 100
        gross_profit = sum(winning_trades) if winning_trades else 0
        gross_loss = abs(sum(losing_trades)) if losing_trades else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit

        max_dd = 0.0
        if equity_list:
            peak = equity_list[0]
            for eq in equity_list:
                if eq > peak:
                    peak = eq
                dd = (peak - eq) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

        return (
            f"📊 **Bot Performance Metrics Dashboard**\n"
            f"• Total Trades: {total_trades}\n"
            f"• Win Rate: {win_rate:.2f}%\n"
            f"• Profit Factor: {profit_factor:.2f}\n"
            f"• Max Drawdown: {max_dd*100:.2f}%\n"
            f"• Gross Profit: {gross_profit:.2f} USDT\n"
            f"• Gross Loss: {gross_loss:.2f} USDT"
        )
    except Exception as e:
        return f"Metrics calculation error: {e}"

# --- 6. MAIN TRADING CYCLE ---
def run_cycle(notify_whatsapp=True):
    with trading_lock:
        reconcile_state_with_exchange()
        state = get_state()

        if is_news_time_restricted():
            return "Paused: Major economic news window active or news API check failed."

        if not check_market_liquidity_and_spread():
            return "Paused: Insufficient liquidity, spread too wide, or orderbook check failed."

        price = get_price()
        if price is None:
            return "Failed to fetch market price (API error)."

        time.sleep(0.3)
        usdt, btc = get_balances()
        if usdt is None or btc is None:
            return "Failed to fetch balances (API error)."

        equity = usdt + (btc * price)
        log_equity_curve(equity)
        halted = check_daily_loss_limit(equity)
        messages = []

        current_time = time.time()
        last_hb = state.get("last_heartbeat_time", 0.0)
        if (current_time - last_hb) >= 14400:
            metrics_summary = calculate_trading_metrics()
            hb_msg = f"💚 **Bot Health Heartbeat & Metrics**\nStatus: Alive 🟢\nEquity: {equity:.2f} USDT\n\n{metrics_summary}"
            send_whatsapp_message(hb_msg)
            state["last_heartbeat_time"] = current_time
            save_state(state)

        signal, info, candle_ts = generate_signal()
        current_atr = info.get("atr", 0.0)

        last_processed_candle = state.get("last_candle_timestamp", 0)
        is_duplicate_candle = (candle_ts > 0 and candle_ts <= last_processed_candle)

        if state["in_position"] and btc > 0:
            entry = state["entry_price"]
            initial_size = state.get("initial_position_size", btc)
            highest_price = max(state.get("highest_price_since_entry", entry), price)
            state["highest_price_since_entry"] = highest_price

            dynamic_sl = state.get("dynamic_sl", entry - (current_atr * ATR_SL_MULTIPLIER))
            dynamic_tp2 = state.get("dynamic_tp", entry + (current_atr * ATR_TP2_MULTIPLIER))
            tp1_price = entry + (current_atr * ATR_TP1_MULTIPLIER)

            tp1_hit = state.get("tp1_hit", False)
            if not tp1_hit and price >= tp1_price and initial_size > 0:
                half_size = initial_size * 0.5
                if btc >= half_size:
                    order_id, filled_size, deal_funds, is_done = place_market_sell_with_slippage_check(half_size, price)
                    if order_id and filled_size > 0:
                        exit_price = deal_funds / filled_size if filled_size > 0 else price
                        net_realized = (exit_price * filled_size * (1 - FEE_RATE)) - (entry * filled_size * (1 + FEE_RATE))
                        state["day_realized_pnl"] = state.get("day_realized_pnl", 0.0) + net_realized
                        state["position_size"] = max(0.0, btc - filled_size)
                        state["tp1_hit"] = True
                        if entry > dynamic_sl:
                            dynamic_sl = entry
                        state["dynamic_sl"] = dynamic_sl
                        save_state(state)
                        log_trade("PARTIAL_TP1", exit_price, filled_size, "TP1 reached", order_id)
                        messages.append(f"🎯 Partial TP1 hit! Closed 50% @ {exit_price}")

            trailing_sl = highest_price - (current_atr * ATR_SL_MULTIPLIER)
            if trailing_sl > dynamic_sl:
                dynamic_sl = trailing_sl
            state["dynamic_sl"] = dynamic_sl
            save_state(state)

            is_stop_loss = price <= dynamic_sl
            is_take_profit = price >= dynamic_tp2
            is_strong_reversal = (signal == "SELL") and (not is_duplicate_candle)

            if is_stop_loss or is_take_profit or is_strong_reversal:
                reason_str = "Stop-loss hit" if is_stop_loss else ("Final Take-profit hit" if is_take_profit else "Strong bearish reversal signal")
                action_tag = "SELL_STOPLOSS" if is_stop_loss else ("SELL_TAKEPROFIT" if is_take_profit else "SELL_REVERSAL")

                current_holdings_size = state.get("position_size", btc)
                order_id, filled_size, deal_funds, is_done = place_market_sell_with_slippage_check(current_holdings_size, price)
                if order_id and filled_size > 0:
                    exit_price = deal_funds / filled_size if filled_size > 0 else price
                    net_realized = (exit_price * filled_size * (1 - FEE_RATE)) - (entry * filled_size * (1 + FEE_RATE))

                    state["day_realized_pnl"] = state.get("day_realized_pnl", 0.0) + net_realized
                    state["in_position"] = False
                    state["entry_price"] = 0.0
                    state["position_size"] = 0.0
                    state["initial_position_size"] = 0.0
                    state["dynamic_sl"] = 0.0
                    state["dynamic_tp"] = 0.0
                    state["tp1_hit"] = False
                    state["highest_price_since_entry"] = 0.0
                    state["break_even_activated"] = False
                    if candle_ts > 0:
                        state["last_candle_timestamp"] = candle_ts
                    save_state(state)
                    log_trade(action_tag, exit_price, filled_size, reason_str, order_id)
                    messages.append(f"🔴 Position fully closed ({reason_str})! P&L: {net_realized:.2f} USDT")

        state = get_state()
        usdt, btc = get_balances()
        if usdt is None or btc is None:
            return "Failed to re-fetch balances after position check."

        if halted:
            return "Trading halted: Daily loss limit reached."

        last_processed_candle = state.get("last_candle_timestamp", 0)
        is_duplicate_candle = (candle_ts > 0 and candle_ts <= last_processed_candle)

        last_trade_time = state.get("last_trade_time", 0.0)
        trades_today = state.get("trades_today_count", 0)
        in_cooldown = (current_time - last_trade_time) < COOLDOWN_SECONDS

        if not state["in_position"]:
            if signal == "BUY":
                if is_duplicate_candle:
                    messages.append(f"🛡️ Duplicate Candle Protection: Timestamp {candle_ts} already processed.")
                elif in_cooldown:
                    messages.append("⏳ Cooldown active.")
                elif trades_today >= MAX_TRADES_PER_DAY:
                    messages.append("⚠️ Max daily trades reached.")
                else:
                    if current_atr <= 0:
                        return "Skipped: ATR invalid or zero."

                    risk_amount = equity * RISK_PER_TRADE_PCT
                    sl_distance = current_atr * ATR_SL_MULTIPLIER

                    position_size_btc = risk_amount / sl_distance
                    position_value_usdt = position_size_btc * price

                    max_allowed = equity * MAX_POSITION_PCT_CAP
                    position_value_usdt = min(position_value_usdt, max_allowed)

                    trade_usdt = max(MIN_TRADE_USDT, min(position_value_usdt, usdt))

                    if usdt >= trade_usdt:
                        order_id, filled_size, deal_funds, is_done = place_market_buy_with_slippage_check(trade_usdt, price)
                        if order_id and filled_size > 0:
                            actual_qty = filled_size
                            actual_cost = deal_funds if deal_funds > 0 else trade_usdt
                            actual_entry_price = actual_cost / actual_qty if actual_qty > 0 else price

                            state["in_position"] = True
                            state["entry_price"] = actual_entry_price
                            state["position_size"] = actual_qty
                            state["initial_position_size"] = actual_qty
                            state["dynamic_sl"] = actual_entry_price - (current_atr * ATR_SL_MULTIPLIER)
                            state["dynamic_tp"] = actual_entry_price + (current_atr * ATR_TP2_MULTIPLIER)
                            state["tp1_hit"] = False
                            state["highest_price_since_entry"] = actual_entry_price
                            state["trades_today_count"] = trades_today + 1
                            state["last_trade_time"] = current_time
                            if candle_ts > 0:
                                state["last_candle_timestamp"] = candle_ts
                            save_state(state)

                            log_trade("BUY", actual_entry_price, actual_qty, info.get('reason'), order_id)
                            messages.append(f"✅ BUY executed: Size {actual_qty:.6f} BTC @ {actual_entry_price}")

        if not messages:
            messages.append("Cycle complete. No action required.")

        report = f"🤖 Bot Cycle Report\nPrice: ${price} | USDT: {usdt:.2f}\n\n" + "\n".join(messages)
        if notify_whatsapp:
            send_whatsapp_message(report)
        return report

def background_trading_loop():
    while not shutdown_flag.is_set():
        try:
            run_cycle(notify_whatsapp=True)
        except Exception as e:
            logging.exception(f"Background loop error: {e}")
        for _ in range(LOOP_INTERVAL_SECONDS):
            if shutdown_flag.is_set():
                break
            time.sleep(1)

def _start_background_loop_once():
    global background_thread_ref
    if getattr(app, "_bg_loop_started", False):
        return
    
    try:
        reconcile_state_with_exchange()
        background_thread_ref = threading.Thread(target=background_trading_loop, daemon=True)
        background_thread_ref.start()
        app._bg_loop_started = True
        logging.info("Background trading loop started successfully.")
    except Exception as e:
        logging.error(f"Failed to start background loop: {e}")

_start_background_loop_once()

def shutdown_link_actions(signum=None, frame=None):
    shutdown_flag.set()
    global background_thread_ref
    if background_thread_ref and background_thread_ref.is_alive():
        background_thread_ref.join(timeout=5.0)
    try:
        whatsapp_queue.put(None)
        state = get_state()
        save_state(state)
        send_whatsapp_message("⚠️ Bot shutdown gracefully. State saved successfully.")
    except Exception as check_err:
        logging.error(f"Shutdown action error: {check_err}")

signal.signal(signal.SIGTERM, shutdown_link_actions)
signal.signal(signal.SIGINT, shutdown_link_actions)

# --- 7. SECURE WEBHOOK (Green API Compatible & AI Chat) ---
@app.route('/webhook/<path:secret_token>', methods=['POST'])
def secure_webhook(secret_token):
    if not hmac.compare_digest(secret_token, WEBHOOK_SECRET):
        abort(403, description="Unauthorized Token")

    data = request.json or {}
    
    logging.info(f"RECEIVED WEBHOOK DATA: {json.dumps(data, indent=2)}")
    
    try:
        if "messageData" in data:
            sender_data = data.get("senderData", {})
            chat_id = sender_data.get("chatId", "")
            
            if not chat_id:
                chat_id = data.get("chatId", "")
            
            logging.info(f"Extracted Chat ID: {chat_id} | Expected: {MY_PHONE_CHAT_ID}")

            if MY_PHONE_CHAT_ID not in chat_id:
                return "OK", 200

            msg_data = data.get("messageData", {})
            text = ""
            type_msg = msg_data.get("typeMessage", "")

            if type_msg == "textMessage":
                text = msg_data.get("textMessageData", {}).get("textMessage", "").strip()
            elif type_msg == "extendedTextMessage":
                text = msg_data.get("extendedTextMessageData", {}).get("text", "").strip()

            if not text:
                return "OK", 200

            text_lower = text.lower().strip()

            trading_triggers = ["trade", "status", "cycle", "run", "bot"]
            is_trading_cmd = any(
                text_lower == t or text_lower.startswith(t + " ")
                for t in trading_triggers
            )

            if is_trading_cmd:
                threading.Thread(
                    target=run_cycle,
                    kwargs={"notify_whatsapp": True},
                    daemon=True
                ).start()
            else:
                def ai_reply_task():
                    reply = get_ai_reply(text)
                    send_whatsapp_message(reply, chat_id)
                threading.Thread(target=ai_reply_task, daemon=True).start()

    except Exception as e:
        logging.error(f"Webhook processing error: {e}")
    return "OK", 200

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    finally:
        shutdown_link_actions()
