import time
import requests
from bs4 import BeautifulSoup
import datetime
import platform
import os
import json
from twilio.rest import Client
import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
TO_PHONE_NUMBER = os.getenv("TO_PHONE_NUMBER")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 120
NOTIFIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified.json")


def load_selectors(filename="selectors.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_products(filename="products.json"):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        print(f"❌ Brak pliku {filename}")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_target_price_map(products):
    pid_to_price = {}
    grouped = {}
    for p in products:
        pid = p.get("product_id")
        if not pid:
            continue
        grouped.setdefault(pid, []).append(p)

    for pid, group in grouped.items():
        for p in group:
            if p.get("target_price") is not None:
                pid_to_price[pid] = p["target_price"]
                break

    return pid_to_price


SELECTORS = load_selectors()
PRODUCTS = load_products()

print(f"✅ Załadowano {len(PRODUCTS)} produktów:")
for p in PRODUCTS:
    print(f"- {p['name']} ({p['url']})")


def parse_price(price_str):
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", price_str)
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def load_notified():
    if not os.path.exists(NOTIFIED_FILE):
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}
    try:
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}


def save_notified(data):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_price(soup, store):
    selector = SELECTORS.get(store, {}).get("price", "")
    if not selector or selector.startswith("xpath="):
        return "Brak ceny (tylko dla Playwright)"
    price_tag = soup.select_one(selector)
    return price_tag.get_text(strip=True) if price_tag else "Brak ceny"


def is_available(url, store, max_retries=3, retry_delay=5):
    headers = {"User-Agent": "Mozilla/5.0"}
    use_playwright = SELECTORS.get(store, {}).get("use_selenium", False)

    attempt = 0
    while attempt < max_retries:
        try:
            price = "Brak ceny"

            if use_playwright:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.set_extra_http_headers({
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36"
                    })
                    page.goto(url, wait_until="networkidle")
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    price = get_price(soup, store)

                    availability_selector = SELECTORS.get(store, {}).get("availability", "")
                    unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")

                    def try_selector(selector):
                        if selector.startswith("xpath="):
                            xpath = selector.replace("xpath=", "")
                            el = page.locator(f"xpath={xpath}")
                        else:
                            el = page.locator(selector)
                        return el.count() > 0 and el.first.is_visible()

                    if unavailability_selector and try_selector(unavailability_selector):
                        return False, price
                    if availability_selector and try_selector(availability_selector):
                        return True, price

                    return False, price
            else:
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                price = get_price(soup, store)

                availability_selector = SELECTORS.get(store, {}).get("availability", "")
                unavailability_selector = SELECTORS.get(store, {}).get("unavailability", "")

                if unavailability_selector:
                    el = soup.select_one(unavailability_selector)
                    if el and any(w in el.get_text(strip=True).lower() for w in ["brak", "wyprzedany", "niedostępny"]):
                        return False, price
                if availability_selector:
                    el = soup.select_one(availability_selector)
                    if el:
                        return True, price

                return False, price

        except Exception as e:
            print(f"[{timestamp()}] ⚠️ Błąd przy sprawdzaniu {url} (próba {attempt + 1}/{max_retries}): {e}")
            attempt += 1
            if attempt < max_retries:
                print(f"[{timestamp()}] ⏳ Próba ponownego sprawdzenia za {retry_delay} sekund...")
                time.sleep(retry_delay)
            else:
                print(f"[{timestamp()}] ❌ Maksymalna liczba prób wyczerpana dla {url}")
                return False, "Brak ceny"


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID nieustawione")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, data=payload)
        if response.status_code == 200:
            print("✅ Telegram wysłany")
        else:
            print(f"❌ Błąd Telegram: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Wyjątek Telegram: {e}")


def send_to_discord(message):
    data = {"content": message}
    try:
        response = requests.post(WEBHOOK_URL, json=data)
        if response.status_code in [200, 204]:
            print("✅ Wiadomość wysłana na Discorda.")
        else:
            print(f"❌ Błąd Discord: {response.status_code} {response.text}")
    except Exception as e:
        print(f"❌ Błąd Discord: {e}")


