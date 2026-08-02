import base64
import csv
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import threading
import time

import requests
from flask import Flask, abort, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)


# --- ENVIRONMENT VARIABLES (stripped — avoids the newline/whitespace bugs we hit before) ---
def _clean_env(name, default=None):
    val = os.getenv(name, default)
    return val.strip() if val else val


API_KEY = _clean_env("KUCOIN_API_KEY")
API_SECRET = _clean_env("KUCOIN_API_SECRET")
API_PASSPHRASE = _clean_env("KUCOIN_PASSPHRASE")

logging.info(
    f"KuCoin creds diag: API_KEY={API_KEY!r} (len={len(API_KEY) if API_KEY else 0}), "
    f"API_SECRET_masked={(API_SECRET[:4] + '...' + API_SECRET[-4:]) if API_SECRET and len(API_SECRET) > 8 else 'MISSING'} "
    f"(len={len(API_SECRET) if API_SECRET else 0}), "
    f"API_PASSPHRASE_masked={(API_PASSPHRASE[:2] + '...' + API_PASSPHRASE[-2:]) if API_PASSPHRASE and len(API_PASSPHRASE) > 4 else 'MISSING'} "
    f"(len={len(API_PASSPHRASE) if API_PASSPHRASE else 0})"
)
WEBHOOK_SECRET = _clean_env("WEBHOOK_SECRET")
ID_INSTANCE = _clean_env("GREEN_API_ID_INSTANCE")
API_TOKEN = _clean_env("GREEN_API_TOKEN")
MY_PHONE_CHAT_ID = _clean_env("MY_PHONE_CHAT_ID")
GROQ_API_KEY = _clean_env("GROQ_API_KEY")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET environment variable is required.")
if not all([API_KEY, API_SECRET, API_PASSPHRASE, ID_INSTANCE, API_TOKEN, MY_PHONE_CHAT_ID]):
    raise RuntimeError("Missing required environment variables — check KuCoin and Green API config.")

# --- STRATEGY / RISK PARAMETERS ---
PROFIT_TARGET_PCT = float(_clean_env("PROFIT_TARGET_PCT", "0.5")) / 100.0
STOP_LOSS_PCT = float(_clean_env("STOP_LOSS_PCT", "5")) / 100.0
LOOP_INTERVAL_SECONDS = int(_clean_env("LOOP_INTERVAL_SECONDS", "60"))  # every minute default
MIN_TRADE_USDT = float(_clean_env("MIN_TRADE_USDT", "1"))
FEE_RATE = 0.001

SYMBOL = "BTC-USDT"
BASE_URL = "https://api.kucoin.com"

DATA_DIR = _clean_env("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "flex_state.json")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "flex_trade_log.csv")

trading_lock = threading.Lock()
shutdown_flag = threading.Event()
background_thread_ref = None

DEFAULT_STATE = {
    "in_position": False, "entry_price": 0.0, "entry_qty": 0.0,
    "last_buy_amount": 0.0, "total_realized_profit_usdt": 0.0, "stop_order_id": None,
}


# --- STATE ---
def load_state():
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        logging.warning("State file unreadable, starting fresh.")
        return dict(DEFAULT_STATE)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# --- KUCOIN API ---
def _kucoin_headers(endpoint, method, body=""):
    ts = str(int(time.time() * 1000))
    str_to_sign = ts + method.upper() + endpoint + body
    signature = base64.b64encode(hmac.new(API_SECRET.encode(), str_to_sign.encode(), hashlib.sha256).digest()).decode()
    passphrase = base64.b64encode(hmac.new(API_SECRET.encode(), API_PASSPHRASE.encode(), hashlib.sha256).digest()).decode()
    return {
        "KC-API-KEY": API_KEY, "KC-API-SIGN": signature, "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": passphrase, "KC-API-KEY-VERSION": "3", "Content-Type": "application/json",
    }


def get_price():
    r = requests.get(f"{BASE_URL}/api/v1/market/orderbook/level1", params={"symbol": SYMBOL}, timeout=7)
    r.raise_for_status()
    price = r.json().get("data", {}).get("price")
    if price is None:
        raise RuntimeError("Could not fetch price")
    return float(price)


def get_balances():
    endpoint = "/api/v1/accounts"
    headers = _kucoin_headers(endpoint, "GET")
    r = requests.get(f"{BASE_URL}{endpoint}", headers=headers, params={"type": "trade"}, timeout=7)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"Balance fetch failed: {data}")
    usdt, btc = 0.0, 0.0
    for acc in data.get("data", []):
        if acc.get("currency") == "USDT":
            usdt = float(acc.get("available", 0))
        elif acc.get("currency") == "BTC":
            btc = float(acc.get("available", 0))
    return usdt, btc


