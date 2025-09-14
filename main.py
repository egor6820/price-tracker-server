from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import re
import json
import time
import traceback
from typing import Optional

# ---- Configurable: path to JSON with per-site selectors (можеш тримати цей JSON поруч з кодом) ----
SELECTORS_FILE = "site_selectors.json"

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
    oldPrice: Optional[str] = None
    inStock: bool = True

# ---- Load per-site selectors (simple structure) ----
# Example content of site_selectors.json:
# {
#   "rozetka.com.ua": {
#       "name": ["h1.title__font", "h1.product__title", "h1.product__heading", "[itemprop='name']"],
#       "price": ["p.product-price__big", "[itemprop='price']", "meta[property='product:price:amount']"],
#       "old_price": ["p.product-price__small", ".product-price__old", ".old-price"],
#       "in_stock_text": ["в наявності", "є в наявності", "товар відсутній"]
#   }
# }
try:
    with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
        SITE_SELECTORS = json.load(f)
except Exception:
    SITE_SELECTORS = {}

# ---- Helpers ----
def clean_price_text(text: str) -> str:
    if not text:
        return "Невідома ціна"
    txt = text.strip()
    # заміна нерозривних пробілів
    txt = txt.replace("\xa0", " ")
    # знайдемо перший фрагмент з цифрами, комами або крапками
    m = re.search(r"[0-9]+[0-9\s\.,\u00A0]*[0-9]*", txt)
    if m:
        found = m.group(0)
        # прибрати пробіли і замінити коми на точку, якщо це десятковий роздільник
        cleaned = found.replace(" ", "").replace("\u00A0", "")
        # якщо є і коми і крапка — залишимо як є (люди можуть використовувати різні формати)
        cleaned = cleaned.replace(",", ".")
        return cleaned
    return txt

def extract_ld_json(soup: BeautifulSoup):
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    for s in scripts:
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            # іноді в одному <script> може бути масив або кілька об'єктів
            try:
                text = s.string or ""
                # спробуємо знайти JSON частини
                data = json.loads(text.strip())
            except Exception:
                continue
        # support either list or dict
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type", "").lower() in ("product", "offer"):
                    yield item
        elif isinstance(data, dict):
            if data.get("@type", "").lower() in ("product", "offer") or "offers" in data:
                yield data

def price_from_ld(item):
    # шукаємо offers.price або offers.priceSpecification
    offers = item.get("offers")
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("priceSpecification", {}).get("price")
        currency = offers.get("priceCurrency") or (offers.get("priceSpecification", {}).get("priceCurrency") if isinstance(offers.get("priceSpecification"), dict) else None)
        if price:
            return str(price) + (f" {currency}" if currency else "")
    # інші поля
    if "price" in item:
        return str(item["price"])
    return None

def text_contains_any(text: str, needles: list):
    t = (text or "").lower()
    for n in needles:
        if n.lower() in t:
            return True
    return False

# ---- Main parsing logic ----
def parse_using_requests(url: str, timeout: int = 20):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

def parse_with_playwright(url: str, domain_cfg: dict | None = None, max_attempts=2):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
                    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                })
                # збільшені таймаути, навігація з очікуванням networkidle
                page.goto(url, timeout=120000)
                try:
                    page.wait_for_load_state('networkidle', timeout=60000)
                except PlaywrightTimeout:
                    # все одно продовжимо — іноді networkidle не настає, але контент є
                    pass
                # даємо трохи часу на виконання JS (але не надто довго)
                page.wait_for_timeout(1500)

                # Перший пріоритет: доменні селектори з конфігів (якщо задані)
                if domain_cfg:
                    # name
                    for sel in domain_cfg.get("name", []):
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0:
                                txt = el.inner_text(timeout=2000).strip()
                                if txt:
                                    browser.close()
                                    return {"name": txt, "html": page.content()}
                        except Exception:
                            continue
                    # price
                    for sel in domain_cfg.get("price", []):
                        try:
                            el = page.locator(sel).first
                            if el.count() > 0:
                                # meta tags handled later, but try inner_text
                                txt = el.inner_text(timeout=2000).strip()
                                if txt:
                                    browser.close()
                                    return {"price": txt, "html": page.content()}
                        except Exception:
                            continue

                # Якщо специфічних селекторів немає або не спрацювали, повернемо HTML та дамо загальній логіці розпарсити
                html = page.content()
                browser.close()
                return {"html": html}
        except Exception as e:
            last_exc = e
            # короткий бекоф
            time.sleep(1 + attempt)
            continue
    # якщо не вдалося
    raise last_exc or Exception("Playwright failed")

