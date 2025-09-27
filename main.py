# main.py - improved for Render (requests-first, safer Playwright, heuristics, last-good cache)
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
import random
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import urlparse
from threading import Lock

# ---- Configurable: path to JSON with per-site selectors ----
SELECTORS_FILE = "site_selectors.json"

app = FastAPI()

# --- Health check endpoint для UptimeRobot / Render ---
@app.get("/ping")
def ping():
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

# Rotate User-Agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/117.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
]

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
    if not text:
        return None
    s = str(text).strip()
    s = s.replace("\u00A0", " ").replace("\xa0", " ")
    m = re.search(r"[-+]?[0-9\.\,\s\u00A0\u202F]{1,50}", s)
    if not m:
        return None
    num_s = m.group(0).strip()
    num_s = num_s.replace(" ", "")
    if ',' in num_s and '.' in num_s:
        if num_s.rfind(',') > num_s.rfind('.'):
            normalized = num_s.replace('.', '').replace(',', '.')
        else:
            normalized = num_s.replace(',', '')
    elif ',' in num_s:
        if re.search(r",\d{3}(?!\d)", num_s):
            normalized = num_s.replace(',', '')
        else:
            normalized = num_s.replace(',', '.')
    elif '.' in num_s:
        if re.search(r"\.\d{3}(?!\d)", num_s):
            normalized = num_s.replace('.', '')
        else:
            normalized = num_s
    else:
        normalized = num_s
    normalized = re.sub(r"[^\d\.\-+]", "", normalized)
    if not normalized:
        return None
    if normalized.count('.') > 1:
        parts = normalized.split('.')
        normalized = ''.join(parts[:-1]) + '.' + parts[-1]
    try:
        val = float(normalized)
    except Exception:
        return None
    if val <= 0:
        return None
    if val.is_integer():
        return str(int(val))
    else:
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
    score = 0
    t = (text or "").lower()
    if contains_currency(t):
        score += 200
    cls_id = " ".join(filter(None, [(" ".join(tag.get("class")) if tag.get("class") else ""), tag.get("id") or ""]))
    if re.search(r"(price|cost|цiн|ціна|price__|product-price|sale|amount|sum|грн|uah|price--|product__price|price-old|old-price)", cls_id, flags=re.I):
        score += 120
    if tag.get("itemprop") and "price" in tag.get("itemprop").lower():
        score += 100
    if tag.name == "meta" and tag.get("property", "").lower().find("price") != -1:
        score += 80
    words = len((text or "").split())
    if words <= 4:
        score += 10
    if re.search(r"(відгук|reviews|rating|шт|pcs|вага|кг|грам)", t):
        score -= 80
    return score

def find_best_price(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str], Optional[Tag]]:
    for m in soup.find_all("meta"):
        if m.get("property", "").lower() in ("product:price:amount", "og:price:amount"):
            content = m.get("content", "")
            cp = clean_price_text(content)
            if cp:
                return cp, None, m
    item_price = soup.select("[itemprop='price'], [itemprop*='price']")
    for it in item_price:
        text = tag_text_or_attr(it)
        cp = clean_price_text(text)
        if cp:
            return cp, None, it
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
    item_name = soup.select("[itemprop='name'], [itemprop*='name']")
    for it in item_name:
        txt = tag_text_or_attr(it)
        if txt and is_valid_name_candidate(txt):
            return txt
    for h in soup.find_all(["h1","h2","h3"]):
        t = h.get_text(" ", strip=True)
        if t and is_valid_name_candidate(t):
            return t
    candidates = []
    for tag in soup.find_all(True, class_=re.compile(r"(title|product|name|goods|item)", flags=re.I)):
        txt = tag_text_or_attr(tag)
        if txt and is_valid_name_candidate(txt):
            candidates.append(txt)
    if candidates:
        candidates_sorted = sorted(candidates, key=lambda x: len(x))
        return candidates_sorted[0]
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

# ---- New: caches / heuristics for suspicious prices & last-good fallback ----
LAST_GOOD_CACHE: Dict[str, Dict[str, Any]] = {}  # url -> {"ts": float, "name": str, "currentPrice": str, "oldPrice": str, "inStock": bool}
CACHE_LOCK = Lock()
SUSPICIOUS_PRICE_URLS: Dict[str, set] = {}  # price_str -> set(urls)
SUSPICIOUS_THRESHOLD = 3  # if same price appears for >=3 different URLs -> suspicious
LAST_GOOD_TTL = 7 * 24 * 3600  # seconds (7 days)

