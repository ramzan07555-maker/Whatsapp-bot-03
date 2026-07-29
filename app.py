import os
import threading
import time
import requests
from pybit.unified_trading import HTTP
from flask import Flask, request

# Bybit API Keys
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(
    testnet=False,
    api_key=API_KEY,
    api_secret=API_SECRET
)

app = Flask(__name__)

MY_PHONE_NUMBER = "966572686730"

# (ඔබ WhatsApp Cloud API පාවිච්චි කරන්නේ නම් මෙයට අදාළ Token සහ Phone Number ID එක මෙතැනට දෙන්න. 
# වෙනත් Gateway එකක් නම් එහි API අදාළ අයුරින් වෙනස් කරගත හැක)
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "YOUR_WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "YOUR_PHONE_NUMBER_ID")

def send_whatsapp_message(to_number, message_text):
    """WhatsApp එකට ආපහු මැසේජ් එකක් යවන ෆන්ෂන් එක"""
    try:
        url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": message_text}
        }
        response = requests.post(url, json=payload, headers=headers)
        print("WhatsApp Send Response:", response.json())
    except Exception as e:
        print("Error sending WhatsApp message:", str(e))

# --- 1. දවසේ පැය 24 පුරාම ඔටෝ ට්‍රේඩ් කරන කොටස ---
def auto_trading_worker():
    print("24/7 Auto Trading Bot Started in Background...")
    symbol = "BTCUSDT"
    
    while True:
        try:
            response = session.get_tickers(category="spot", symbol=symbol)
            price = float(response['result']['list'][0]['lastPrice'])
            print(f"[Auto Trade] Current {symbol} Price: {price}")
            time.sleep(3600)
        except Exception as e:
            print("Auto Trading Error:", str(e))
            time.sleep(60)

# --- 2. WhatsApp Webhook (පරණ 405 එරර් එක මඟහරවා ගැනීමට /webhook රූට් එක නිවැරදි කර ඇත) ---
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "GET":
        # WhatsApp Webhook Verification සඳහා (Meta එකෙන් verify token එකක් ඉල්ලුවොත්)
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode and token:
            return challenge, 200
        return "Bybit 24/7 Auto Trading & WhatsApp Bot is Running!", 200

    # WhatsApp එකෙන් POST ඉල්ලීමක් එන අවස්ථාව
    data = request.json
    try:
        # මැසේජ් එක එවපු කෙනාගේ නම්බර් එක ලබා ගැනීම
        sender_number = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0].get("from")
        
        if not sender_number:
            return {"status": "no_message"}, 200

        print(f"Incoming message from: {sender_number}")
        
        # ඔයාගේ නම්බර් එක දැයි පරීක්ෂා කරයි
        if sender_number != MY_PHONE_NUMBER:
            print("Unauthorized sender. Ignored.")
            return {"status": "ignored"}, 200

        # මැසේජ් එක කියවීම
        message_text = data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0].get("text", {}).get("body", "").lower()
        print(f"Message from you: {message_text}")

        # ප්‍රශ්න වලට දෙන උත්තර
        reply_message = ""
        if "balance" in message_text or "ශේෂය" in message_text:
            wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            balance = wallet['result']['list'][0]['coin'][0]['walletBalance']
            reply_message = f"Your current USDT balance is: {balance}"
            
        elif "status" in message_text:
            reply_message = "Bot is running 24/7 and trading automatically for you!"
            
        else:
            reply_message = f"Command received! Bot is active. You said: {message_text}"

        # WhatsApp එකට මැසේජ් එක යැවීම
        send_whatsapp_message(sender_number, reply_message)
        
        return {"status": "success"}, 200
        
    except Exception as e:
        print("Webhook Error:", str(e))
        return {"status": "error", "message": str(e)}, 400

if __name__ == "__main__":
    t = threading.Thread(target=auto_trading_worker)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
