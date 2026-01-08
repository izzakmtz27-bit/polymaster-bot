import os
import time
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_alert(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg}
    requests.post(url, data=data)

send_alert("🟢 Polymaster is ONLINE.\nAlerts-only mode active.")

while True:
    # Placeholder for strategy logic
    time.sleep(60)
