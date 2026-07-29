import os
from flask import Flask, request
from groq import Groq
import requests
import hmac
import hashlib
import time
from urllib.parse import urlencode

app = Flask(__name__)

# Groq API සෙටප් කිරීම
GROQ_API_KEY = os.environ.get("GEMINI_API_KEY")
client = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        print("Groq Client Initialized Successfully")
    except Exception as e:
        print(f"Groq Init Error: {e}")

GREEN_API_ID_INSTANCE = os.environ.get("GREEN_API_ID_INSTANCE")
GREEN_API_TOKEN = os.environ.get("GREEN_API_TOKEN")

# Binance Real Account API විස්තර සහ URL එක
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_BASE_URL = "https://api.binance.com"

# ඔබේ WhatsApp අංකය
MY_WHATSAPP_NUMBER = "966572686730"

def get_binance_signature(query_string):
    return hmac.new(
        BINANCE_SECRET_KEY.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def get_binance_account_balance():
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return "Binance API Keys සර්වර් එකේ සෙට් කරලා නැහැ!"
    
    try:
        url = f"{BINANCE_BASE_URL}/api/v3/account"
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp}
        query_string = urlencode(params)
        signature = get_binance_signature(query_string)
        
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.get(f"{url}?{query_string}&signature={signature}", headers=headers)
        data = response.json()
        
        if "balances" in data:
            non_zero = [b for b in data["balances"] if float(b["free"]) > 0 or float(b["locked"]) > 0]
            result_str = "📊 ඔබේ Binance Real ගිණුමේ ශේෂය:\n"
            for b in non_zero:
                result_str += f"- {b['asset']}: නිදහස්: {b['free']}, ලොක් වී ඇති: {b['locked']}\n"
            return result_str
        else:
            return f"Balance දත්ත ලබාගැනීමේ දෝෂයක්: {data}"
    except Exception as e:
        return f"Binance Error: {str(e)}"

def execute_auto_trade():
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        return "ට්‍රේඩ් කිරීමට Binance API Keys අවශ්‍ය වේ!"
    
    try:
        url = f"{BINANCE_BASE_URL}/api/v3/order"
        timestamp = int(time.time() * 1000)
        
        # Spot market එකේ අවම ප්‍රමාණයකට (ഉദാ: BTCUSDT) ට්‍රේඩ් එකක් සැකසීම
        params = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.0001",  # කුඩා ප්‍රමාණයක් සඳහා
            "timestamp": timestamp
        }
        query_string = urlencode(params)
        signature = get_binance_signature(query_string)
        
        headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
        response = requests.post(f"{url}?{query_string}&signature={signature}", headers=headers)
        data = response.json()
        
        if "orderId" in data:
            return f"🚀 සාර්ථකයි! ලයිව් මාකට් එකේ BTC ට්‍රේඩ් එකක් (BUY) ක්‍රියාත්මක විය. Order ID: {data['orderId']}"
        else:
            return f"⚠️ ට්‍රේඩ් කිරීමේදී දෝෂයක් ඇති විය: {data.get('msg', data)}"
    except Exception as e:
        return f"Trade Execution Error: {str(e)}"

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "Private Real Trading Bot is Running 24/7!"

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.json
        print("--- Incoming Webhook Data ---")
        
        if data.get("typeWebhook") == "incomingMessageReceived":
            sender = data.get("senderData", {}).get("chatId", "")
            
            # වෙනත් අංක වලින් එන මැසේජ් මඟ හරියි (ඔයාගේ අංකයෙන් එන ඒවාට පමණක් ක්‍රියා කරයි)
            if MY_WHATSAPP_NUMBER not in sender:
                print(f"Ignored message from unauthorized user: {sender}")
                return "OK", 200

            message_data = data.get("messageData", {})
            message_text = ""
            if message_data.get("typeMessage") == "textMessage":
                message_text = message_data.get("textMessageData", {}).get("textMessage", "")
            elif message_data.get("typeMessage") == "extendedTextMessage":
                message_text = message_data.get("extendedTextMessageData", {}).get("text", "")

            print(f"Sender (Authorized): {sender}, Message: {message_text}")

            if sender and message_text:
                ai_reply = "සමාවෙන්න, මට දැන් උත්තර දෙන්න බැහැ."
                text_lower = message_text.lower()
                
                if "start trading" in text_lower or "ට්‍රේඩ් කරන්න" in text_lower or "trade" in text_lower:
                    ai_reply = execute_auto_trade()
                elif "profit" in text_lower or "ප්‍රොෆිට්" in text_lower or "balance" in text_lower or "ලාභය" in text_lower or "salli" in text_lower:
                    ai_reply = get_binance_account_balance()
                else:
                    if client:
                        try:
                            system_prompt = "You are an expert automated crypto trading bot manager. Answer user queries clearly."
                            completion = client.chat.completions.create(
                                model="llama-3.3-70b-versatile",
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": message_text}
                                ],
                                temperature=0.7,
                            )
                            ai_reply = completion.choices[0].message.content
                        except Exception as e:
                            ai_reply = f"දෝෂයක් සිදු විය: {str(e)}"

                url = f"https://api.green-api.com/waInstance{GREEN_API_ID_INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
                payload = {
                    "chatId": sender,
                    "message": ai_reply
                }
                headers = {'Content-Type': 'application/json'}
                resp = requests.post(url, json=payload, headers=headers)
                print(f"Green API Send Response: {resp.status_code} - {resp.text}")

        return "OK", 200
    except Exception as e:
        print(f"Webhook Main Error: {e}")
        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
