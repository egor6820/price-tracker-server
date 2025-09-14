from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup, Tag
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import re
import json
import time
import traceback
from typing import Optional, List

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

CURRENCY_KEYWORDS = ['₴', 'грн', 'uah', '₽', 'руб', '₽', 'uah', '$', 'usd', '€', 'eur', 'uah']

def clean_price_text(text: Optional[str]) -> Optional[str]:
    """Вертає рядок з цифрами (наприклад '119' або '119.00') або None, якщо цифр немає."""
    if not text:
        return None
    txt = text.strip()
    txt = txt.replace("\xa0", " ").replace("\u00A0", " ")
    # шукаємо фрагмент з числом (може бути з розділовими пробілами або комою)
    m = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?|[0-9]+(?:[.,][0-9]{1,2})?)", txt)
    if m:
        found = m.group(1)
        cleaned = found.replace(" ", "").replace("\u00A0", "").replace(",", ".")
        try:
            val = float(cleaned)
            # прості захисти від того, що ми витягли номер відгуків або дуже маленьке значення
            if val < 0.01:
                return None
            # дозволяємо і 0.5 - іноді ціни бувають маленькі, тому поріг <10 прибрали
        except:
            return None
        # Повертаємо як рядок без форматування (де дробова чсать з крапкою)
        if cleaned.endswith(".0"):
            cleaned = cleaned[:-2]
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

# ---- New heuristic helpers for generic sites ----

def tag_text_or_attr(tag: Tag) -> str:
    """Повернути найбільш інформативний текст з тегу або його content/value атрибутів."""
    if tag is None:
        return ""
    # meta
    if tag.name == "meta":
        for attr in ("content", "value"):
            if tag.get(attr):
                return str(tag.get(attr))
        return ""
    # формальні атрибути
    for attr in ("data-price", "data-product-price", "content", "value", "title", "alt"):
        if tag.get(attr):
            return str(tag.get(attr))
    # текст
    return tag.get_text(" ", strip=True) or ""

def score_price_candidate(tag: Tag, text: str) -> int:
    """Оцінка кандидата на ціну — чим більше, тим краще."""
    score = 0
    t = (text or "").lower()
    # +50 якщо є явна валюта
    for cur in ['грн', '₴', 'uah', '$', 'usd', '€', 'eur', 'руб', '₽']:
        if cur in t:
            score += 50
    # +40 якщо клас або id містить price/cost/costs/sale
    cls_id = " ".join(filter(None, [tag.get("class") and " ".join(tag.get("class")), tag.get("id") or ""]))
    if re.search(r"(price|cost|цiн|ціна|price__|product-price|sale|amount|value|sum|грн|uah)", cls_id, flags=re.I):
        score += 40
    # +30 якщо тег має itemprop=price або meta property price
    if tag.get("itemprop") and "price" in tag.get("itemprop").lower():
        score += 30
    if tag.name == "meta" and tag.get("property", "").lower().find("price") != -1:
        score += 20
    # +10 за короткий текст (менше 6 слів) — типовий формат ціни
    words = len((text or "").split())
    if words <= 4:
        score += 10
    # -penalty якщо в тексті слова 'відгук', 'коментар', 'шт' — ймовірно не ціна
    if re.search(r"(відгук|коментар|коментарі|reviews|rating|шт|pcs|відгук)", t):
        score -= 30
    return score

