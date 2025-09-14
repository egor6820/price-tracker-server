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
from typing import Optional, List, Tuple

# ---- Configurable: path to JSON with per-site selectors ----
SELECTORS_FILE = "site_selectors.json"

app = FastAPI()

# --- Health check endpoint для UptimeRobot / Render ---
@app.get("/")
def health_check():
    return {"status": "ok"}

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
# Розширений список плейсхолдерів / текстів-завантаження
PLACEHOLDER_KEYWORDS = [
    "зачекайте", "трохи", "завантаж", "loading", "please wait", "очікуйте", "завантаження",
    "loading...", "завантажу", "шукаємо", "будь ласка зачекайте", "будь ласка, зачекайте"
]

CURRENCY_KEYWORDS = ['₴', 'грн', 'uah', '$', 'usd', '€', 'eur', 'руб', '₽']

def contains_currency(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    for cur in CURRENCY_KEYWORDS:
        if cur in t:
            return True
    if re.search(r"\bгрн\b|\buah\b|\busd\b|\beur\b", t):
        return True
    return False

def clean_price_text(text: Optional[str]) -> Optional[str]:
    """
    Нормалізує рядок з числом з урахуванням тисячних та десяткових роздільників.
    Повертає рядок з крапкою в ролі десятичного роздільника, або None.
    """
    if not text:
        return None
    # Видаляємо непотрібні пробіли/NBSP зверху/знизу, але зберігаємо внутрішні символи для аналізу
    s = str(text).strip()
    s = s.replace("\u00A0", " ").replace("\xa0", " ")
    # Витягнемо перший корисний фрагмент, що містить цифри, коми, крапки та пробіли
    m = re.search(r"[-+]?[0-9\.\,\s\u00A0\u202F]{1,50}", s)
    if not m:
        return None
    num_s = m.group(0).strip()
    # Видаляємо пробіли (звичайні) — зазвичай вони є сепараторами тисяч
    num_s = num_s.replace(" ", "")
    # Тепер у num_s можуть бути коми і/або крапки.
    # Якщо присутні і ',' і '.': правіший роздільник є десятковим
    if ',' in num_s and '.' in num_s:
        if num_s.rfind(',') > num_s.rfind('.'):
            # кома — десятковий, точки — тисячні
            normalized = num_s.replace('.', '').replace(',', '.')
        else:
            # крапка — десятковий, коми — тисячні
            normalized = num_s.replace(',', '')
    elif ',' in num_s:
        # лише коми: якщо кома стоїть перед групами по 3 цифри -> тисячні, інакше — десяткова
        # приклад: 1,280  -> тисячні (початкове число), але 1,28 -> десяткова
        if re.search(r",\d{3}(?!\d)", num_s):
            normalized = num_s.replace(',', '')
        else:
            normalized = num_s.replace(',', '.')
    elif '.' in num_s:
        # лише крапки: схожа логіка
        if re.search(r"\.\d{3}(?!\d)", num_s):
            normalized = num_s.replace('.', '')
        else:
            normalized = num_s
    else:
        normalized = num_s

    # Видалимо все окрім цифр, крапки, мінуса та плюса
    normalized = re.sub(r"[^\d\.\-+]", "", normalized)

    if not normalized:
        return None

    # Якщо є більше однієї крапки — залишимо останню як десяткову, видаливши інші
    if normalized.count('.') > 1:
        parts = normalized.split('.')
        # з'єднаємо всі частини крім останньої, потім додамо останню через крапку
        normalized = ''.join(parts[:-1]) + '.' + parts[-1]

    # Спроба перетворити у float
    try:
        val = float(normalized)
    except Exception:
        return None
    if val <= 0:
        # вважаємо неціною або безглуздою
        return None
    # Повертаємо компактне представлення: без .0 якщо ціле
    if val.is_integer():
        return str(int(val))
    else:
        # обрізати зайві нулі в кінці
        sres = ("{:.2f}".format(val)).rstrip('0').rstrip('.')
        return sres

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

# ---- New heuristic helpers ----

def tag_text_or_attr(tag: Tag) -> str:
    if tag is None:
        return ""
    if tag.name == "meta":
        for attr in ("content", "value"):
            if tag.get(attr):
                return str(tag.get(attr))
        return ""
    for attr in ("data-price", "data-product-price", "content", "value", "title", "alt"):
        if tag.get(attr):
            return str(tag.get(attr))
    return tag.get_text(" ", strip=True) or ""

def score_price_candidate(tag: Tag, text: str) -> int:
    """Оціночна функція — більший бал = кращий кандидат."""
    score = 0
    t = (text or "").lower()

    # великий бонус, якщо є валюта
    if contains_currency(t):
        score += 200

    # клас/id що містить price/cost/грн/ua... — важливий
    cls_id = " ".join(filter(None, [(" ".join(tag.get("class")) if tag.get("class") else ""), tag.get("id") or ""]))
    if re.search(r"(price|cost|цiн|ціна|price__|product-price|sale|amount|sum|грн|uah|price--|product__price|price-old|old-price)", cls_id, flags=re.I):
        score += 120

    # itemprop
    if tag.get("itemprop") and "price" in tag.get("itemprop").lower():
        score += 100

    # meta price
    if tag.name == "meta" and tag.get("property", "").lower().find("price") != -1:
        score += 80

    # короткий текст (типова ціна — короткий)
    words = len((text or "").split())
    if words <= 4:
        score += 10

    # штрафи
    if re.search(r"(відгук|reviews|rating|шт|pcs|вага|кг|грам)", t):
        score -= 80

    return score

def find_best_price(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str], Optional[Tag]]:
    """
    Повертає (currentPrice_str, oldPrice_str, price_tag)
    Пошук з пріоритетами: meta/ld/itemprop -> кандидати з класами/атрибутами -> fallback regex (з преферуванням валюти)
    """
    # 1) Meta с property product:price:amount
    for m in soup.find_all("meta"):
        if m.get("property", "").lower() in ("product:price:amount", "og:price:amount"):
            content = m.get("content", "")
            cp = clean_price_text(content)
            if cp:
                return cp, None, m

    # 2) itemprop=price
    item_price = soup.select("[itemprop='price'], [itemprop*='price']")
    for it in item_price:
        text = tag_text_or_attr(it)
        cp = clean_price_text(text)
        if cp:
            return cp, None, it

    # 3) Збираємо кандидатів з DOM
    candidates = []
    for tag in soup.find_all():
        if tag.name not in ("span","p","div","strong","b","li","a","td","em","meta"):
            continue
        txt = tag_text_or_attr(tag)
        if not txt:
            continue
        if not re.search(r"\d", txt):
            continue
        cp = clean_price_text(txt)
        if not cp:
            continue
        sc = score_price_candidate(tag, txt)
        candidates.append((tag, txt, cp, sc))

    if candidates:
        candidates_sorted = sorted(candidates, key=lambda x: x[3], reverse=True)
        for best_tag, best_text, best_cp, best_score in candidates_sorted:
            try:
                num = float(best_cp)
            except:
                num = None
            has_currency = contains_currency(best_text) or contains_currency(tag_text_or_attr(best_tag))
            cls_id = " ".join(filter(None, [(" ".join(best_tag.get("class")) if best_tag.get("class") else ""), best_tag.get("id") or ""]))
            has_price_class = bool(re.search(r"(price|product-price|price__|цiн|ціна|грн|uah|cost|amount|sum|sale|old-price)", cls_id, flags=re.I))
            if not has_currency:
                if num is not None and num < 20 and not has_price_class:
                    continue
            # пошук old price поруч
            old_price = None
            parent = best_tag.parent
            if parent:
                for child in parent.find_all():
                    if child == best_tag:
                        continue
                    ch_txt = tag_text_or_attr(child)
                    if not ch_txt or not re.search(r"\d", ch_txt):
                        continue
                    op = clean_price_text(ch_txt)
                    if op and op != best_cp and (contains_currency(ch_txt) or re.search(r"(old|previous|strike|product-price__small|product-old|price--old)", " ".join(filter(None, [child.get("class") and " ".join(child.get("class"),), child.get("id") or ""])), flags=re.I) if False else True):
                        old_price = op
                        break
            return best_cp, old_price, best_tag

    # 4) fallback regex: спершу шукаємо з валютою
    full_text = soup.get_text(" ", strip=True)
    m = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?)\s*(грн|₴|uah|usd|\$|€|eur|руб|₽)", full_text, flags=re.I)
    if m:
        cp = clean_price_text(m.group(1))
        if cp:
            return cp, None, None
    m2 = re.search(r"([0-9]{1,3}(?:[ \u00A0][0-9]{3})*(?:[.,][0-9]{1,2})?)", full_text)
    if m2:
        cp = clean_price_text(m2.group(1))
        if cp:
            num = float(cp)
            surrounding = full_text[max(0, m2.start()-40):m2.end()+40]
            if num < 20 and not contains_currency(surrounding):
                return None, None, None
            return cp, None, None

    return None, None, None

