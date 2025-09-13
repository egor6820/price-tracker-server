from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import requests
from playwright.sync_api import sync_playwright

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # змінити на конкретний домен для безпеки
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

        # Rozetka / AliExpress
        if "aliexpress.com" in url or "rozetka.com.ua" in url:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, slow_mo=50)
                page = browser.new_page()
                
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5845.140 Safari/537.36",
                    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
                })

                page.goto(url, timeout=120000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                html = page.content()
                browser.close()
        else:
            r = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
            })
            html = r.text

        soup = BeautifulSoup(html, 'html.parser')

        # Назва
        name = soup.title.string.strip() if soup.title else "Невідома назва"

        # Поточна ціна
        currentPriceTag = (
            soup.select_one(".product-prices__big") or
            soup.select_one(".product-price__current") or
            soup.select_one("[itemprop='price']") or
            soup.select_one("meta[property='product:price:amount']")
        )
        currentPrice = currentPriceTag.get_text().strip() if currentPriceTag and currentPriceTag.name != "meta" else (
            currentPriceTag["content"] if currentPriceTag else "Невідома ціна"
        )

        # Стара ціна
        oldPriceTag = (
            soup.select_one(".product-price__old") or
            soup.select_one(".product-prices__small")
        )
        oldPrice = oldPriceTag.get_text().strip() if oldPriceTag else None

        # Наявність
        inStock = bool(soup.select_one(".in-stock, .available")) or True

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print(f"Error: {e}")
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None)