def find_best_price(soup: BeautifulSoup) -> (Optional[str], Optional[str], Optional[Tag]):
    """
    Повертає (currentPrice_str, oldPrice_str, price_tag)
    шукає серед meta, itemprop, attributes, класи, тексти з валютою, та ранжує кандидатів.
    """
    # 1) Самі важливі мета/ld дані (будуть також оброблені окремо, але тут додамо)
    # meta property product:price:amount
    metas = soup.find_all("meta")
    for m in metas:
        if m.get("property", "").lower() in ("product:price:amount",):
            content = m.get("content", "")
            cp = clean_price_text(content)
            if cp:
                return cp, None, m

    # 2) itemprop price
    item_price = soup.select("[itemprop='price'], [itemprop='priceCurrency']")
    for it in item_price:
        text = tag_text_or_attr(it)
        cp = clean_price_text(text)
        if cp:
            return cp, None, it

    # 3) збираємо кандидати: елементи з атрибутами data-price або класами з "price" або елементи <span>/<p>/<div> з числами
    candidates = []
    # a) явні атрибути
    for tag in soup.find_all(attrs={"data-price": True}):
        txt = tag_text_or_attr(tag)
        cp = clean_price_text(txt)
        if cp:
            candidates.append((tag, txt, score_price_candidate(tag, txt)))

    # b) атрибути content/value у meta або інш.
    for tag in soup.find_all():
        # обмежимо пошук, щоб не йти по всіх великих блоках — беремо короткі теги
        if tag.name in ("span", "p", "div", "strong", "b", "li", "a", "td", "em") or tag.name == "meta":
            txt = tag_text_or_attr(tag)
            if not txt:
                continue
            # шукаємо рядки з цифрами
            if re.search(r"\d", txt):
                cp = clean_price_text(txt)
                if cp:
                    candidates.append((tag, txt, score_price_candidate(tag, txt)))

    # c) сортування кандидатів за оцінкою
    if candidates:
        candidates_sorted = sorted(candidates, key=lambda x: x[2], reverse=True)
        best_tag, best_text, best_score = candidates_sorted[0]
        # намагаємось знайти old price поруч (наприклад тег з класом old або 'strike')
        old_price = None
        # перевіримо в дочірніх/сусідніх елементах
        parent = best_tag.parent
        if parent:
            # шукаємо в parent старі позначення
            for child in parent.find_all():
                if child == best_tag:
                    continue
                t = tag_text_or_attr(child)
                if re.search(r"(old|previous|strike|crossed|product-price__small|product-old|price--old|sale)", " ".join(filter(None, [child.get("class") and " ".join(child.get("class")), child.get("id") or ""])) , flags=0):
                    op = clean_price_text(t)
                    if op:
                        old_price = op
                        break
                # або якщо в тексті є валюта + число та теги мають меншу важливість
                if any(cur in (t or "").lower() for cur in ['грн','₴','$','€','uah','usd','eur','руб','₽']) and child != best_tag:
                    op = clean_price_text(t)
                    if op and op != clean_price_text(best_text):
                        old_price = op
                        break
        return clean_price_text(best_text), old_price, best_tag

    # 4) останній шанс — загальний regex по всьому тексту, намагаємось знайти найближчу до ключових слів "грн", "₴" і т.д.
    full_text = soup.get_text(" ", strip=True)
    # шукаємо патерни з валютою
    m = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?)\s*(грн|₴|uah|usd|\$|€|eur|руб|₽)", full_text, flags=re.I)
    if m:
        cp = clean_price_text(m.group(1))
        if cp:
            return cp, None, None
    # загальний захоп першого числа
    m2 = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?)", full_text)
    if m2:
        cp = clean_price_text(m2.group(1))
        if cp:
            return cp, None, None

    return None, None, None

