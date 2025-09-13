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
        # Використовуємо Playwright для Rozetka та Allo, та Aliexpress (якщо хочемо спробувати)
        if any(site in url for site in ["aliexpress.com", "rozetka.com.ua", "allo.ua"]):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                # User-Agent щоб обійти Cloudflare
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/116.0.5845.140 Safari/537.36"
                })
                
                # Переходимо на сторінку
                try:
                    page.goto(url, timeout=60000)
                except PlaywrightTimeout:
                    return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

                # Очікуємо появу основного елемента ціни (для Rozetka і Allo)
                try:
                    if "rozetka.com.ua" in url:
                        page.wait_for_selector(".product-price__current", timeout=10000)
                    elif "allo.ua" in url:
                        page.wait_for_selector(".price, .product-title", timeout=10000)
                    # Aliexpress може не мати селекторів через captcha
                except PlaywrightTimeout:
                    pass  # Якщо не з’явилось — продовжимо

                html = page.content()
                browser.close()
        else:
            # Для інших сайтів requests
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
            }, timeout=30)
            html = r.text

        soup = BeautifulSoup(html, 'html.parser')

        # Назва
        name = (
            soup.select_one("title") or
            soup.select_one(".product-title, .product-name, h1")
        )
        name = name.get_text().strip() if name else "Невідома назва"

        # Поточна ціна
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

        # Стара ціна
        oldPriceTag = (
            soup.select_one(".old-price, .price-old, .product-old-price, .snow-price_SnowPrice-old") or
            soup.select_one(".product-price__old")
        )
        oldPrice = oldPriceTag.get_text().strip() if oldPriceTag else None

        # Наявність
        inStock = bool(soup.select_one(".in-stock, .available")) or True

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print(f"Error: {e}")
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
