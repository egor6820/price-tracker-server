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
try:
    with open(SELECTORS_FILE, "r", encoding="utf-8") as f:
        SITE_SELECTORS = json.load(f)
except Exception:
    SITE_SELECTORS = {}

# default for rozetka if not in JSON
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
PLACEHOLDER_KEYWORDS = ["зачекайте", "трохи", "завантаж", "loading", "loading...", "please wait"]

def clean_price_text(text: Optional[str]) -> Optional[str]:
    """Вертає рядок з цифрами (наприклад '119' або '119.00') або None, якщо цифр немає."""
    if not text:
        return None
    txt = text.strip()
    txt = txt.replace("\xa0", " ")
    # знайдемо найбільш ймовірну частину з ціною, включаючи пробіли/тисячні/коми/крапки
    m = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?|[0-9]+(?:[.,][0-9]{1,2})?)", txt)
    if m:
        found = m.group(1)
        cleaned = found.replace(" ", "").replace("\u00A0", "").replace(",", ".")
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
        currency = offers.get("priceCurrency") or (offers.get("priceSpecification", {}).get("priceCurrency") if isinstance(offers.get("priceSpecification"), dict) else None)
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

# ---- Playwright extraction with waiting for "real" price/name ----
def extract_with_playwright_direct(url: str, domain_cfg: dict | None = None, wait_for_price_sec: int = 20):
    """Повертає dict з полями name, price_text, old_price_text, html (можливо None)"""
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
            # даємо трохи часу на рендер (але у циклі чекаємо селектори)
            page.wait_for_timeout(500)

            # Якщо є domain_cfg, пробуємо спочатку селектори
            if domain_cfg:
                # name
                for sel in domain_cfg.get("name", []):
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            txt = el.inner_text(timeout=2000).strip()
                            if txt and all(kw not in txt.lower() for kw in PLACEHOLDER_KEYWORDS):
                                result["name"] = txt
                                # don't break: we prefer to keep name if found
                                break
                    except Exception:
                        continue

                # price: чекати поки selector дасть реальні цифри
                for sel in domain_cfg.get("price", []):
                    try:
                        # якщо селектор - meta, беремо attribute
                        if sel.startswith("meta"):
                            meta = page.query_selector(sel)
                            if meta:
                                content = meta.get_attribute("content")
                                if content and clean_price_text(content):
                                    result["price_text"] = content.strip()
                                    print("Playwright: meta price selector matched:", sel)
                                    break
                            continue

                        el = page.locator(sel).first
                        if el.count() == 0:
                            continue
                        # чекатимемо поки inner_text містить цифри і не містить плейсхолдерів
                        end_time = time.time() + wait_for_price_sec
                        while time.time() < end_time:
                            try:
                                txt = el.inner_text(timeout=2000).strip()
                            except Exception:
                                txt = ""
                            if text_has_digits_and_not_placeholder(txt):
                                result["price_text"] = txt
                                print("Playwright: price selector matched:", sel)
                                break
                            time.sleep(0.4)
                        if result["price_text"]:
                            break
                    except Exception:
                        continue

                # old_price
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

            # Якщо нічого не знайдено через селектори, повертаємо HTML для фолбеку
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