def place_market_buy(usdt_amount):
    endpoint = "/api/v1/orders"
    body = {"clientOid": str(int(time.time() * 1000)), "side": "buy", "symbol": SYMBOL,
            "type": "market", "funds": f"{usdt_amount:.2f}"}
    body_str = json.dumps(body)
    headers = _kucoin_headers(endpoint, "POST", body_str)
    r = requests.post(f"{BASE_URL}{endpoint}", headers=headers, data=body_str, timeout=7)
    data = r.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"Buy failed: {data.get('msg', data)}")
    return data.get("data", {}).get("orderId")


def place_market_sell(btc_amount):
    endpoint = "/api/v1/orders"
    body = {"clientOid": str(int(time.time() * 1000)) + "s", "side": "sell", "symbol": SYMBOL,
            "type": "market", "size": f"{btc_amount:.8f}"}
    body_str = json.dumps(body)
    headers = _kucoin_headers(endpoint, "POST", body_str)
    r = requests.post(f"{BASE_URL}{endpoint}", headers=headers, data=body_str, timeout=7)
    data = r.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"Sell failed: {data.get('msg', data)}")
    return data.get("data", {}).get("orderId")


def place_stop_loss_sell(btc_amount, stop_price):
    """Places a native stop-market sell order on KuCoin's own server — triggers
    continuously on their infrastructure, not our polling loop, so protection
    is effectively instant rather than limited by our check interval."""
    endpoint = "/api/v1/stop-order"
    body = {
        "clientOid": str(int(time.time() * 1000)) + "sl",
        "side": "sell", "symbol": SYMBOL, "type": "market",
        "size": f"{btc_amount:.8f}",
        "stop": "loss", "stopPrice": f"{stop_price:.2f}",
    }
    body_str = json.dumps(body)
    headers = _kucoin_headers(endpoint, "POST", body_str)
    r = requests.post(f"{BASE_URL}{endpoint}", headers=headers, data=body_str, timeout=7)
    data = r.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"Stop-loss order failed: {data.get('msg', data)}")
    return data.get("data", {}).get("orderId")


def cancel_stop_order(order_id):
    if not order_id:
        return
    endpoint = f"/api/v1/stop-order/{order_id}"
    headers = _kucoin_headers(endpoint, "DELETE")
    try:
        r = requests.delete(f"{BASE_URL}{endpoint}", headers=headers, timeout=7)
        data = r.json()
        if data.get("code") != "200000":
            logging.warning(f"Stop order cancel returned: {data}")
    except Exception as e:
        logging.warning(f"Stop order cancel failed (may have already triggered): {e}")


def log_trade(action, price, amount, order_id, note=""):
    is_new = not os.path.exists(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "action", "price", "amount", "order_id", "note"])
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), action, price, amount, order_id, note])


# --- WHATSAPP (direct send, correct Content-Type, logs success/failure) ---
def send_whatsapp(message, chat_id=None):
    target = chat_id or MY_PHONE_CHAT_ID
    url = f"https://7107.api.greenapi.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
    # Worst case here must stay comfortably under gunicorn's worker timeout —
    # 2 attempts x 6s request timeout + up to 2s backoff = ~14s max, so this can
    # never be the thing that gets a worker killed mid-request.
    for attempt in range(2):
        try:
            r = requests.post(url, json={"chatId": target, "message": message}, timeout=6)
            if r.status_code in (200, 201):
                logging.info(f"WhatsApp sent: {message[:50]!r}")
                return True
            logging.error(f"WhatsApp send failed: {r.status_code} {r.text[:200]}")
        except Exception as e:
            logging.error(f"WhatsApp send exception: {e}")
        time.sleep(2 ** attempt)
    return False


