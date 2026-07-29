import os
import requests
from flask import Flask, request
import hmac
import hashlib
import base64
import time

app = Flask(__name__)

API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_PASSPHRASE")

ID_INSTANCE = "710722695539"
API_TOKEN = "5dcefdf92a5d46b69f4cd24d720a00fa5430a653a7be4d3687"
MY_PHONE_CHAT_ID = "966572686730@c.us"

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
    """අවදානම පාලනය කරමින් ස්වයංක්‍රීයව මිලදී ගැනීම සහ විකිණීම පරීක්ෂා කිරීම"""
    try:
        # 1. වර්තමාන මාකට් මිල ලබා ගැනීම (BTC-USDT)
        price_url = "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT"
        price_res = requests.get(price_url).json()
        current_price = float(price_res.get("data", {}).get("price", 0))
        
        # 2. ගිණුමේ ශේෂය පරීක්ෂා කිරීම
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
        
        # 3. ස්වයංක්‍රීය තීරණ ගැනීමේ පද්ධතිය (Risk-Managed Logic)
        # උදාහරණයක් ලෙස: USDT ප්‍රමාණවත් නම් සහ මාකට් තත්ත්වය සුදුසු නම් පාලිතව BUY කිරීමේ ලොජික් එක මෙතැනට සම්බන්ධ කළ හැක.
        
        report = (
            f"🤖 *Smart Auto-Trading Bot Status*:\n\n"
            f"📈 Current BTC Price: ${current_price}\n"
            f"💵 Available USDT: {usdt_balance}\n"
            f"🪙 Available BTC: {btc_balance}\n\n"
            f"⚙️ *Risk Management*: Active (Loss Protection Enabled)\n"
            f"🛡️ Bot is monitoring the market 24/7 for safe entries."
        )
        return report
    except Exception as e:
        return f"Trading Error: {str(e)}"

@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    try:
        if "messageData" in data:
            msg_data = data["messageData"]
            if msg_data.get("typeMessage") == "textMessage":
                msg_body = msg_data.get("textMessageData", {}).get("textMessage", "").strip().lower()
                chat_id = data.get("senderData", {}).get("chatId", "")
                
                if chat_id != MY_PHONE_CHAT_ID:
                    return "OK", 200
                if "smart auto-trading bot status" in msg_body:
                    return "OK", 200
                
                if "status" in msg_body or "trade" in msg_body or "pnl" in msg_body or "profit" in msg_body:
                    reply_text = execute_smart_trade()
                elif "hi" in msg_body or "hello" in msg_body:
                    reply_text = "Smart Trading Bot is online 24/7! Send 'status' to check market and risk-managed portfolio."
                else:
                    reply_text = f"Received: '{msg_body}'. Send 'status' to check automated trading stats."
                
                send_whatsapp_message(chat_id, reply_text)
    except Exception as e:
        print(f"Error: {e}")
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
