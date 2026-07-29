import os
import requests
from flask import Flask, request
import hmac
import hashlib
import base64
import time

app = Flask(__name__)

# Render Environment Variables වලින් KuCoin විස්තර ලබා ගැනීම
API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_PASSPHRASE")

ID_INSTANCE = "710722695539"
API_TOKEN = "5dcefdf92a5d46b69f4cd24d720a00fa5430a653a7be4d3687"
MY_PHONE_CHAT_ID = "966572686730@c.us"

def send_whatsapp_message(chat_id, message_text):
    """Green API හරහා WhatsApp මැසේජ් යැවීම"""
    try:
        url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        payload = {
            "chatId": chat_id,
            "message": message_text
        }
        response = requests.post(url, json=payload)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

def get_kucoin_signature(endpoint, method, body=""):
    """KuCoin Signed API ඉල්ලීම් සඳහා Signature එක සකස් කිරීම"""
    try:
        header_timestamp = str(int(time.time() * 1000))
        str_to_sign = header_timestamp + method + endpoint + body
        
        signature = base64.b64encode(
            hmac.new(API_SECRET.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')
        
        passphrase_encoded = base64.b64encode(
            hmac.new(API_SECRET.encode('utf-8'), API_PASSPHRASE.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')
        
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

def get_account_status():
    """ගිණුමේ බැලන්ස් සහ ට්‍රේඩින් තත්ත්වය පරීක්ෂා කිරීම"""
    try:
        endpoint = "/api/v1/accounts"
        headers = get_kucoin_signature(endpoint, "GET")
        if not headers:
            return "Error generating API signature."
            
        url = f"https://api.kucoin.com{endpoint}?type=trade"
        response = requests.get(url, headers=headers)
        data = response.json()
        
        # ලයිව් මාකට් මිල ලබා ගැනීම
        price_url = "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT"
        price_res = requests.get(price_url).json()
        current_btc_price = price_res.get("data", {}).get("price", "N/A")
        
        if data.get("code") == "200000":
            accounts = data.get("data", [])
            msg = (
                f"🤖 *KuCoin Autonomous Bot Status*:\n\n"
                f"📈 Current BTC Market Price: ${current_btc_price}\n"
                f"⚙️ Bot Mode: 24/7 Active & Monitoring\n\n"
                f"💰 *Account Balances (Trade Account)*:\n"
            )
            has_funds = False
            for acc in accounts:
                balance = float(acc.get("balance", 0))
                if balance > 0:
                    has_funds = True
                    currency = acc.get("currency")
                    msg += f"• *{currency}*: {balance} (Available: {acc.get('available')})\n"
            
            if not has_funds:
                msg += "No active funds found in trade account."
            return msg
        else:
            return f"KuCoin API Error: {data.get('msg', 'Unknown error')}"
    except Exception as e:
        print(f"Status Error: {e}")
        return f"Error fetching status: {str(e)}"

@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Received data:", data)
    
    try:
        if "messageData" in data:
            msg_data = data["messageData"]
            if msg_data.get("typeMessage") == "textMessage":
                msg_body = msg_data.get("textMessageData", {}).get("textMessage", "").strip().lower()
                
                sender_data = data.get("senderData", {})
                chat_id = sender_data.get("chatId", "")
                
                print(f"Message from {chat_id}: {msg_body}")
                
                # වෙනත් අංක වලින් එන මැසේජ් නොසලකා හැරීම
                if chat_id != MY_PHONE_CHAT_ID:
                    return "OK", 200

                # බොට් යවන මැසේජ් වලට නැවත රිප්ලයි වීම වැළැක්වීම
                if "kucoin autonomous bot status" in msg_body:
                    return "OK", 200

                # ඔයා WhatsApp එකෙන් යවන කමාන්ඩ් එකට අනුව ප්‍රතිචාර දැක්වීම
                if "status" in msg_body or "balance" in msg_body or "profit" in msg_body or "pnl" in msg_body or "grid" in msg_body:
                    reply_text = get_account_status()
                elif "hi" in msg_body or "hello" in msg_body:
                    reply_text = "Bot is running 24/7! Send 'status' to check your portfolio, balances, and market price."
                else:
                    reply_text = f"Received: '{msg_body}'. Send 'status' to check your active trading and account details."
                
                send_whatsapp_message(chat_id, reply_text)
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