def find_best_name(soup: BeautifulSoup, price_tag: Optional[Tag] = None) -> Optional[str]:
    """Спробувати знайти найімовірнішу назву товару (без плейсхолдерів)."""
    # 1) og/twitter/meta title
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        t = og.get("content").strip()
        if is_valid_name_candidate(t):
            return t
    tw = soup.find("meta", attrs={"name":"twitter:title"})
    if tw and tw.get("content"):
        t = tw.get("content").strip()
        if is_valid_name_candidate(t):
            return t
    meta_title = soup.find("meta", attrs={"name":"title"})
    if meta_title and meta_title.get("content"):
        t = meta_title.get("content").strip()
        if is_valid_name_candidate(t):
            return t
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        t = title_tag.string.strip()
        t = re.split(r"[\|\-—:]", t)[0].strip()
        if is_valid_name_candidate(t):
            return t

    # 2) itemprop name
    item_name = soup.select("[itemprop='name'], [itemprop*='name']")
    for it in item_name:
        txt = tag_text_or_attr(it)
        if txt and is_valid_name_candidate(txt):
            return txt

    # 3) h1/h2/h3
    for h in soup.find_all(["h1","h2","h3"]):
        t = h.get_text(" ", strip=True)
        if t and is_valid_name_candidate(t):
            return t

    # 4) класи що містять 'title', 'product', 'name'
    candidates = []
    for tag in soup.find_all(True, class_=re.compile(r"(title|product|name|goods|item)", flags=re.I)):
        txt = tag_text_or_attr(tag)
        if txt and is_valid_name_candidate(txt):
            candidates.append(txt)
    if candidates:
        candidates_sorted = sorted(candidates, key=lambda x: len(x))
        return candidates_sorted[0]

    # 5) proximity heuristic
    if price_tag:
        nearby = find_nearby_name(price_tag)
        if nearby and is_valid_name_candidate(nearby):
            return nearby

    lines = [l.strip() for l in re.split(r"\n+", soup.get_text()) if l.strip()]
    for l in lines:
        if len(l) > 4 and len(l.split()) < 25 and is_valid_name_candidate(l):
            return l.strip()

    return None

