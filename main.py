from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import requests
from playwright.sync_api import sync_playwright

app = FastAPI()

# Дозволяємо запити з будь-якого домену
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
        if "aliexpress.com" in url:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url)
                page.wait_for_timeout(3000)
                html = page.content()
                browser.close()
        else:
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"})
            html = r.text

        soup = BeautifulSoup(html, 'html.parser')

        name = soup.title.string.strip() if soup.title else "Невідома назва"

        currentPriceTag = (
            soup.select_one("meta[property='product:price:amount']") or
            soup.select_one("[itemprop='price']") or
            soup.select_one("span.price, .price-current, .snow-price_SnowPrice-main") or
            soup.select_one("div.price, .price-value")
        )
        if currentPriceTag:
            currentPrice = currentPriceTag["content"] if currentPriceTag.name == "meta" else currentPriceTag.get_text().strip()
        else:
            currentPrice = "Невідома ціна"

        oldPriceTag = soup.select_one(".old-price, .price-old, .product-old-price, .snow-price_SnowPrice-old")
        oldPrice = oldPriceTag.get_text().strip() if oldPriceTag else None

        inStock = bool(soup.select_one(".in-stock, .available")) or True

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print(f"Error: {e}")
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None)