def find_best_name(soup: BeautifulSoup, price_tag: Optional[Tag] = None) -> Optional[str]:
    """Спробувати знайти найімовірнішу назву товару."""
    # 1) meta og:title / twitter:title / meta[name=title] / <title>
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og.get("content").strip()
    tw = soup.find("meta", attrs={"name":"twitter:title"})
    if tw and tw.get("content"):
        return tw.get("content").strip()
    meta_title = soup.find("meta", attrs={"name":"title"})
    if meta_title and meta_title.get("content"):
        return meta_title.get("content").strip()
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        t = title_tag.string.strip()
        # інколи title містить сайт після '—' або '|' — обріжемо
        t = re.split(r"[\|\-—:]", t)[0].strip()
        if len(t) > 3:
            return t

    # 2) itemprop name
    item_name = soup.select("[itemprop='name']")
    for it in item_name:
        txt = tag_text_or_attr(it)
        if txt and len(txt) > 2:
            return txt

    # 3) h1, h2
    h1 = soup.find("h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t and all(kw not in t.lower() for kw in PLACEHOLDER_KEYWORDS):
            return t
    # h2 fallback
    for h in soup.find_all(["h1","h2","h3"]):
        t = h.get_text(" ", strip=True)
        if t and len(t) > 3 and all(kw not in t.lower() for kw in PLACEHOLDER_KEYWORDS):
            # давайте уникально виберемо найкоротший серед заголовків або перший найбільш інформативний
            return t

    # 4) класи що містять 'title', 'product', 'name'
    candidates = []
    for tag in soup.find_all(True, class_=re.compile(r"(title|product|name|goods|item)", flags=re.I)):
        txt = tag_text_or_attr(tag)
        if txt and len(txt) > 3:
            candidates.append(txt)
    if candidates:
        # повертаємо найкоротший/найімовірніший
        candidates_sorted = sorted(candidates, key=lambda x: len(x))
        return candidates_sorted[0]

    # 5) proximity heuristic: якщо є price_tag, шукаємо заголовок поруч
    if price_tag:
        nearby = find_nearby_name(price_tag)
        if nearby:
            return nearby

    # 6) останній фолбек: перші 80 символів великого текстового блоку (інколи опис включає назву)
    body_texts = [t.strip() for t in soup.get_text(" ", strip=True).split("\n") if t.strip()]
    if body_texts:
        for t in body_texts:
            if len(t) > 6 and len(t.split()) < 20:
                # ймовірно це заголовок/заголовок секції
                return t.split(" - ")[0][:200].strip()

    return None

def find_nearby_name(price_tag: Tag) -> Optional[str]:
    """Шукаємо заголовок (h1/h2/title-like) в батьківському блоці ціни."""
    if not price_tag:
        return None
    # піднімемось на декілька рівнів вгору і шукатимемо заголовки
    node = price_tag
    for r in range(4):
        node = node.parent
        if not node:
            break
        # шукаємо h1-h3 в цьому блоці
        for h in node.find_all(["h1","h2","h3"]):
            txt = h.get_text(" ", strip=True)
            if txt and len(txt) > 3:
                return txt
        # також шукаємо елементи з class 'title' або 'product-name'
        t = node.find(True, class_=re.compile(r"(title|product|name|goods|item)", flags=re.I))
        if t:
            txt = tag_text_or_attr(t)
            if txt and len(txt) > 3:
                return txt
    return None

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

        # ld+json (найбільш надійний перший варіант)
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

        # domain-specific selectors (ваша оригінальна логіка)
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
            if not name:
                for sel in domain_cfg.get("name", []):
                    tag = soup.select_one(sel)
                    if tag:
                        txt = tag.get_text(" ", strip=True)
                        if txt:
                            name = txt
                            break
            if not oldPrice:
                for sel in domain_cfg.get("old_price", []):
                    tag = soup.select_one(sel)
                    if tag:
                        txt = tag.get_text(" ", strip=True)
                        op = clean_price_text(txt)
                        if op:
                            oldPrice = op
                            break

        # ---- NEW: потужні фолбеки для будь-якого сайту ----
        # 1) Якщо ще нема currentPrice: розумний пошук по DOM / класам / атрибутам
        price_tag = None
        if not currentPrice:
            cp, op, ptag = find_best_price(soup)
            if cp:
                currentPrice = cp
                price_tag = ptag
            if op and not oldPrice:
                oldPrice = op

        # 2) Якщо є ціна — знайти назву поруч або загальні заголовки
        if not name:
            name_try = find_best_name(soup, price_tag=price_tag)
            if name_try:
                name = name_try

        # 3) Якщо все ще нема — загальні фолбеки: пошук класів product-title, .title, meta tags тощо (ще одна спроба)
        if not name:
            # шукаємо елементи з класами id які можуть містити назву
            candidates = []
            for sel in ["[class*='title']", "[class*='product']", "[id*='title']", "[id*='product']", "[class*='name']"]:
                for tag in soup.select(sel):
                    txt = tag_text_or_attr(tag)
                    if txt and len(txt) > 3 and not any(kw in txt.lower() for kw in PLACEHOLDER_KEYWORDS):
                        candidates.append(txt)
            if candidates:
                # беремо найкоротший (часто це сама назва)
                name = sorted(candidates, key=lambda x: len(x))[0]

        # 4) Фінальний фолбек для ціни: regex по всьому тексту, але ближче до початку / ключових слів
        if not currentPrice:
            full_text = soup.get_text(" ", strip=True)
            m = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?)\s*(грн|₴|uah|usd|\$|€|eur|руб|₽)", full_text, flags=re.I)
            if m:
                cp = clean_price_text(m.group(1))
                if cp:
                    currentPrice = cp
            else:
                m2 = re.search(r"[0-9]+(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?", full_text)
                if m2:
                    cp = clean_price_text(m2.group(0))
                    if cp:
                        currentPrice = cp

        # останні установки значень
        name = name or "Невідома назва"
        currentPrice = currentPrice or "Невідома ціна"
        oldPrice = oldPrice or None
        inStock = bool(inStock if inStock is not None else (currentPrice and currentPrice != "Невідома ціна"))

        # debug prints (видаліть у продакшені якщо не потрібно)
        print("parse_product debug:", {"url": url, "name": name, "currentPrice": currentPrice, "oldPrice": oldPrice, "inStock": inStock})

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