def is_valid_name_candidate(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.strip()
    low = t.lower()
    for kw in PLACEHOLDER_KEYWORDS:
        if kw in low:
            return False
    if re.match(r"^[\.\-\,\s]+$", t):
        return False
    if re.search(r"\.{3,}", t):
        return False
    if len(re.sub(r"\s+", "", t)) < 3:
        return False
    return True

def find_nearby_name(price_tag: Tag) -> Optional[str]:
    if not price_tag:
        return None
    node = price_tag
    for _ in range(4):
        node = node.parent
        if not node:
            break
        for h in node.find_all(["h1","h2","h3"]):
            txt = h.get_text(" ", strip=True)
            if is_valid_name_candidate(txt):
                return txt
        t = node.find(True, class_=re.compile(r"(title|product|name|goods|item)", flags=re.I))
        if t:
            txt = tag_text_or_attr(t)
            if is_valid_name_candidate(txt):
                return txt
    return None

# ---- Playwright extraction ----
def extract_with_playwright_direct(url: str, domain_cfg: dict | None = None, wait_for_price_sec: int = 25):
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
            page.goto(url, timeout=140000)
            try:
                page.wait_for_load_state('networkidle', timeout=70000)
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
def parse_using_requests(url: str, timeout: int = 25):
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
                extracted = extract_with_playwright_direct(url, domain_cfg=domain_cfg, wait_for_price_sec=25)
                html = extracted.get("html") or ""
                if extracted.get("name"):
                    cand = extracted["name"].strip()
                    if is_valid_name_candidate(cand):
                        name = cand
                if extracted.get("price_text"):
                    cp = clean_price_text(extracted["price_text"])
                    if cp:
                        if contains_currency(extracted["price_text"]) or float(cp) >= 20:
                            currentPrice = cp
                        else:
                            # якщо селектор домену явно price — можна дозволити, але зараз ми консервативні
                            pass
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
                    cand = item.get("name") or item.get("headline")
                    if cand and is_valid_name_candidate(cand):
                        name = cand
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

        # domain-specific selectors
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
                            txt = tag_text_or_attr(tag)
                            if contains_currency(txt) or float(cp) >= 20 or re.search(r"(price|product-price|грн|uah)", " ".join(filter(None, [tag.get("class") and " ".join(tag.get("class")), tag.get("id") or ""])), flags=re.I):
                                currentPrice = cp
                                break
            if not name:
                for sel in domain_cfg.get("name", []):
                    tag = soup.select_one(sel)
                    if tag:
                        txt = tag.get_text(" ", strip=True)
                        if txt and is_valid_name_candidate(txt):
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

        # powerful fallbacks
        price_tag = None
        if not currentPrice:
            cp, op, ptag = find_best_price(soup)
            if cp:
                currentPrice = cp
                price_tag = ptag
            if op and not oldPrice:
                oldPrice = op

        if not name:
            name_try = find_best_name(soup, price_tag=price_tag)
            if name_try:
                name = name_try

        if not name:
            candidates = []
            for sel in ["[class*='title']", "[class*='product']", "[id*='title']", "[id*='product']", "[class*='name']"]:
                for tag in soup.select(sel):
                    txt = tag_text_or_attr(tag)
                    if txt and is_valid_name_candidate(txt):
                        candidates.append(txt)
            if candidates:
                name = sorted(candidates, key=lambda x: len(x))[0]

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
                        num = float(cp)
                        surrounding = full_text[max(0, m2.start()-40):m2.end()+40]
                        if num < 20 and not contains_currency(surrounding):
                            currentPrice = None
                        else:
                            currentPrice = cp

        name = name or "Невідома назва"
        currentPrice = currentPrice or "Невідома ціна"
        oldPrice = oldPrice or None
        inStock = bool(inStock if inStock is not None else (currentPrice and currentPrice != "Невідома ціна"))

        print("parse_product debug:", {"url": url, "name": name, "currentPrice": currentPrice, "oldPrice": oldPrice, "inStock": inStock})

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)