def domain_from_url(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
        return p.netloc.lower().replace("www.", "")
    except Exception:
        return None

def is_domain_like(text: str) -> bool:
    if not text:
        return False
    text = text.strip().lower()
    # Looks like "example.com" or "example.ua" and no spaces, short
    return bool(re.match(r"^[a-z0-9\-]+(\.[a-z]{2,})+$", text))

def record_suspicious_price(price_str: str, url: str):
    if not price_str:
        return
    urls = SUSPICIOUS_PRICE_URLS.setdefault(price_str, set())
    urls.add(url)

def price_marked_globally_suspicious(price_str: str) -> bool:
    urls = SUSPICIOUS_PRICE_URLS.get(price_str)
    return urls is not None and len(urls) >= SUSPICIOUS_THRESHOLD

def is_suspect_result(url: str, name: Optional[str], price_text: Optional[str], html_snippet: Optional[str] = None) -> bool:
    """
    Return True if the parsed result looks suspicious and should not be trusted.
    Conservative checks:
      - missing/invalid price (can't parse)
      - price parsed but < 20 and no currency context
      - name looks like domain-only or placeholder
      - price value appears across multiple different URLs (global suspicious)
    """
    # Name checks
    dom = domain_from_url(url)
    if name:
        n = name.strip().lower()
        # if name equals or contains domain string -> suspect
        if dom and dom in n:
            return True
        # if name is just domain-like (like 'rozetka.com.ua') -> suspect
        if is_domain_like(n) and len(n.split()) == 1:
            return True

    # Price checks
    if not price_text:
        return True  # missing price is suspicious in many cases

    cp = clean_price_text(price_text)
    if not cp:
        return True

    try:
        num = float(cp)
    except:
        return True

    # global suspicious values (appearing across many URLs)
    if price_marked_globally_suspicious(cp):
        return True

    # if no currency text and numeric value < 20 and not a likely sale price -> suspect
    if not contains_currency(price_text) and num < 20:
        # small numbers might be weight, count, rating etc.
        return True

    # otherwise, looks acceptable
    return False

# ---- Playwright extraction (improved, shorter timeouts, domcontentloaded) ----
def extract_with_playwright_direct(url: str, domain_cfg: dict | None = None, wait_for_price_sec: int = 12):
    result = {"name": None, "price_text": None, "old_price_text": None, "html": None}
    last_exc = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        # make viewport somewhat desktop-like; sometimes mobile view hides prices
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.set_extra_http_headers({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.google.com/"
        })
        try:
            # Prefer faster event: DOMContentLoaded, not full 'load'
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except PlaywrightTimeout:
                # second chance with longer, still bounded
                page.goto(url, timeout=60000, wait_until="domcontentloaded")

            # Try to wait a bit for networkidle but don't block too long
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(300)

            if domain_cfg:
                # name with waiting loop
                for sel in domain_cfg.get("name", []):
                    try:
                        el = page.locator(sel).first
                        if el.count() == 0:
                            continue
                        end_time = time.time() + wait_for_price_sec
                        while time.time() < end_time:
                            try:
                                txt = el.inner_text(timeout=1200).strip()
                            except Exception:
                                txt = ""
                            if txt and all(kw not in txt.lower() for kw in PLACEHOLDER_KEYWORDS):
                                result["name"] = txt
                                break
                            time.sleep(0.25)
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
                                txt = el.inner_text(timeout=1200).strip()
                            except Exception:
                                txt = ""
                            if text_has_digits_and_not_placeholder(txt):
                                result["price_text"] = txt
                                break
                            time.sleep(0.25)
                        if result["price_text"]:
                            break
                    except Exception:
                        continue

                # old price
                for sel in domain_cfg.get("old_price", []):
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            txt = el.inner_text(timeout=1200).strip()
                            if text_has_digits_and_not_placeholder(txt):
                                result["old_price_text"] = txt
                                break
                    except Exception:
                        continue

            result["html"] = page.content()
            # quick blocked detection: title contains domain or obvious captcha text
            try:
                title = page.title()
            except Exception:
                title = ""
            dom = domain_from_url(url)
            lowtitle = (title or "").lower()
            if dom and dom in lowtitle and (not result["price_text"] and not result["name"]):
                # likely a placeholder / blocked page
                browser.close()
                raise Exception("Page looks like domain placeholder / blocked (title contains domain)")

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

# ---- Robust fetch: requests-first then Playwright as fallback + heuristics ----
def robust_fetch_html(url: str, domain_cfg: dict | None = None, playwright_attempts: int = 2, requests_attempts: int = 2):
    start_time = time.time()

    # ---------- 1) Quick requests-first attempt (fast) ----------
    try:
        html = parse_using_requests(url, timeout=8)
        if html and len(html) > 200:
            soup = BeautifulSoup(html, "html.parser")
            # Try ld+json first
            extracted = {}
            for item in extract_ld_json(soup):
                if not extracted.get("name"):
                    cand = item.get("name") or item.get("headline")
                    if cand and is_valid_name_candidate(cand):
                        extracted["name"] = cand
                if not extracted.get("price_text"):
                    p = price_from_ld(item)
                    if p:
                        extracted["price_text"] = p
                if extracted.get("name") and extracted.get("price_text"):
                    break

            # Domain-specific selectors fallback
            if domain_cfg:
                if not extracted.get("price_text"):
                    for sel in domain_cfg.get("price", []):
                        tag = soup.select_one(sel)
                        if tag:
                            if tag.name == "meta":
                                cp_text = tag.get("content", "").strip()
                            else:
                                cp_text = tag.get_text(" ", strip=True)
                            if cp_text and text_has_digits_and_not_placeholder(cp_text):
                                extracted["price_text"] = cp_text
                                break
                if not extracted.get("name"):
                    for sel in domain_cfg.get("name", []):
                        tag = soup.select_one(sel)
                        if tag:
                            txt = tag.get_text(" ", strip=True)
                            if txt and is_valid_name_candidate(txt):
                                extracted["name"] = txt
                                break

            # best-effort fallback
            if not extracted.get("price_text"):
                cp, op, ptag = find_best_price(soup)
                if cp:
                    extracted["price_text"] = cp
                if op:
                    extracted["old_price_text"] = op
            if not extracted.get("name"):
                name_try = find_best_name(soup, price_tag=ptag if 'ptag' in locals() else None)
                if name_try:
                    extracted["name"] = name_try

            # If quick result looks acceptable -> return
            if extracted.get("price_text") or extracted.get("name"):
                # Validate quick result
                suspect = is_suspect_result(url, extracted.get("name"), extracted.get("price_text"), html[:800] if html else None)
                if not suspect:
                    print(f"Requests quick success for {url} in {time.time()-start_time:.2f}s")
                    return html, extracted
                else:
                    # record suspicious price (for later detection)
                    cp_val = clean_price_text(extracted.get("price_text"))
                    if cp_val:
                        record_suspicious_price(cp_val, url)
                    print(f"Requests quick produced suspect result for {url} -> will try Playwright")
    except Exception as e:
        print(f"Requests quick failed for {url}: {e}")

    # ---------- 2) Playwright attempts (only if requests didn't give good result) ----------
    last_exc = None
    for attempt in range(playwright_attempts):
        try:
            extracted = extract_with_playwright_direct(url, domain_cfg=domain_cfg, wait_for_price_sec=12)
            html = extracted.get("html") or ""
            if html and len(html) > 200:
                # basic heuristic: if suspect -> record and possibly fallback
                suspect = is_suspect_result(url, extracted.get("name"), extracted.get("price_text"), html[:800] if html else None)
                if suspect:
                    cp_val = clean_price_text(extracted.get("price_text"))
                    if cp_val:
                        record_suspicious_price(cp_val, url)
                    # If we have last-good cached -> return it immediately to avoid spurious result
                    with CACHE_LOCK:
                        lg = LAST_GOOD_CACHE.get(url)
                        if lg and (time.time() - lg["ts"] <= LAST_GOOD_TTL):
                            print(f"Playwright returned suspect for {url}, returning LAST_GOOD cached result instead.")
                            return lg["html"] if lg.get("html") else html, {
                                "name": lg["name"],
                                "price_text": lg["currentPrice"],
                                "old_price_text": lg.get("oldPrice")
                            }
                    # Otherwise try next attempt or fallback to requests fallback
                    raise Exception("Playwright returned suspect result (likely blocked or placeholder)")
                print(f"Playwright success for {url} in {time.time()-start_time:.2f}s")
                return html, extracted
        except Exception as e:
            last_exc = e
            print(f"Playwright attempt {attempt+1} failed for {url}: {e}")
        time.sleep(random.uniform(0.5, 1.2))

    # ---------- 3) fallback to requests with bigger timeout ----------
    for i in range(requests_attempts):
        try:
            html = parse_using_requests(url, timeout=20)
            if html and len(html) > 200:
                print(f"Requests fallback success for {url} in {time.time()-start_time:.2f}s")
                return html, {}
        except Exception as e:
            last_exc = e
            print(f"Requests attempt {i+1} failed for {url}: {e}")
        time.sleep(random.uniform(0.5, 1.5))

    if last_exc:
        raise last_exc
    return "", {}

# ---- Fallback requests ----
def parse_using_requests(url: str, timeout: int = 25):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

@app.post("/parse", response_model=ParseResponse)
def parse_product(req: ParseRequest):
    start_time = time.time()
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

        # robust fetch (requests-first then Playwright fallback)
        html, extracted = robust_fetch_html(url, domain_cfg=domain_cfg)

        # If playwright/requests returned something in extracted — adopt it carefully
        if isinstance(extracted, dict) and extracted:
            if extracted.get("name"):
                cand = extracted["name"].strip()
                if is_valid_name_candidate(cand):
                    name = cand
            if extracted.get("price_text"):
                cp = clean_price_text(extracted["price_text"])
                if cp:
                    # accept price if currency present OR value >= 20
                    if contains_currency(extracted["price_text"]) or float(cp) >= 20:
                        currentPrice = cp
            if extracted.get("old_price_text"):
                op = clean_price_text(extracted["old_price_text"])
                if op:
                    oldPrice = op

        if not html and not (name or currentPrice):
            return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

        soup = BeautifulSoup(html, "html.parser")

        # ld+json parsing (if still missing)
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

        # Finalize defaults
        name = name or "Невідома назва"
        currentPrice = currentPrice or "Невідома ціна"
        oldPrice = oldPrice or None
        inStock = bool(inStock if inStock is not None else (currentPrice and currentPrice != "Невідома ціна"))

        # Final suspect check: if suspect AND we have last-good cached -> return last-good instead
        suspect_final = is_suspect_result(url, name if name != "Невідома назва" else None, currentPrice if currentPrice != "Невідома ціна" else None, html[:800] if html else None)
        if suspect_final:
            cp_val = None
            try:
                cp_val = clean_price_text(currentPrice)
            except Exception:
                cp_val = None
            if cp_val:
                record_suspicious_price(cp_val, url)
            with CACHE_LOCK:
                lg = LAST_GOOD_CACHE.get(url)
                if lg and (time.time() - lg["ts"] <= LAST_GOOD_TTL):
                    # return cached good result
                    print(f"parse_product: final result for {url} suspicious, returning LAST_GOOD cached result")
                    return ParseResponse(name=lg["name"], currentPrice=lg["currentPrice"], oldPrice=lg.get("oldPrice"), inStock=lg.get("inStock", True))
            # else allow returning the "Невідома ..." or suspicious result to client
            if name == "Невідома назва" and currentPrice == "Невідома ціна":
                return ParseResponse(name="Помилка завантаження", currentPrice="Помилка", oldPrice=None, inStock=False)

        # If we reach here and result is plausible -> update LAST_GOOD cache
        with CACHE_LOCK:
            LAST_GOOD_CACHE[url] = {
                "ts": time.time(),
                "name": name,
                "currentPrice": currentPrice,
                "oldPrice": oldPrice,
                "inStock": inStock,
                "html": html if isinstance(html, str) and len(html) < 20000 else None  # avoid huge html caching
            }

        total_time = time.time() - start_time
        print(f"parse_product debug (time: {total_time:.2f}s):", {"url": url, "name": name, "currentPrice": currentPrice, "oldPrice": oldPrice, "inStock": inStock})

        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)

    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        # If we have last-good, return it instead of unknown to protect users from wrong push
        with CACHE_LOCK:
            lg = LAST_GOOD_CACHE.get(url)
            if lg and (time.time() - lg["ts"] <= LAST_GOOD_TTL):
                print(f"parse_product: exception for {url}, returning LAST_GOOD cached result")
                return ParseResponse(name=lg["name"], currentPrice=lg["currentPrice"], oldPrice=lg.get("oldPrice"), inStock=lg.get("inStock", True))
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
