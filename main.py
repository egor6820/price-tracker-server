from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

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

@app.post("/parse", response_model=ParseResponse)
def parse_product(req: ParseRequest):
    url = req.url
    try:
        html = ""
        if "rozetka.com.ua" in url:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)  # для дебагу можна ставити False
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/116.0.5845.140 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                page = context.new_page()
                
                # Переходимо на сторінку
                page.goto(url, timeout=90000)
                
                # Очікуємо появу ціни
                try:
                    page.wait_for_selector(".product-price__current", timeout=60000)
                except PlaywrightTimeout:
                    page.screenshot(path="roz_fail.png")  # скріншот для дебагу
                    return ParseResponse(
                        name="Помилка завантаження (Cloudflare?)",
                        currentPrice="Помилка",
                        oldPrice=None,
                        inStock=False
                    )

                html = page.content()
                browser.close()
        else:
            # для інших сайтів
            html = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0"
            }, timeout=30).text

        soup = BeautifulSoup(html, 'html.parser')

        # Назва
        name_tag = soup.select_one("h1.product__title") or soup.select_one("title")
        name = name_tag.get_text().strip() if name_tag else "Невідома назва"

        # Поточна ціна
        price_tag = soup.select_one(".product-price__current")
        currentPrice = price_tag.get_text().strip() if price_tag else "Невідома ціна"

        # Стара ціна
        old_price_tag = soup.select_one(".product-price__old")
        oldPrice = old_price_tag.get_text().strip() if old_price_tag else None

        # Наявність
        inStock = bool(soup.select_one(".in-stock, .available")) or True

        return ParseResponse(
            name=name,
            currentPrice=currentPrice,
            oldPrice=oldPrice,
            inStock=inStock
        )

    except Exception as e:
        print(f"Error: {e}")
        return ParseResponse(
            name="Невідома назва",
            currentPrice="Невідома ціна",
            oldPrice=None,
            inStock=False
        )
