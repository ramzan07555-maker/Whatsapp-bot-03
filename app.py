import os
import requests
from flask import Flask, request
import hmac
import hashlib
import base64
import time
from groq import Groq

app = Flask(__name__)

# API Keys & Tokens
API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_PASSPHRASE")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ID_INSTANCE = "710722695539"
API_TOKEN = "5dcefdf92a5d46b69f4cd24d720a00fa5430a653a7be4d3687"
MY_PHONE_CHAT_ID = "966572686730@c.us"

# Initialize Groq Client
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def send_whatsapp_message(chat_id, message_text):
    try:
        url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        payload = {"chatId": chat_id, "message": message_text}
        return requests.post(url, json=payload).json()
    except Exception as e:
        print(f"Error: {e}")
        return None

def get_kucoin_signature(endpoint, method, body=""):
    try:
        header_timestamp = str(int(time.time() * 1000))
        str_to_sign = header_timestamp + method.upper() + endpoint + body
        signature = base64.b64encode(hmac.new(API_SECRET.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        passphrase_encoded = base64.b64encode(hmac.new(API_SECRET.encode('utf-8'), API_PASSPHRASE.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        
        headers = {
            'KC-API-KEY': API_KEY,
            'KC-API-SIGN': signature,
            'KC-API-TIMESTAMP': header_timestamp,
            'KC-API-PASSPHRASE': passphrase_encoded,
            'KC-API-KEY-VERSION': '2',
            'Content-Type': 'application/json'
        }
        return headers
    except Exception as e:
        print(f"Signature Error: {e}")
        return None

def execute_smart_trade():
    """මාකට් මිල සහ ගිණුමේ ශේෂය ලබා ගැනීම"""
    try:
        price_url = "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT"
        price_res = requests.get(price_url).json()
        current_price = float(price_res.get("data", {}).get("price", 0))
        
        endpoint = "/api/v1/accounts"
        headers = get_kucoin_signature(endpoint, "GET")
        if not headers:
            return "API Signature Error."
            
        response = requests.get(f"https://api.kucoin.com{endpoint}?type=trade", headers=headers)
        data = response.json()
        
        usdt_balance = 0.0
        btc_balance = 0.0
        
        if data.get("code") == "200000":
            for acc in data.get("data", []):
                if acc.get("currency") == "USDT":
                    usdt_balance = float(acc.get("available", 0))
                elif acc.get("currency") == "BTC":
                    btc_balance = float(acc.get("available", 0))
        
        report = (
            f"🤖 *Smart Auto-Trading Bot Status*:\n\n"
            f"📈 Current BTC Price: ${current_price}\n"
            f"💵 Available USDT: {usdt_balance}\n"
            f"🪙 Available BTC: {btc_balance}\n\n"
            f"⚙️ *Risk Management*: Active (Loss Protection Enabled)\n"
            f"🛡️ Bot is monitoring the market 24/7 for safe entries."
        )
        return report, current_price, usdt_balance, btc_balance
    except Exception as e:
        return f"Trading Error: {str(e)}", 0, 0, 0

def ask_groq_ai(prompt, market_data):
    """Groq AI හරහා ස්මාර්ට් පිළිතුරු සකස් කිරීම"""
    if not groq_client:
        return "Groq AI is not configured."
    
    system_prompt = (
        f"You are an expert crypto trading assistant. Current market and account data: {market_data}. "
        "Answer the user's question intelligently, professionally, and concisely in Sinhala or English depending on user input."
    )
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.7,
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        return f"AI Error: {str(e)}"

@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    try:
        if "messageData" in data:
            msg_data = data["messageData"]
            if msg_data.get("typeMessage") == "textMessage":
                msg_body = msg_data.get("textMessageData", {}).get("textMessage", "").strip()
                msg_lower = msg_body.lower()
                chat_id = data.get("senderData", {}).get("chatId", "")
                
                if chat_id != MY_PHONE_CHAT_ID:
                    return "OK", 200
                if "smart auto-trading bot status" in msg_lower or "ai error" in msg_lower:
                    return "OK", 200
                
                # දත්ත ලබා ගැනීම
                status_report, price, usdt, btc = execute_smart_trade()
                market_info = f"BTC Price: ${price}, USDT Balance: {usdt}, BTC Balance: {btc}"
                
                if "status" in msg_lower or "balance" in msg_lower or "trade" in msg_lower:
                    reply_text = status_report
                else:
                    # වෙනත් ඕනෑම ප්‍රශ්නයකට Groq AI එකෙන් ස්මාර්ට් පිළිතුරක් ලබා දීම
                    reply_text = ask_groq_ai(msg_body, market_info)
                
                send_whatsapp_message(chat_id, reply_text)
    except Exception as e:
        print(f"Error: {e}")
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
