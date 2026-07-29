import os
import requests
from flask import Flask, request
from pybit.unified_trading import HTTP

app = Flask(__name__)

# Render Environment Variables වලින් API Key සහ Secret ලබා ගැනීම
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# Bybit HTTP session එක සැකසීම (Live trading සඳහා testnet=False)
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

def get_bybit_grid_status(symbol="BTCUSDT"):
    """Bybit API එක හරහා ලයිව් මාකට් ප්‍රයිස් එක ලබාගෙන ග්‍රිඩ් තත්ත්වය පරීක්ෂා කිරීම"""
    try:
        response = session.get_tickers(
            category="spot",
            symbol=symbol
        )
        list_data = response.get("result", {}).get("list", [])
        if list_data:
            current_price = float(list_data[0].get("lastPrice"))
            buy_price = round(current_price * 0.99, 2)
            sell_price = round(current_price * 1.01, 2)
            
            msg = (
                f"🤖 *Bybit Grid Bot Status* ({symbol}):\n\n"
                f"📍 Current Market Price: ${current_price}\n"
                f"🟢 Lower Grid (Buy Zone): ${buy_price}\n"
                f"🔴 Upper Grid (Sell Zone): ${sell_price}\n"
                f"✨ Status: Connected to Bybit successfully!"
            )
            return msg
        else:
            return f"Could not fetch market price for {symbol} from Bybit."
    except Exception as e:
        print(f"Bybit API Error: {e}")
        return f"Bybit Error: {str(e)}"

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
                if "bybit grid bot status" in msg_body:
                    return "OK", 200

                if "grid" in msg_body or "start" in msg_body or "btc" in msg_body:
                    reply_text = get_bybit_grid_status("BTCUSDT")
                elif "hi" in msg_body or "hello" in msg_body:
                    reply_text = "Hello! Send 'grid' to check your Bybit automated trading status."
                else:
                    reply_text = f"Received: '{msg_body}'. Send 'grid' to view the Bybit strategy."
                
                send_whatsapp_message(chat_id, reply_text)
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
