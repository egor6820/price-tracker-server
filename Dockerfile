# Використовуємо легкий Python образ
FROM python:3.11-slim

# Робоча директорія всередині контейнера
WORKDIR /app

# Копіюємо та встановлюємо залежності
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Встановлюємо браузер для Playwright з усіма залежностями
RUN playwright install --with-deps chromium

# Копіюємо весь код
COPY . .

# Команда для запуску FastAPI на Render
//CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
CMD ["sh", "-lc", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
