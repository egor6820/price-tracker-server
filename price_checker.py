import requests
import time

# 👉 Адреса твого бекенду
API_URL = "https://price-tracker-api.onrender.com"

def check_all_prices():
    print("Перевірка цін...")

    # список товарів, які хочеш перевіряти
    product_urls = [
        "https://rozetka.com.ua/ua/product-link-1",
        "https://allo.ua/some-product",
        "https://aliexpress.com/item/100500123456"
    ]

    for url in product_urls:
        try:
            response = requests.post(
                f"{API_URL}/parse",   # ✅ викликаємо /parse
                json={"url": url},    # передаємо URL товару
                timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                print(f"✔ {data['name']} | Ціна: {data['currentPrice']} | "
                      f"Стара ціна: {data.get('oldPrice')} | "
                      f"В наявності: {data['inStock']}")
            else:
                print(f"❌ Помилка {response.status_code} для {url}: {response.text}")
        except Exception as e:
            print(f"⚠️ Не вдалося отримати дані для {url}: {e}")

    time.sleep(10)
    print("Завершено.")

if __name__ == "__main__":
    check_all_prices()