# ---- Fallback: requests + BeautifulSoup extraction ----
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

        used_playwright = False
        html = ""

        dynamic_domains = ["rozetka.com.ua", "aliexpress.com", "allo.ua"]
        if domain_cfg or any(d in url for d in dynamic_domains):
            used_playwright = True
            try:
                extracted = extract_with_playwright_direct(url, domain_cfg=domain_cfg, wait_for_price_sec=20)
                html = extracted.get("html") or ""
                # prefer direct extracted texts if valid
                if extracted.get("name") and extracted["name"].strip():
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
                # fallback to requests
                print("Playwright failed, falling back to requests:", e)
                try:
                    html = parse_using_requests(url, timeout=30)
                except Exception as e2:
                    print("Requests fallback also failed:", e2)
                    return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)
        else:
            try:
                html = parse_using_requests(url, timeout=30)
            except Exception as e:
                print("Requests failed, trying playwright:", e)
                extracted = extract_with_playwright_direct(url, domain_cfg=domain_cfg, wait_for_price_sec=10)
                html = extracted.get("html") or ""

        if not html and not (name or currentPrice):
            return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

        # parse html with BeautifulSoup (fallback/extra checks)
        soup = BeautifulSoup(html, "html.parser")

        # 1) ld+json
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

        # 2) domain cfg with BeautifulSoup (if playwright didn't return strong results)
        if domain_cfg:
            if not name:
                for sel in domain_cfg.get("name", []):
                    tag = soup.select_one(sel)
                    if tag:
                        txt = tag.get_text(" ", strip=True)
                        if text_has_digits_and_not_placeholder(txt) or (txt and not any(k in txt.lower() for k in PLACEHOLDER_KEYWORDS)):
                            name = txt
                            print("Soup: name selector matched:", sel)
                            break
                        elif txt:
                            # name may contain no digits (that's fine) but must not contain placeholders
                            if not any(k in txt.lower() for k in PLACEHOLDER_KEYWORDS):
                                name = txt
                                break

            if not currentPrice:
                for sel in domain_cfg.get("price", []):
                    tag = soup.select_one(sel)
                    if tag:
                        if tag.name == "meta":
                            content = tag.get("content", "").strip()
                            cp = clean_price_text(content)
                            if cp:
                                currentPrice = cp
                                print("Soup: meta price matched:", sel)
                                break
                        else:
                            txt = tag.get_text(" ", strip=True)
                            cp = clean_price_text(txt)
                            if cp:
                                currentPrice = cp
                                print("Soup: price selector matched:", sel)
                                break

            if not oldPrice:
                for sel in domain_cfg.get("old_price", []):
                    tag = soup.select_one(sel)
                    if tag:
                        txt = tag.get_text(" ", strip=True)
                        op = clean_price_text(txt)
                        if op:
                            oldPrice = op
                            print("Soup: old price selector matched:", sel)
                            break

            if inStock is None and "in_stock_text" in domain_cfg:
                page_text = soup.get_text(" ", strip=True).lower()
                if text_contains_any(page_text, domain_cfg.get("in_stock_text", [])):
                    inStock = False

        # 3) meta / og / itemprop
        if not name:
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
                cp = clean_price_text(m1.get("content"))
                if cp:
                    currentPrice = cp
            else:
                ip = soup.select_one("[itemprop='price']")
                if ip:
                    if ip.name == "meta":
                        cp = clean_price_text(ip.get("content"))
                        if cp:
                            currentPrice = cp
                    else:
                        cp = clean_price_text(ip.get_text(" ", strip=True))
                        if cp:
                            currentPrice = cp

        if not oldPrice:
            op = soup.select_one(".old-price, .product-price__old, .price-old, .product-price__small")
            if op:
                op_txt = clean_price_text(op.get_text(" ", strip=True))
                if op_txt:
                    oldPrice = op_txt

        # 4) general text regex fallback (last resort)
        if not currentPrice:
            full_text = soup.get_text(" ", strip=True)
            m = re.search(r"[0-9]+(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?", full_text)
            if m:
                cp = clean_price_text(m.group(0))
                if cp:
                    currentPrice = cp

        # 5) inStock heuristics
        if inStock is None:
            page_text = soup.get_text(" ", strip=True).lower()
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
                inStock = bool(currentPrice)

        # final tidy
        name = name or "Невідома назва"
        currentPrice = currentPrice or None
        oldPrice = oldPrice or None

        return ParseResponse(
            name=name,
            currentPrice=currentPrice if currentPrice else "Невідома ціна",
            oldPrice=oldPrice,
            inStock=bool(inStock)
        )

    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
