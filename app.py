import os
from flask import Flask, request
from groq import Groq
import requests

app = Flask(__name__)

# Groq API සෙටප් කිරීම (GEMINI_API_KEY වේරියබල් එකටම Groq කී එක දෙන්න පුළුවන්)
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

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "WhatsApp Groq AI Bot is Running 24/7!"

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = request.json
        print("--- Incoming Webhook Data ---")
        
        if data.get("typeWebhook") == "incomingMessageReceived":
            sender = data.get("senderData", {}).get("chatId", "")
            message_data = data.get("messageData", {})
            
            message_text = ""
            if message_data.get("typeMessage") == "textMessage":
                message_text = message_data.get("textMessageData", {}).get("textMessage", "")
            elif message_data.get("typeMessage") == "extendedTextMessage":
                message_text = message_data.get("extendedTextMessageData", {}).get("text", "")

            print(f"Sender: {sender}, Message: {message_text}")

            if sender and message_text:
                ai_reply = "සමාවෙන්න, මට දැන් AI එකට සම්බන්ධ වෙන්න බැහැ."
                if client:
                    try:
                        print("Sending request to Groq...")
                        # ලෝකයේ වේගවත්ම සහ නොමිලේ දෙන Llama 3 මාදිලිය භාවිතා කිරීම
                        completion = client.chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=[
                                {"role": "user", "content": message_text}
                            ],
                            temperature=0.7,
                        )
                        ai_reply = completion.choices[0].message.content
                        print(f"Groq Reply: {ai_reply}")
                    except Exception as e:
                        ai_reply = f"දෝෂයක් සිදු විය: {str(e)}"
                        print(f"Groq Error: {e}")

                # WhatsApp වෙත Green API හරහා පිළිතුර යැවීම
                url = f"https://api.green-api.com/waInstance{GREEN_API_ID_INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
                payload = {
                    "chatId": sender,
                    "message": ai_reply
                }
                headers = {'Content-Type': 'application/json'}
                print(f"Sending response to Green API for chatId: {sender}")
                resp = requests.post(url, json=payload, headers=headers)
                print(f"Green API Send Response: {resp.status_code} - {resp.text}")

        return "OK", 200
    except Exception as e:
        print(f"Webhook Main Error: {e}")
        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