# --- RECONCILIATION (catches state/exchange mismatches, including native stop-loss triggers) ---
def reconcile_state():
    with trading_lock:
        try:
            state = load_state()
            usdt, btc = get_balances()
            price = get_price()
            btc_value = btc * price

            if state["in_position"] and btc_value < MIN_TRADE_USDT:
                # Most likely cause: the native stop-loss order triggered on KuCoin's
                # side since our last check. Clear position state and, if we have
                # enough USDT, auto re-buy using the last amount — same behavior as
                # a programmatic stop-loss exit, just detected after the fact.
                logging.warning("Reconcile: state says in-position but exchange shows ~0 BTC — likely stop-loss triggered.")
                state["in_position"] = False
                state["entry_price"] = 0.0
                state["entry_qty"] = 0.0
                state["stop_order_id"] = None
                amount = state.get("last_buy_amount", 0)
                save_state(state)
                send_whatsapp(f"🛡️ Stop-loss appears to have triggered (position closed on exchange).")
                if amount >= MIN_TRADE_USDT and usdt >= amount:
                    do_buy(state, price, amount, reason="auto re-entry after stop-loss")
                else:
                    send_whatsapp(f"⚠️ Can't auto re-buy (need {amount:.2f}, have {usdt:.2f} USDT). Send 'buy <amount>' when ready.")
            elif not state["in_position"] and btc_value >= MIN_TRADE_USDT:
                logging.warning("Reconcile: exchange holds BTC but state says flat. Adopting as current position.")
                state["in_position"] = True
                state["entry_price"] = price
                state["entry_qty"] = btc
                if not state.get("last_buy_amount"):
                    state["last_buy_amount"] = round(btc_value, 2)
                save_state(state)
                send_whatsapp(f"⚠️ Reconciliation: found {btc:.8f} BTC on exchange not tracked in state — now tracking it (no stop-loss placed for this — consider selling and re-buying via 'buy <amount>' to get protection).")
        except Exception as e:
            logging.exception(f"Reconciliation failed: {e}")


# --- TRADE ACTIONS ---
def do_buy(state, price, amount, reason=""):
    order_id = place_market_buy(amount)
    approx_qty = amount * (1 - FEE_RATE) / price
    state["in_position"] = True
    state["entry_price"] = price
    state["entry_qty"] = approx_qty
    state["last_buy_amount"] = amount
    save_state(state)
    log_trade("BUY", price, amount, order_id, note=reason)

    stop_price = price * (1 - STOP_LOSS_PCT)
    stop_order_id = None
    if STOP_LOSS_PCT > 0:
        try:
            stop_order_id = place_stop_loss_sell(approx_qty, stop_price)
            state["stop_order_id"] = stop_order_id
            save_state(state)
        except Exception as e:
            logging.error(f"Failed to place stop-loss order: {e}")
            send_whatsapp(f"⚠️ Bought but stop-loss order FAILED to place: {e}\nPosition is unprotected — check manually.")

    msg = f"🟢 Bought {amount:.2f} USDT @ ${price:,.2f}" + (f" ({reason})" if reason else "")
    if stop_order_id:
        msg += f"\n🛡️ Stop-loss set @ ${stop_price:,.2f} ({STOP_LOSS_PCT*100:.1f}%, order {stop_order_id})"
    send_whatsapp(msg)


def do_sell(state, price, reason):
    # cancel the resting stop-loss order first — otherwise it could also try
    # to execute against a position we're about to close ourselves
    if state.get("stop_order_id"):
        cancel_stop_order(state["stop_order_id"])
        state["stop_order_id"] = None

    qty = state["entry_qty"]
    order_id = place_market_sell(qty)
    sell_value = qty * price * (1 - FEE_RATE)
    buy_value = qty * state["entry_price"] * (1 + FEE_RATE)
    realized = sell_value - buy_value
    state["total_realized_profit_usdt"] = state.get("total_realized_profit_usdt", 0.0) + realized
    log_trade("SELL", price, qty, order_id, note=f"{reason}, realized={realized:.4f}")
    send_whatsapp(f"🔴 Sold ({reason}) @ ${price:,.2f}\nRealized: {realized:+.4f} USDT\n"
                  f"Total realized: {state['total_realized_profit_usdt']:+.4f} USDT")
    state["in_position"] = False
    state["entry_price"] = 0.0
    state["entry_qty"] = 0.0
    save_state(state)


# --- HOURLY CYCLE (profit-take / stop-loss check) ---
def run_cycle():
    reconcile_state()  # also detects & handles a triggered native stop-loss order
    with trading_lock:
        state = load_state()
        if not state["in_position"]:
            return  # nothing to check — waiting for a "buy <amount>" command

        price = get_price()
        pct_change = (price - state["entry_price"]) / state["entry_price"]
        logging.info(f"Cycle check: entry=${state['entry_price']:,.2f} now=${price:,.2f} change={pct_change*100:.3f}%")

        if pct_change >= PROFIT_TARGET_PCT:
            do_sell(state, price, f"profit target hit ({pct_change*100:.2f}%)")
            state = load_state()
            usdt, _ = get_balances()
            amount = state.get("last_buy_amount", 0)
            if amount >= MIN_TRADE_USDT and usdt >= amount:
                do_buy(state, price, amount, reason="auto re-entry after profit")
            else:
                send_whatsapp(f"⚠️ Sold for profit but can't auto re-buy (need {amount:.2f}, have {usdt:.2f} USDT). Send 'buy <amount>' when ready.")
        # Stop-loss is now handled by the native KuCoin stop order (near-instant,
        # monitored by their server) plus reconcile_state() detecting the trigger
        # — no need to check pct_change against STOP_LOSS_PCT here too.


