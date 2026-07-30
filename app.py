import os
import requests
from flask import Flask, request
import hmac
import hashlib
import base64
import time
import threading
from groq import Groq

app = Flask(__name__)

# API Keys & Tokens (කලින් ඒවාමයි, කිසිවක් වෙනස් කර නැත)
API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_PASSPHRASE")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ID_INSTANCE = "710722695539"
API_TOKEN = "5dcefdf92a5d46b69f4cd24d720a00fa5430a653a7be4d3687"
MY_PHONE_CHAT_ID = "966572686730@c.us"

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def send_whatsapp_message(chat_id, message_text):
    try:
        url = f"https://api.green-api.com/waInstance{ID_INSTANCE}/sendMessage/{API_TOKEN}"
        payload = {"chatId": chat_id, "message": message_text}
        return requests.post(url, json=payload).json()
    except Exception as e:
        print(f"Error: {e}")
        return None

def get_kucoin_signature(endpoint, method, body=""):
    try:
        header_timestamp = str(int(time.time() * 1000))
        str_to_sign = header_timestamp + method.upper() + endpoint + body
        signature = base64.b64encode(hmac.new(API_SECRET.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        passphrase_encoded = base64.b64encode(hmac.new(API_SECRET.encode('utf-8'), API_PASSPHRASE.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        
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

def get_market_and_balances():
    try:
        price_url = "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=BTC-USDT"
        price_res = requests.get(price_url).json()
        current_price = float(price_res.get("data", {}).get("price", 0))
        
        endpoint = "/api/v1/accounts"
        headers = get_kucoin_signature(endpoint, "GET")
        if not headers:
            return current_price, 0.0, 0.0
            
        response = requests.get(f"https://api.kucoin.com{endpoint}?type=trade", headers=headers)
        data = response.json()
        
        usdt_balance = 0.0
        btc_balance = 0.0
        
        if data.get("code") == "200000":
            for acc in data.get("data", []):
                if acc.get("currency") == "USDT":
                    usdt_balance = float(acc.get("available", 0))
                elif acc.get("currency") == "BTC":
                    btc_balance = float(acc.get("available", 0))
                    
        return current_price, usdt_balance, btc_balance
    except Exception as e:
        print(f"Data Fetch Error: {e}")
        return 0.0, 0.0, 0.0

def place_kucoin_order(side, amount_usdt=10):
    """KuCoin එකේ සැබෑ ලෙසම Market Order එකක් (BUY හෝ SELL) ක්‍රියාත්මක කිරීම"""
    try:
        endpoint = "/api/v1/orders"
        client_oid = str(int(time.time() * 1000))
        
        # මිලදී ගැනීමේදී හෝ විකිණීමේදී අවශ්‍ය පරාමිතීන් සකස් කිරීම
        # (මෙහිදී ආරක්ෂාවට අවම ප්‍රමාණයක් හෝ පවතින ශේෂය පදනම් කරගනී)
        body_dict = {
            "clientOid": client_oid,
            "side": side.lower(), # 'buy' හෝ 'sell'
            "symbol": "BTC-USDT",
            "type": "market"
        }
        
        if side.lower() == 'buy':
            body_dict["funds"] = str(amount_usdt) # බයි කිරීමට පාවිච්චි කරන USDT ප්‍රමාණය
        else:
            # BTC විකිණීමේදී (උදාහරණයක් ලෙස සතුව ඇති මුළු ප්‍රමාණයම හෝ කොටසක්)
            _, _, btc_bal = get_market_and_balances()
            if btc_bal < 0.0001:
                return "Not enough BTC to sell."
            body_dict["size"] = str(btc_bal)

        import json
        body_str = json.dumps(body_dict)
        headers = get_kucoin_signature(endpoint, "POST", body_str)
        if not headers:
            return "Failed to generate API signature."

        response = requests.post(f"https://api.kucoin.com{endpoint}", headers=headers, data=body_str)
        res_data = response.json()
        
        if res_data.get("code") == "200000":
            return f"Success! Order ID: {res_data.get('data', {}).get('orderId')}"
        else:
            return f"Order Failed: {res_data.get('msg', 'Unknown error')}"
    except Exception as e:
        return f"Order Execution Error: {str(e)}"

def evaluate_ai_trading_decision(price, usdt, btc):
    """Groq AI හරහා අවදානම පාලනය කරමින් ස්වයංක්‍රීයව Buy/Sell තීරණ ගැනීම"""
    if not groq_client:
        return "HOLD", "Groq AI not configured."
    
    prompt = (
        f"Current BTC Price: ${price}, USDT Balance: {usdt}, BTC Balance: {btc}. "
        "You are an automated risk-managed crypto trading bot. "
        "Analyze whether to BUY, SELL, or HOLD. "
        "Rules: Minimize loss, ensure strict risk control. "
        "Respond strictly in one word first (BUY, SELL, or HOLD), followed by a short reason in Sinhala or English."
    )
    
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            temperature=0.3,
        )
        ai_response = chat_completion.choices[0].message.content.strip()
        return ai_response
    except Exception as e:
        return "HOLD", f"AI Error: {str(e)}"

def execute_auto_trade_logic():
    price, usdt, btc = get_market_and_balances()
    if price == 0.0:
        return "Failed to fetch market data."
    
    # AI තීරණය ලබා ගැනීම
    ai_decision_full = evaluate_ai_trading_decision(price, usdt, btc)
    
    # AI ප්‍රතිඵලයෙන් පළමු වචනය (BUY, SELL, HOLD) පමණක් වෙන් කර ගැනීම
    decision_word = ai_decision_full.split()[0].upper() if ai_decision_full else "HOLD"
    
    trade_result_msg = ""
    if "BUY" in decision_word and usdt >= 5:
        # USDT ප්‍රමාණවත් නම් ස්වයංක්‍රීයව BUY කිරීම (උදාහරණයක් ලෙස ඩොලර් 10 බැගින්)
        trade_result_msg = place_kucoin_order("buy", amount_usdt=10)
    elif "SELL" in decision_word and btc > 0.0001:
        # BTC ප්‍රමාණවත් නම් ස්වයංක්‍රීයව SELL කිරීම
        trade_result_msg = place_kucoin_order("sell")
    else:
        trade_result_msg = "No trade executed (HOLD or Insufficient Balance)."

    report = (
        f"🤖 *Fully Auto-Trading Bot Report*:\n\n"
        f"📈 BTC Price: ${price}\n"
        f"💵 USDT: {usdt} | 🪙 BTC: {btc}\n\n"
        f"🧠 *AI Decision*:\n{ai_decision_full}\n\n"
        f"⚡ *Execution Status*:\n{trade_result_msg}\n\n"
        f"🛡️ (24/7 Fully Automated Mode)"
    )
    return report

def background_trading_loop():
    """24 පැයම ක්‍රියාත්මක වෙමින් ස්වයංක්‍රීයව මාකට් එක නිරීක්ෂණය කරන ලූප් එක"""
    while True:
        try:
            # උදාහරණයක් ලෙස සෑම මිනිත්තු 15 කට වතාවක් ඔටෝ ට්‍රේඩ් ලොජික් එක ක්‍රියාත්මක වේ
            report = execute_auto_trade_logic()
            # අවශ්‍ය නම් ස්වයංක්‍රීයව WhatsApp වෙත ද මෙහිදී වාර්තා යැවිය හැක:
            # send_whatsapp_message(MY_PHONE_CHAT_ID, report)
        except Exception as e:
            print(f"Background Loop Error: {e}")
        
        # විනාඩි 15 ක διάστημα (Interval) එකක් ලබා දීම (තත්පර වලින්)
        time.sleep(900)

@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    try:
        if "messageData" in data:
            msg_data = data["messageData"]
            if msg_data.get("typeMessage") == "textMessage":
                msg_body = msg_data.get("textMessageData", {}).get("textMessage", "").strip()
                msg_lower = msg_body.lower()
                chat_id = data.get("senderData", {}).get("chatId", "")
                
                if chat_id != MY_PHONE_CHAT_ID:
                    return "OK", 200
                if "fully auto-trading bot report" in msg_lower:
                    return "OK", 200
                
                if "status" in msg_lower or "trade" in msg_lower or "auto" in msg_lower or "run" in msg_lower:
                    reply_text = execute_auto_trade_logic()
                else:
                    price, usdt, btc = get_market_and_balances()
                    market_info = f"BTC Price: ${price}, USDT: {usdt}, BTC: {btc}"
                    
                    system_prompt = f"You are a smart crypto trading assistant. Market data: {market_info}. Answer concisely in Sinhala or English."
                    chat_completion = groq_client.chat.completions.create(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": msg_body}
                        ],
                        model="llama-3.3-70b-versatile",
                        temperature=0.7,
                    )
                    reply_text = chat_completion.choices[0].message.content
                
                send_whatsapp_message(chat_id, reply_text)
    except Exception as e:
        print(f"Error: {e}")
    return "OK", 200

if __name__ == "__main__":
    # සර්වර් එක පටන් ගන්නා විටම 24/7 background loop එක වෙනම Thread එකක් ලෙස ආරම්භ කිරීම
    t = threading.Thread(target=background_trading_loop, daemon=True)
    t.start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
