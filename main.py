from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

PROXY_SERVER = os.getenv("PROXY_SERVER")  # беремо з Render Environment

@app.post("/parse", response_model=ParseResponse)
def parse_product(req: ParseRequest):
    url = req.url
    try:
        html = ""

        # Playwright для Rozetka
        if "rozetka.com.ua" in url:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True,
                                            proxy={"server": PROXY_SERVER} if PROXY_SERVER else None)
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.140 Safari/537.36"
                })
                page.goto(url, timeout=120000)  # 2 хвилини max
                page.wait_for_timeout(7000)      # 7 секунд, щоб JS відпрацював
                html = page.content()
                page.screenshot(path="debug.png")  # скріншот для перевірки
                browser.close()
        else:
            html = "<html></html>"  # Для інших сайтів поки порожньо

        soup = BeautifulSoup(html, 'html.parser')

        # Назва
        name_tag = soup.select_one("h1.product__title")
        name = name_tag.get_text().strip() if name_tag else "Невідома назва"

        # Поточна ціна
        price_tag = soup.select_one("p.product-price__big") or soup.select_one(".product-price__current")
        currentPrice = price_tag.get_text().strip() if price_tag else "Невідома ціна"

        # Стара ціна
        old_tag = soup.select_one(".product-price__old")
        oldPrice = old_tag.get_text().strip() if old_tag else None

        # Наявність
        inStock = bool(soup.select_one(".buy-button"))  # є кнопка купити → в наявності

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print(f"Error: {e}")
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None)