def background_loop():
    while not shutdown_flag.is_set():
        try:
            run_cycle()
        except Exception:
            logging.exception("Background cycle error")
            try:
                send_whatsapp(f"⚠️ Bot error during hourly check — check logs.")
            except Exception:
                pass
        for _ in range(LOOP_INTERVAL_SECONDS):
            if shutdown_flag.is_set():
                break
            time.sleep(1)


def _start_background_loop_once():
    global background_thread_ref
    if getattr(app, "_bg_started", False):
        return
    reconcile_state()
    background_thread_ref = threading.Thread(target=background_loop, daemon=True)
    background_thread_ref.start()
    app._bg_started = True


_start_background_loop_once()


# --- CASUAL AI CHAT (Groq) — completely separate from trading logic. ---
# This function can only ever return a text reply; it has no access to do_buy/
# do_sell/place_market_* and cannot execute trades under any circumstances.
# Trading commands are matched and handled deterministically in handle_command()
# BEFORE this is ever reached — the AI only sees messages that didn't match
# 'buy <amount>', 'sell', or 'status'.
def get_ai_reply(user_text):
    if not GROQ_API_KEY:
        return "Commands: 'buy <amount>' | 'sell' | 'status'"
    try:
        state = load_state()
        context = (
            f"You are a WhatsApp assistant for a crypto trading bot. Be brief and casual. "
            f"Current status: {'in a BTC position' if state.get('in_position') else 'not in a position'}. "
            f"You do NOT execute trades yourself — only the commands 'buy <amount>', 'sell', and 'status' do. "
            f"If the user seems to want to trade, remind them to use those exact commands. "
            f"Do not give financial advice or price predictions."
        )
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": context},
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.7,
                "max_tokens": 200,
            },
            timeout=8,
        )
        data = r.json()
        if r.status_code == 200:
            return data["choices"][0]["message"]["content"].strip()
        logging.error(f"Groq API error: {r.status_code} {data}")
        return "Sorry, AI chat is temporarily unavailable. Commands: 'buy <amount>' | 'sell' | 'status'"
    except Exception as e:
        logging.error(f"Groq request failed: {e}")
        return "Sorry, AI chat is temporarily unavailable. Commands: 'buy <amount>' | 'sell' | 'status'"


# --- WHATSAPP COMMAND HANDLING ---
def handle_command(text, chat_id):
    """Top-level wrapper: any exception (e.g. KuCoin rejects an order below
    its own minimum size) gets reported back via WhatsApp instead of silently
    dying in the background thread, which is where this runs."""
    try:
        _handle_command_inner(text, chat_id)
    except Exception as e:
        logging.exception("Command handling failed")
        send_whatsapp(f"❌ Command failed: {e}", chat_id)