def send_sms(message):
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        sms = client.messages.create(body=message, from_=TWILIO_FROM_NUMBER, to=TO_PHONE_NUMBER)
        print(f"📱 SMS wysłany! SID: {sms.sid}")
    except Exception as e:
        print(f"❌ Błąd SMS: {e}")


def notify_available(product, price):
    print(f"[{timestamp()}] ✅ {product['name']} dostępny! Cena: {price}")
    discord_message = f"@everyone ✅ Produkt **{product['name']}** dostępny za **{price}**!\n🔗 {product['url']}"
    sms_message = f"{product['name']} za {price}. Link: {product['url']}"
    send_to_discord(discord_message)
    send_telegram(sms_message)
    play_sound()


def notify_unavailable(product):
    print(f"[{timestamp()}] ❌ {product['name']} niedostępny.")


def notify_price_change(product, old_price, new_price):
    print(f"[{timestamp()}] 💸 Cena spadła dla {product['name']}! {old_price} → {new_price}")
    msg = (
        f"@everyone 💸 Cena SPADŁA dla **{product['name']}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n"
        f"{product['url']}"
    )
    send_to_discord(msg)
    send_telegram(msg)


def notify_price_increase(product, old_price, new_price):
    target_price = product.get("target_price")
    new_val = parse_price(new_price)
    if target_price is not None and new_val is not None and new_val > target_price:
        return  # Nie wysyłaj powiadomienia, jeśli cena przekracza target

    print(f"[{timestamp()}] 🔺 Cena wzrosła dla {product['name']}! {old_price} → {new_price}")
    msg = (
        f"@everyone 🔺 Cena WZROSŁA dla **{product['name']}**!\n"
        f"Stara cena: {old_price}\nNowa cena: {new_price}\n"
        f"{product['url']}"
    )
    send_to_discord(msg)
    send_telegram(msg)



def play_sound():
    system = platform.system()
    if system == "Windows":
        import winsound
        winsound.Beep(1000, 500)
    elif system == "Darwin":
        os.system("afplay /System/Library/Sounds/Ping.aiff")
    else:
        os.system("echo -e '\a'")


def timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_price_history(product, old_price, new_price):
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.csv")
    first_write = not os.path.exists(log_file)
    with open(log_file, "a", encoding="utf-8") as f:
        if first_write:
            f.write("timestamp,product_name,old_price,new_price,url\n")
        f.write(f"{timestamp()},{product['name']},{old_price},{new_price},{product['url']}\n")


def check_product(product, notified, group_target_price=None):
    try:
        store = product.get("store", "unknown")
        name = product["name"]

        if store not in notified:
            notified[store] = {}

        available, price = is_available(product["url"], store)
        previous_entry = notified[store].get(name, {})
        last_state = previous_entry.get("available")
        old_price = previous_entry.get("price")

        current_price_value = parse_price(price)
        target_price = group_target_price or product.get("target_price")

        if available and last_state != True:
            notified[store][name] = {"available": True, "price": price, "timestamp": timestamp()}
            if target_price is None or (current_price_value is not None and current_price_value <= target_price):
                notify_available(product, price)
            if old_price and price != old_price:
                old_val = parse_price(old_price)
                new_val = current_price_value
                if old_val is not None and new_val is not None:
                    log_price_history(product, old_price, price)

        elif not available and last_state != False:
            notify_unavailable(product)
            notified[store][name] = {"available": False, "price": price, "timestamp": timestamp()}

        elif available and price and old_price and price != old_price:
            old_val = parse_price(old_price)
            new_val = current_price_value
            if old_val is not None and new_val is not None:
                if new_val < old_val:
                    notify_price_change(product, old_price, price)
                elif new_val > old_val:
                    notify_price_increase(product, old_price, price)
                log_price_history(product, old_price, price)
                notified[store][name]["price"] = price
                notified[store][name]["timestamp"] = timestamp()

    except Exception as e:
        print(f"[{timestamp()}] ⚠️ Błąd przy {product['name']}: {e}")


def main():

    notified = load_notified()

    with ThreadPoolExecutor(max_workers=5) as executor:

        futures = [executor.submit(check_product, p, notified) for p in PRODUCTS]

        for future in as_completed(futures):

            pass 

    save_notified(notified)

    print(f"[{timestamp()}] ✅ Sprawdzenie zakończone.")



if __name__ == "__main__":

    main()
