@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Received data:", data)
    
    try:
        # Green API / Generic Gateway format එකට අනුව මැසේජ් එක අල්ලාගැනීම
        if "messageData" in data:
            msg_data = data["messageData"]
            if msg_data.get("typeMessage") == "textMessage":
                msg_body = msg_data.get("textMessageData", {}).get("textMessage", "")
                
                # senderData එකෙන් නම්බර් එක ලබා ගැනීම
                sender_data = data.get("senderData", {})
                chat_id = sender_data.get("chatId", "")
                
                # chatId එකෙන් නම්බර් එක පමණක් වෙන් කර ගැනීම (උදා: 94753227140@c.us -> 94753227140)
                from_number = chat_id.split("@")[0] if "@" in chat_id else chat_id
                
                print(f"Message from {from_number}: {msg_body}")
                
                # ආපහු රිප්ලයි යැවීම
                send_whatsapp_message(from_number, f"Bot reply: {msg_body}")
                
    except Exception as e:
        print(f"Error parsing message: {e}")
        
    return "OK", 200