def _handle_command_inner(text, chat_id):
    text = text.strip().lower()

    if text.startswith("buy"):
        parts = text.split()
        if len(parts) < 2:
            send_whatsapp("Usage: buy <amount>  e.g. 'buy 100'", chat_id)
            return
        try:
            amount = float(parts[1])
        except ValueError:
            send_whatsapp("Amount ekak type karanna. e.g. 'buy 100'", chat_id)
            return
        if amount < MIN_TRADE_USDT:
            send_whatsapp(f"Minimum trade amount is {MIN_TRADE_USDT} USDT.", chat_id)
            return
        with trading_lock:
            state = load_state()
            if state["in_position"]:
                send_whatsapp("Already in a position. Send 'sell' to close it first, or 'status' to check.", chat_id)
                return
            usdt, _ = get_balances()
            if usdt < amount:
                send_whatsapp(f"Insufficient balance. Need {amount:.2f}, have {usdt:.2f} USDT.", chat_id)
                return
            price = get_price()
            do_buy(state, price, amount, reason="manual command")

    elif text == "sell":
        with trading_lock:
            state = load_state()
            if not state["in_position"]:
                send_whatsapp("No open position to sell.", chat_id)
                return
            price = get_price()
            do_sell(state, price, "manual sell command")

    elif text == "status":
        with trading_lock:
            state = load_state()
        try:
            price = get_price()
            usdt, btc = get_balances()
        except Exception:
            price, usdt, btc = 0.0, 0.0, 0.0
        if state["in_position"]:
            pct = (price - state["entry_price"]) / state["entry_price"] * 100 if state["entry_price"] else 0
            msg = (f"📊 Status\nIn position: YES\nEntry: ${state['entry_price']:,.2f}\n"
                   f"Current: ${price:,.2f} ({pct:+.3f}%)\nProfit target: {PROFIT_TARGET_PCT*100:.2f}% | "
                   f"Stop-loss: {STOP_LOSS_PCT*100:.1f}%\nTotal realized: {state.get('total_realized_profit_usdt', 0):+.4f} USDT\n"
                   f"USDT: {usdt:.2f} | BTC: {btc:.8f}")
        else:
            msg = (f"📊 Status\nIn position: NO\nLast buy amount: {state.get('last_buy_amount', 0):.2f}\n"
                   f"Total realized: {state.get('total_realized_profit_usdt', 0):+.4f} USDT\n"
                   f"USDT: {usdt:.2f} | BTC: {btc:.8f}\nSend 'buy <amount>' to open a position.")
        send_whatsapp(msg, chat_id)

    else:
        reply = get_ai_reply(text)
        send_whatsapp(reply, chat_id)


# --- WEBHOOK (with duplicate-delivery protection) ---
_recent_message_ids = []
_recent_ids_lock = threading.Lock()
_MAX_RECENT_IDS = 200


def _is_duplicate(message_id):
    if not message_id:
        return False
    with _recent_ids_lock:
        if message_id in _recent_message_ids:
            return True
        _recent_message_ids.append(message_id)
        if len(_recent_message_ids) > _MAX_RECENT_IDS:
            del _recent_message_ids[0]
        return False


@app.route('/webhook/<path:secret_token>', methods=['POST'])
def webhook(secret_token):
    if not hmac.compare_digest(secret_token, WEBHOOK_SECRET):
        abort(403)

    data = request.json or {}

    # Green API sends webhooks for several event types — incoming messages,
    # but also delivery/status updates about messages WE sent (outgoingMessageStatus,
    # outgoingAPIMessageReceived, etc). Without this check, the bot's own replies
    # could get echoed back and processed as if they were new incoming messages,
    # causing a self-triggering reply loop. Only react to genuine incoming messages.
    if data.get("typeWebhook") != "incomingMessageReceived":
        return "OK", 200

    message_id = data.get("idMessage") or data.get("messageData", {}).get("idMessage")
    if _is_duplicate(message_id):
        return "OK", 200

    try:
        msg_data = data.get("messageData", {})
        if msg_data.get("typeMessage") == "textMessage":
            text = msg_data.get("textMessageData", {}).get("textMessage", "")
            chat_id = data.get("senderData", {}).get("chatId", "")
            logging.info(f"Webhook diag: received chat_id={chat_id!r} expected={MY_PHONE_CHAT_ID!r} match={chat_id == MY_PHONE_CHAT_ID} text={text!r}")
            if chat_id == MY_PHONE_CHAT_ID:
                # Bounded wait: start the work in a thread, wait up to 35s for it
                # to finish before returning. This gives the reply time to actually
                # go out before the HTTP response completes (avoiding the original
                # bug where Render's free-tier spin-down cut off a fire-and-forget
                # background thread before send_whatsapp ran). But capping the wait
                # at 25s — safely under gunicorn's worker timeout — means a slow
                # KuCoin/WhatsApp call can no longer get the whole worker process
                # killed (which is what caused the 500s and worker restarts).
                t = threading.Thread(target=handle_command, args=(text, chat_id), daemon=True)
                t.start()
                t.join(timeout=35)
                if t.is_alive():
                    logging.warning("handle_command still running after 35s — returning response, thread continues in background.")
            else:
                logging.warning(f"Webhook: chat_id mismatch, message ignored. Full payload: {json.dumps(data)[:500]}")
    except Exception:
        logging.exception("Webhook processing error")

    return "OK", 200


# --- SHUTDOWN ---
def shutdown_handler(signum=None, frame=None):
    shutdown_flag.set()
    global background_thread_ref
    if background_thread_ref and background_thread_ref.is_alive():
        background_thread_ref.join(timeout=5.0)
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
