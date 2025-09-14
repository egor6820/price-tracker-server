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

# ---- Configurable: path to JSON with per-site selectors ----
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

# ---- Load per-site selectors ----
try:
    with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
        SITE_SELECTORS = json.load(f)
except Exception:
    SITE_SELECTORS = {}

SITE_SELECTORS.setdefault("rozetka.com.ua", {
    "name": [
        "h1.title__font",
        "h1[class*='title__font']",
        "h1.product__title",
        "[itemprop='name']"
    ],
    "price": [
        "p.product-price__big",
        "p[class*='product-price__big']",
        ".product-price__big",
        "p.product-price__main",
        "[itemprop='price']",
        "meta[property='product:price:amount']"
    ],
    "old_price": [
        "p.product-price__small",
        "p[class*='product-price__small']",
        ".product-price__small",
        ".product-price__old",
        ".product-old-price"
    ],
    "in_stock_text": [
        "немає в наявності",
        "відсутній",
        "закінчився",
        "є в наявності",
        "в наявності"
    ]
})

# ---- Helpers ----
PLACEHOLDER_KEYWORDS = ["зачекайте", "трохи", "завантаж", "loading", "please wait"]

def clean_price_text(text: Optional[str]) -> Optional[str]:
    """Вертає рядок з цифрами (наприклад '119' або '119.00') або None, якщо цифр немає."""
    if not text:
        return None
    txt = text.strip()
    txt = txt.replace("\xa0", " ")
    m = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?|[0-9]+(?:[.,][0-9]{1,2})?)", txt)
    if m:
        found = m.group(1)
        cleaned = found.replace(" ", "").replace("\u00A0", "").replace(",", ".")
        try:
            val = float(cleaned)
            if val < 10:  # захист від випадкових «97 відгуків»
                return None
        except:
            pass
        return cleaned
    return None

