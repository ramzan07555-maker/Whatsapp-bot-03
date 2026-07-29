import os
import requests
from flask import Flask, request

app = Flask(__name__)

ID_INSTANCE = os.getenv("PHONE_NUMBER_ID", "710722695539")
API_TOKEN = os.getenv("WHATSAPP_TOKEN", "5dcefdf92a5d46b69f4cd24d720a00fa5430a653a7be4d3687")

def send_whatsapp_message(chat_id, message_text):
    """Green API හරහා නිවැරදිව මැසේජ් යැවීම"""
    try:
        # Green API නිවැරදි Endpoints ෆෝමැට් එක
        url = f"https://7107.api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        
        payload = {
            "chatId": chat_id,
            "message": message_text
        }
        
        response = requests.post(url, json=payload)
        print("Send response status:", response.status_code)
        print("Send response text:", response.text)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Received data:", data)
    
    try:
        if "messageData" in data:
            msg_data = data["messageData"]
            if msg_data.get("typeMessage") == "textMessage":
                msg_body = msg_data.get("textMessageData", {}).get("textMessage", "")
                
                sender_data = data.get("senderData", {})
                chat_id = sender_data.get("chatId", "")
                
                print(f"Message from {chat_id}: {msg_body}")
                
                send_whatsapp_message(chat_id, f"Bot reply: {msg_body}")
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
