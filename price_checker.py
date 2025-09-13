import time
import requests  # бібліотека для HTTP-запитів

# 👉 сюди вставляєш адресу свого бекенду з Render або Railway
API_URL = "https://price-tracker-api.onrender.com"

def check_all_prices():
    print("Перевірка цін...")

    try:
        response = requests.get(f"{API_URL}/prices")  # ендпоінт бекенду
        if response.status_code == 200:
            data = response.json()
            print("Отримані ціни:", data)
        else:
            print("Помилка:", response.status_code, response.text)
    except Exception as e:
        print("Не вдалося підключитися до бекенду:", e)

    time.sleep(10)  # затримка, щоб симулювати періодичність
    print("Завершено.")

if __name__ == "__main__":
    check_all_prices()