def text_has_digits_and_not_placeholder(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    if not re.search(r"\d", t):
        return False
    for kw in PLACEHOLDER_KEYWORDS:
        if kw in t:
            return False
    return True

def extract_ld_json(soup: BeautifulSoup):
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    for s in scripts:
        try:
            data = json.loads(s.string or "{}")
        except Exception:
            try:
                text = s.string or ""
                data = json.loads(text.strip())
            except Exception:
                continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type", "").lower() in ("product", "offer"):
                    yield item
        elif isinstance(data, dict):
            if data.get("@type", "").lower() in ("product", "offer") or "offers" in data:
                yield data

def price_from_ld(item):
    offers = item.get("offers")
    if isinstance(offers, dict):
        price = offers.get("price") or offers.get("priceSpecification", {}).get("price")
        currency = offers.get("priceCurrency") or (
            offers.get("priceSpecification", {}).get("priceCurrency")
            if isinstance(offers.get("priceSpecification"), dict)
            else None
        )
        if price:
            return str(price) + (f" {currency}" if currency else "")
    if "price" in item:
        return str(item["price"])
    return None

def text_contains_any(text: str, needles: list):
    t = (text or "").lower()
    for n in needles:
        if n.lower() in t:
            return True
    return False

# ---- Playwright extraction ----
def extract_with_playwright_direct(url: str, domain_cfg: dict | None = None, wait_for_price_sec: int = 20):
    result = {"name": None, "price_text": None, "old_price_text": None, "html": None}
    last_exc = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        page = browser.new_page()
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        try:
            page.goto(url, timeout=120000)
            try:
                page.wait_for_load_state('networkidle', timeout=60000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(500)

            if domain_cfg:
                # name з циклом очікування
                for sel in domain_cfg.get("name", []):
                    try:
                        el = page.locator(sel).first
                        if el.count() == 0:
                            continue
                        end_time = time.time() + wait_for_price_sec
                        while time.time() < end_time:
                            try:
                                txt = el.inner_text(timeout=2000).strip()
                            except Exception:
                                txt = ""
                            if txt and all(kw not in txt.lower() for kw in PLACEHOLDER_KEYWORDS):
                                result["name"] = txt
                                break
                            time.sleep(0.4)
                        if result["name"]:
                            break
                    except Exception:
                        continue

                # price
                for sel in domain_cfg.get("price", []):
                    try:
                        if sel.startswith("meta"):
                            meta = page.query_selector(sel)
                            if meta:
                                content = meta.get_attribute("content")
                                if content and clean_price_text(content):
                                    result["price_text"] = content.strip()
                                    break
                            continue
                        el = page.locator(sel).first
                        if el.count() == 0:
                            continue
                        end_time = time.time() + wait_for_price_sec
                        while time.time() < end_time:
                            try:
                                txt = el.inner_text(timeout=2000).strip()
                            except Exception:
                                txt = ""
                            if text_has_digits_and_not_placeholder(txt):
                                result["price_text"] = txt
                                break
                            time.sleep(0.4)
                        if result["price_text"]:
                            break
                    except Exception:
                        continue

                # old price
                for sel in domain_cfg.get("old_price", []):
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            txt = el.inner_text(timeout=2000).strip()
                            if text_has_digits_and_not_placeholder(txt):
                                result["old_price_text"] = txt
                                break
                    except Exception:
                        continue

            result["html"] = page.content()
            browser.close()
            return result

        except Exception as e:
            last_exc = e
            traceback.print_exc()
            try:
                browser.close()
            except Exception:
                pass
            raise last_exc

# ---- Fallback requests ----
def parse_using_requests(url: str, timeout: int = 20):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

@app.post("/parse", response_model=ParseResponse)
def parse_product(req: ParseRequest):
    url = req.url
    domain_cfg = None
    for domain_key, cfg in SITE_SELECTORS.items():
        if domain_key in url:
            domain_cfg = cfg
            break

    try:
        name = None
        currentPrice = None
        oldPrice = None
        inStock = None
        html = ""

        dynamic_domains = ["rozetka.com.ua", "aliexpress.com", "allo.ua"]
        if domain_cfg or any(d in url for d in dynamic_domains):
            try:
                extracted = extract_with_playwright_direct(url, domain_cfg=domain_cfg, wait_for_price_sec=20)
                html = extracted.get("html") or ""
                if extracted.get("name"):
                    name = extracted["name"].strip()
                if extracted.get("price_text"):
                    cp = clean_price_text(extracted["price_text"])
                    if cp:
                        currentPrice = cp
                if extracted.get("old_price_text"):
                    op = clean_price_text(extracted["old_price_text"])
                    if op:
                        oldPrice = op
            except Exception as e:
                print("Playwright failed, falling back to requests:", e)
                html = parse_using_requests(url, timeout=30)
        else:
            html = parse_using_requests(url, timeout=30)

        if not html and not (name or currentPrice):
            return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

        soup = BeautifulSoup(html, "html.parser")

        # ld+json
        if not name or not currentPrice:
            for item in extract_ld_json(soup):
                if not name:
                    name = item.get("name") or item.get("headline")
                if not currentPrice:
                    p = price_from_ld(item)
                    if p:
                        cp = clean_price_text(p)
                        if cp:
                            currentPrice = cp
                if not inStock:
                    offers = item.get("offers") if isinstance(item, dict) else None
                    if offers and isinstance(offers, dict):
                        avail = offers.get("availability", "")
                        if avail:
                            inStock = not ("outofstock" in str(avail).lower() or "notavailable" in str(avail).lower())

        # soup selectors
        if domain_cfg:
            if not currentPrice:
                for sel in domain_cfg.get("price", []):
                    tag = soup.select_one(sel)
                    if tag:
                        if tag.name == "meta":
                            cp = clean_price_text(tag.get("content", "").strip())
                        else:
                            cp = clean_price_text(tag.get_text(" ", strip=True))
                        if cp:
                            currentPrice = cp
                            break

        # fallback: пробуємо лише по блоках з цінами
        if not currentPrice:
            price_candidates = soup.select(
                ".product-price, .product-price__big, .product-price__main, [itemprop='price']"
            )
            for tag in price_candidates:
                cp = clean_price_text(tag.get_text(" ", strip=True))
                if cp:
                    currentPrice = cp
                    break

        # останній fallback — regex
        if not currentPrice:
            full_text = soup.get_text(" ", strip=True)
            m = re.search(r"[0-9]+(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?", full_text)
            if m:
                cp = clean_price_text(m.group(0))
                if cp:
                    currentPrice = cp

        name = name or "Невідома назва"
        currentPrice = currentPrice or "Невідома ціна"
        oldPrice = oldPrice or None
        inStock = bool(inStock if inStock is not None else currentPrice)

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
