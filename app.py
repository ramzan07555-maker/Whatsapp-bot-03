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

def get_kucoin_market_price(symbol="BTC-USDT"):
    """KuCoin Public API එක හරහා ලයිව් මාකට් ප්‍රයිස් එක ලබා ගැනීම (කිසිදු IP බ්ලොක් කිරීමක් නැත)"""
    try:
        url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}"
        response = requests.get(url)
        data = response.json()
        
        if data.get("code") == "200000":
            price_data = data.get("data", {})
            current_price = float(price_data.get("price"))
            
            buy_price = round(current_price * 0.99, 2)
            sell_price = round(current_price * 1.01, 2)
            
            msg = (
                f"🤖 *KuCoin Spot Grid Bot Status* ({symbol}):\n\n"
                f"📍 Current Market Price: ${current_price}\n"
                f"🟢 Lower Grid (Buy Zone): ${buy_price}\n"
                f"🔴 Upper Grid (Sell Zone): ${sell_price}\n"
                f"✨ Status: Connected to KuCoin successfully!"
            )
            return msg
        else:
            return f"Could not fetch market price for {symbol} from KuCoin."
    except Exception as e:
        print(f"KuCoin API Error: {e}")
        return f"KuCoin Error: {str(e)}"

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
                
                # බොට් යවන මැසේජ් වලට නැවත රිප්ලයි වීම වැළැක්වීම
                if "kucoin spot grid bot status" in msg_body:
                    return "OK", 200

                if "grid" in msg_body or "start" in msg_body or "btc" in msg_body:
                    reply_text = get_kucoin_market_price("BTC-USDT")
                elif "hi" in msg_body or "hello" in msg_body:
                    reply_text = "Hello! Send 'grid' to check your KuCoin automated trading status."
                else:
                    reply_text = f"Received: '{msg_body}'. Send 'grid' to view the KuCoin strategy."
                
                send_whatsapp_message(chat_id, reply_text)
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
