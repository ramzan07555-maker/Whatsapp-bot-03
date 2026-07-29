import os
import threading
import time
import requests
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

MY_PHONE_NUMBER = "966572686730"

# WhatsApp Cloud API Configuration
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "YOUR_WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "YOUR_PHONE_NUMBER_ID")

def send_whatsapp_message(to_number, message_text):
    """WhatsApp එකට ආපහු මැසේජ් එකක් යවන ෆන්ෂන් එක"""
    try:
        url = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": message_text}
        }
        response = requests.post(url, json=payload, headers=headers)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

# Meta Webhook Verification (GET Method)
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    verify_token = "my_verify_token_123"
    
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode and token:
        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        else:
            return "Verification failed", 403
    return "Hello World", 200

# WhatsApp Incoming Messages (POST Method)
@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Received data:", data)
    
    try:
        if "entry" in data:
            for entry in data["entry"]:
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    if "messages" in value:
                        messages = value["messages"]
                        for message in messages:
                            from_number = message["from"]
                            msg_body = message["text"]["body"]
                            
                            print(f"Message from {from_number}: {msg_body}")
                            send_whatsapp_message(from_number, f"Bot reply: {msg_body}")
    except Exception as e:
        print(f"Error: {e}")
        
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
