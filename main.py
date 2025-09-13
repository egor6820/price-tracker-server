from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Для безпеки можна вказати конкретні домени
    allow_methods=["*"],
    allow_headers=["*"]
)

class ParseRequest(BaseModel):
    url: str

class ParseResponse(BaseModel):
    name: str
    currentPrice: str
    oldPrice: str | None = None
    inStock: bool = True

@app.post("/parse", response_model=ParseResponse)
def parse_product(req: ParseRequest):
    url = req.url
    try:
        html = ""
        if any(site in url for site in ["aliexpress.com", "rozetka.com.ua", "allo.ua"]):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                # Розширені headers
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.140 Safari/537.36",
                    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                })
                
                try:
                    page.goto(url, timeout=120000)  # Збільшено таймаут
                    page.wait_for_load_state('networkidle', timeout=60000)  # Збільшено таймаут
                    page.wait_for_timeout(10000)  # Збільшено час на JS
                except PlaywrightTimeout:
                    print("Timeout error loading page")
                    browser.close()
                    return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

                # Специфічне очікування для Rozetka
                if "rozetka.com.ua" in url:
                    try:
                        page.wait_for_selector("h1.product__title, h1.product__heading", timeout=30000)  # Очікування селектора назви
                    except PlaywrightTimeout:
                        print("Timeout waiting for name selector")
                    try:
                        page.wait_for_selector("p.product-price__big", timeout=30000)  # Очікування селектора ціни
                    except PlaywrightTimeout:
                        print("Timeout waiting for price selector")

                html = page.content()
                browser.close()
        else:
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
            }, timeout=60)  # Збільшено таймаут
            html = r.text

        # Дебаг: подивись в консоль, чи є HTML
        print(f"Loaded HTML snippet: {html[:1000]}...")

        soup = BeautifulSoup(html, 'html.parser')

        # Логіка для Rozetka з оновленими селекторами
        if "rozetka.com.ua" in url:
            # Назва: альтернативи
            name_tag = soup.select_one("h1.product__title, h1.product__heading, h1.ng-star-inserted, [itemprop='name'], h1.product__name, h1.rz-product__name")
            name = name_tag.get_text().strip() if name_tag else "Невідома назва"
            print(f"Found name: {name}")  # Дебаг

            # Поточна ціна: з твого фрагменту, з альтернативами
            currentPriceTag = soup.select_one("p.product-price__big, [class*='product-price__big'], [itemprop='price'], meta[property='product:price:amount'], p.product-prices__big, div.product-price p")
            if currentPriceTag:
                if currentPriceTag.name == "meta":
                    currentPrice = currentPriceTag.get("content", "Невідома ціна")
                else:
                    currentPrice = currentPriceTag.get_text().strip()  # Візьме "570₴"
            else:
                currentPrice = "Невідома ціна"
            print(f"Found current price: {currentPrice}")  # Дебаг
            
            # Стара ціна: альтернативи
            oldPriceTag = soup.select_one("p.product-price__small, [class*='product-price__small'], .product-price__old, .old-price, p.product-prices__small")
            oldPrice = oldPriceTag.get_text().strip() if oldPriceTag else None
            print(f"Found old price: {oldPrice}")  # Дебаг
            
            # Наявність: шукаємо клас або текст
            stock_tag = (
                soup.select_one("[class*='status-label'], .product-availability, [class*='status-label--green'], .status-label") or
                soup.find(lambda tag: tag.text and "наявності" in tag.text.lower() or "в наявності" in tag.text.lower())
            )
            inStock = bool(stock_tag)
            print(f"Found inStock: {inStock}")  # Дебаг
        else:
            # Загальна логіка для інших
            name_tag = soup.select_one("title") or soup.select_one(".product-title, .product-name, h1")
            name = name_tag.get_text().strip() if name_tag else "Невідома назва"

            currentPriceTag = (
                soup.select_one(".product-price__current") or
                soup.select_one(".price-current, .price, .snow-price_SnowPrice-main") or
                soup.select_one("meta[property='product:price:amount']") or
                soup.select_one("[itemprop='price']")
            )
            if currentPriceTag:
                if currentPriceTag.name == "meta":
                    currentPrice = currentPriceTag.get("content", "Невідома ціна")
                else:
                    currentPrice = currentPriceTag.get_text().strip()
            else:
                currentPrice = "Невідома ціна"

            oldPriceTag = (
                soup.select_one(".old-price, .price-old, .product-old-price, .snow-price_SnowPrice-old") or
                soup.select_one(".product-price__old")
            )
            oldPrice = oldPriceTag.get_text().strip() if oldPriceTag else None

            inStock = bool(soup.select_one(".in-stock, .available")) or True

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print(f"Error: {e}")
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