@app.post("/parse", response_model=ParseResponse)
def parse_product(req: ParseRequest):
    url = req.url
    domain_cfg = None
    for domain_key, cfg in SITE_SELECTORS.items():
        if domain_key in url:
            domain_cfg = cfg
            break

    try:
        html = ""
        used_playwright = False

        # Визначаємо, чи потрібен браузер:
        # якщо в конфізі є селектори для цього домену — використовуємо playwright (надійніше для динамічних сайтів)
        # або якщо в url містяться відомі динамічні домени - теж playwright
        dynamic_domains = ["rozetka.com.ua", "aliexpress.com", "allo.ua"]
        if domain_cfg or any(d in url for d in dynamic_domains):
            used_playwright = True
            parsed = parse_with_playwright(url, domain_cfg=domain_cfg)
            html = parsed.get("html", "")
        else:
            # пробуємо requests, якщо сторінка статична
            try:
                html = parse_using_requests(url, timeout=30)
            except Exception as e:
                # fallback на playwright
                parsed = parse_with_playwright(url, domain_cfg=domain_cfg)
                html = parsed.get("html", "")

        if not html:
            return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

        # Дебаг (залишити поки що)
        print("Loaded HTML length:", len(html))
        soup = BeautifulSoup(html, "html.parser")

        # 1) Спробуємо ld+json
        name = None
        currentPrice = None
        oldPrice = None
        inStock = None

        for item in extract_ld_json(soup):
            # name
            if not name:
                name = item.get("name") or item.get("headline")
            # price
            if not currentPrice:
                p = price_from_ld(item)
                if p:
                    currentPrice = p
            # availability
            offers = item.get("offers") if isinstance(item, dict) else None
            if offers and isinstance(offers, dict):
                avail = offers.get("availability", "")
                if avail:
                    inStock = not ("OutOfStock".lower() in str(avail).lower() or "notavailable" in str(avail).lower())

        # 2) Селектори з конфігів (швидка спроба)
        if domain_cfg:
            # name
            if not name:
                for sel in domain_cfg.get("name", []):
                    tag = soup.select_one(sel)
                    if tag and tag.get_text(strip=True):
                        name = tag.get_text(strip=True)
                        break
            # price
            if not currentPrice:
                for sel in domain_cfg.get("price", []):
                    tag = soup.select_one(sel)
                    if tag:
                        if tag.name == "meta":
                            currentPrice = tag.get("content")
                        else:
                            currentPrice = tag.get_text(strip=True)
                        if currentPrice:
                            break
            # old price
            if not oldPrice:
                for sel in domain_cfg.get("old_price", []):
                    tag = soup.select_one(sel)
                    if tag and tag.get_text(strip=True):
                        oldPrice = tag.get_text(strip=True)
                        break
            # in stock via text
            if inStock is None and "in_stock_text" in domain_cfg:
                page_text = soup.get_text(" ", strip=True).lower()
                # якщо в тексті є слова "відсутній", "немає в наявності" -> inStock False
                if text_contains_any(page_text, domain_cfg.get("in_stock_text", [])):
                    inStock = False

        # 3) Метатеги / og / itemprop / meta price
        if not name:
            # og:title або meta[name="title"] або title
            meta_og = soup.select_one("meta[property='og:title'], meta[name='og:title']")
            if meta_og and meta_og.get("content"):
                name = meta_og.get("content").strip()
            else:
                t = soup.select_one("meta[name='title']")
                if t and t.get("content"):
                    name = t.get("content").strip()
                else:
                    t2 = soup.select_one("title")
                    if t2:
                        name = t2.get_text(strip=True)

        if not currentPrice:
            m1 = soup.select_one("meta[property='product:price:amount'], meta[name='price']")
            if m1 and m1.get("content"):
                currentPrice = m1.get("content")
            else:
                ip = soup.select_one("[itemprop='price']")
                if ip:
                    if ip.name == "meta":
                        currentPrice = ip.get("content")
                    else:
                        currentPrice = ip.get_text(strip=True)

        if not oldPrice:
            op = soup.select_one(".old-price, .product-price__old, .price-old, .product-price__small")
            if op:
                oldPrice = op.get_text(strip=True)

        # 4) Якщо досі немає — використовуємо універсальний regex пошук по тексту (останній шанс)
        if not currentPrice:
            text = soup.get_text(" ", strip=True)
            m = re.search(r"[0-9]+(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?\s*(?:грн|uah|₴|uah| грн|UAH)?", text, flags=re.IGNORECASE)
            if m:
                currentPrice = m.group(0)

        # 5) Наявність: шукаємо очевидні тексти
        if inStock is None:
            page_text = soup.get_text(" ", strip=True).lower()
            # фрази, які значать що товар немає
            not_in_stock_phrases = [
                "немає в наявності", "відсутній", "закінчився", "sold out", "out of stock", "товар відсутній"
            ]
            in_stock_phrases = [
                "в наявності", "є в наявності", "available", "в наявності:"
            ]
            if text_contains_any(page_text, not_in_stock_phrases):
                inStock = False
            elif text_contains_any(page_text, in_stock_phrases):
                inStock = True
            else:
                # якщо знайшли ціну — вважаємо що, ймовірно, в наявності
                inStock = bool(currentPrice)

        # finalize
        name = name or "Невідома назва"
        currentPrice = clean_price_text(currentPrice or "Невідома ціна")
        oldPrice = clean_price_text(oldPrice) if oldPrice else None

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=bool(inStock))

    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
