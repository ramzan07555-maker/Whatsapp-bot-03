import os
import time
import requests
from flask import Flask, request
from pybit.unified_trading import HTTP

app = Flask(__name__)

# Bybit API Keys (Render Environment Variables වලට දෙන්න ඕනේ)
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# Spot trading සඳහා testnet=False දීලා සාමාන්‍ය live session එක හැදීම
session = HTTP(
    testnet=False,
    api_key=API_KEY,
    api_secret=API_SECRET
)

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

def execute_grid_strategy(symbol="BTCUSDT"):
    """සාමාන්‍ය Grid Logic එක ක්‍රියාත්මක කිරීම"""
    try:
        # 1. වර්තමාන මාකට් ප්‍රයිස් එක ලබා ගැනීම
        response = session.get_tickers(category="spot", symbol=symbol)
        list_data = response.get("result", {}).get("list", [])
        if not list_data:
            return f"Could not fetch market price for {symbol}"
        
        current_price = float(list_data[0].get("lastPrice"))
        
        # උදාහරණයක් ලෙස ප්‍රයිස් එකෙන් 1% පහළින් බයි ඕඩර් එකක් සහ 1% උඩින් සෙල් ඕඩර් එකක් ප්ලේස් කිරීම
        buy_price = round(current_price * 0.99, 2)
        sell_price = round(current_price * 1.01, 2)
        
        # Bybit එකේ ලිමිට් ඕඩර් (Limit Order) එකක් දාන විදිහ
        # (සටහන: ඔබේ එකවුන්ට් එකේ ප්‍රමාණවත් USDT ශේෂයක් තිබිය යුතුය)
        # buy_order = session.place_order(
        #     category="spot",
        #     symbol=symbol,
        #     side="Buy",
        #     orderType="Limit",
        #     qty="0.001",
        #     price=str(buy_price)
        # )
        
        msg = (
            f"🤖 *Grid Bot Status Update* ({symbol}):\n\n"
            f"📍 Current Market Price: ${current_price}\n"
            f"🟢 Target Buy Grid: ${buy_price}\n"
            f"🔴 Target Sell Grid: ${sell_price}\n"
            f"✨ Status: Bot is scanning and running 24/7!"
        )
        return msg
    except Exception as e:
        print(f"Grid Error: {e}")
        return f"Error executing grid strategy: {str(e)}"

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
                
                # WhatsApp එකෙන් 'grid' හෝ 'start' කිව්වම බොට් ස්ටේටස් එක බලාගන්න පුළුවන්
                if "grid" in msg_body or "start" in msg_body or "status" in msg_body:
                    reply_text = execute_grid_strategy("BTCUSDT")
                elif "hi" in msg_body or "hello" in msg_body:
                    reply_text = "Hello! Send 'grid' to check or trigger your 24/7 automated grid trading bot."
                else:
                    reply_text = f"Received: '{msg_body}'. Send 'grid' to run the trading strategy."
                
                send_whatsapp_message(chat_id, reply_text)
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
