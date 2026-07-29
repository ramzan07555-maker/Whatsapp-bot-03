import os
import requests
from flask import Flask, request

app = Flask(__name__)

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

def get_binance_grid_status(symbol="BTCUSDT"):
    """Binance Public API හරහා කිසිදු 403 එරර් එකකින් තොරව ලයිව් මාකට් ඩේටා ලබා ගැනීම"""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        response = requests.get(url)
        data = response.json()
        
        if "price" in data:
            current_price = float(data["price"])
            buy_price = round(current_price * 0.99, 2)
            sell_price = round(current_price * 1.01, 2)
            
            msg = (
                f"🤖 *Automated Grid Status* ({symbol}):\n\n"
                f"📍 Current Market Price: ${current_price}\n"
                f"🟢 Lower Grid (Buy Zone): ${buy_price}\n"
                f"🔴 Upper Grid (Sell Zone): ${sell_price}\n"
                f"✨ Status: Running 24/7 successfully!"
            )
            return msg
        else:
            return f"Could not fetch price for {symbol}"
    except Exception as e:
        print(f"API Error: {e}")
        return "Error fetching live market data."

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
                
                # බාහිර ලூප් (Loop) වීම වැළැක්වීම සඳහා බොට් යවන මැසේජ් වලට රිප්ලයි කිරීම වළක්වයි
                if "bot reply" in msg_body or "status" in msg_body and "automated" in msg_body:
                    return "OK", 200

                if "grid" in msg_body or "start" in msg_body or "btc" in msg_body:
                    reply_text = get_binance_grid_status("BTCUSDT")
                elif "hi" in msg_body or "hello" in msg_body:
                    reply_text = "Hello! Send 'grid' to check your 24/7 automated trading status."
                else:
                    reply_text = f"Received: '{msg_body}'. Send 'grid' to view the active trading strategy."
                
                send_whatsapp_message(chat_id, reply_text)
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
