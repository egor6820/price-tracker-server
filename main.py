from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

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

        if "aliexpress.com" in url or "rozetka.com.ua" in url:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                  " AppleWebKit/537.36 (KHTML, like Gecko)"
                                  " Chrome/116.0.5845.140 Safari/537.36"
                })
                page.goto(url, timeout=60000)  # 60 секунд
                page.wait_for_timeout(5000)    # 5 секунд для JS
                html = page.content()
                browser.close()
        else:
            import requests
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0"
            }, timeout=30)
            html = r.text

        soup = BeautifulSoup(html, 'html.parser')

        # Назва
        name = soup.title.string.strip() if soup.title else "Невідома назва"

        # Поточна ціна
        currentPriceTag = (
            soup.select_one("meta[property='product:price:amount']") or
            soup.select_one("[itemprop='price']") or
            soup.select_one("span.price, .price-current, .snow-price_SnowPrice-main") or
            soup.select_one("div.price, .price-value") or
            soup.select_one(".product-price__current")
        )
        currentPrice = currentPriceTag["content"] if currentPriceTag and currentPriceTag.name == "meta" else currentPriceTag.get_text().strip() if currentPriceTag else "Невідома ціна"

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
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None)
