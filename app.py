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
