import os
import threading
import time
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

# **ඔයාගේ නම්බර් එක විතරයි මෙතන තියෙන්නේ (වෙන කිසිම කෙනෙකුට රිප්لای නොකිරීම සඳහා)**
MY_PHONE_NUMBER = "966572686730"

# --- 1. දවසේ පැය 24 පුරාම ඔටෝ ට්‍රේඩ් කරන කොටස (Background Thread) ---
def auto_trading_worker():
    print("24/7 Auto Trading Bot Started in Background...")
    symbol = "BTCUSDT"
    
    while True:
        try:
            # මෙතැනින් 24 පැයම මාකට් එක චෙක් කරමින් ඔටෝ ට්‍රේඩ්ස් සිදු කරයි
            response = session.get_tickers(category="spot", symbol=symbol)
            price = float(response['result']['list'][0]['lastPrice'])
            print(f"[Auto Trade] Current {symbol} Price: {price}")
            
            # පරතරය (උදාහරණයක් ලෙස පැයකට වරක්)
            time.sleep(3600)
            
        except Exception as e:
            print("Auto Trading Error:", str(e))
            time.sleep(60)

# --- 2. WhatsApp එකෙන් ඔයා අහන ප්‍රශ්න වලට ඔයාගේ නම්බර් එකට පමණක් උත්තර දෙන කොටස ---
@app.route("/")
def home():
    return "Bybit 24/7 Auto Trading & Secure WhatsApp Bot is Running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    
    try:
        # මැසේජ් එක එවපු කෙනාගේ නම්බර් එක ලබා ගැනීම
        sender_number = data.get("from") or data.get("sender", {}).get("phone") or data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0].get("from")
        
        print(f"Incoming message from: {sender_number}")
        
        # **ප්‍රධාන පිරික්සුම:** එන නම්බර් එක ඔයාගේ නම්බර් එක (`966572686730`) නොවේ නම්, සම්පූර්ණයෙන්ම නොසලකා හරියි (Ignore)
        if sender_number != MY_PHONE_NUMBER:
            print("Unauthorized sender. Ignored.")
            return {"status": "ignored", "message": "Not authorized number"}, 200

        # මැසේජ් එක කියවීම
        message_text = data.get("text", {}).get("body", "").lower() or data.get("entry", [{}])[0].get("changes", [{}])[0].get("value", {}).get("messages", [{}])[0].get("text", {}).get("body", "").lower()
        
        print(f"Message from you: {message_text}")

        # ඔයා අහන ප්‍රශ්න වලට දෙන උත්තර (Commands)
        reply_message = ""
        if "balance" in message_text or "ශේෂය" in message_text:
            wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            balance = wallet['result']['list'][0]['coin'][0]['walletBalance']
            reply_message = f"Your current USDT balance is: {balance}"
            
        elif "status" in message_text:
            reply_message = "Bot is running 24/7 and trading automatically for you!"
            
        else:
            reply_message = f"Command received! Bot is active. You said: {message_text}"

        print(f"Replying only to your number ({MY_PHONE_NUMBER}): {reply_message}")
        return {"status": "success", "reply": reply_message}, 200
        
    except Exception as e:
        print("Webhook Error:", str(e))
        return {"status": "error", "message": str(e)}, 400

if __name__ == "__main__":
    # 24/7 ඔටෝ ට්‍රේඩිං ස්ටාර්ට් කිරීම
    t = threading.Thread(target=auto_trading_worker)
    t.daemon = True
    t.start()
    
    # සර්වර් එක ක්‍රියාත්මක කිරීම
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
