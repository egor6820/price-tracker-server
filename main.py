# main.py
import os
import time
import re
import json
import traceback
import functools
from threading import Semaphore
from typing import Optional, Tuple

import requests
from bs4 import BeautifulSoup, Tag
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---- Config ----
SELECTORS_FILE = "site_selectors.json"
PLAYWRIGHT_ENABLED = os.getenv("ENABLE_PLAYWRIGHT", "true").lower() in ("1", "true", "yes")
PLAYWRIGHT_MAX_PARALLEL = int(os.getenv("PLAYWRIGHT_MAX_PARALLEL", "1"))
PLAYWRIGHT_ATTEMPTS = int(os.getenv("PLAYWRIGHT_ATTEMPTS", "2"))

app = FastAPI()

# CORS для мобільного застосунку
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# Health check
@app.get("/ping")
def ping():
    return {"status": "ok"}

# ---- Models ----
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

# Default Rozetka selectors
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
    "зачекайте", "трохи", "завантаж", "loading", "please wait",
    "очікуйте", "завантаження", "loading...", "завантажу", "шукаємо"
]
CURRENCY_KEYWORDS = ['₴', 'грн', 'uah', '$', 'usd', '€', 'eur', 'руб', '₽']

def contains_currency(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    for cur in CURRENCY_KEYWORDS:
        if cur in t:
            return True
    return bool(re.search(r"\bгрн\b|\buah\b|\busd\b|\beur\b", t))

def clean_price_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    s = str(text).strip().replace("\u00A0", " ").replace("\xa0", " ")
    m = re.search(r"[-+]?[0-9\.\,\s]{1,50}", s)
    if not m:
        return None
    num_s = m.group(0).strip().replace(" ", "")
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
    else:
        normalized = num_s
    normalized = re.sub(r"[^\d\.\-+]", "", normalized)
    try:
        val = float(normalized)
    except Exception:
        return None
    if val <= 0:
        return None
    if val.is_integer():
        return str(int(val))
    else:
        return ("{:.2f}".format(val)).rstrip('0').rstrip('.')

def text_has_digits_and_not_placeholder(text: Optional[str]) -> bool:
    if not text or not re.search(r"\d", text):
        return False
    return all(kw not in text.lower() for kw in PLACEHOLDER_KEYWORDS)

def tag_text_or_attr(tag: Tag) -> str:
    if not tag:
        return ""
    if tag.name == "meta":
        for attr in ("content", "value"):
            if tag.get(attr):
                return str(tag.get(attr))
    for attr in ("data-price", "data-product-price", "content", "value", "title", "alt"):
        if tag.get(attr):
            return str(tag.get(attr))
    return tag.get_text(" ", strip=True) or ""

def is_valid_name_candidate(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if any(kw in t for kw in PLACEHOLDER_KEYWORDS):
        return False
    if re.match(r"^[\.\-\,\s]+$", t):
        return False
    if re.search(r"\.{3,}", t):
        return False
    if len(re.sub(r"\s+", "", t)) < 3:
        return False
    return True

# ---- Playwright setup ----
@app.on_event("startup")
async def startup_playwright():
    app.state.play = None
    app.state.browser = None
    app.state.play_semaphore = None
    if not PLAYWRIGHT_ENABLED:
        print("Playwright disabled by ENV")
        return
    def _start():
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process"
        ])
        return p, browser
    try:
        p, browser = await run_in_threadpool(_start)
        app.state.play = p
        app.state.browser = browser
        app.state.play_semaphore = Semaphore(PLAYWRIGHT_MAX_PARALLEL)
        print("Playwright started; semaphore size =", PLAYWRIGHT_MAX_PARALLEL)
    except Exception as e:
        print("Playwright startup failed:", e)
        traceback.print_exc()
        app.state.play = None
        app.state.browser = None
        app.state.play_semaphore = None

@app.on_event("shutdown")
async def shutdown_playwright():
    def _stop(p, browser):
        try:
            if browser:
                browser.close()
        except: pass
        try:
            if p:
                p.stop()
        except: pass
    p = getattr(app.state, "play", None)
    browser = getattr(app.state, "browser", None)
    if p or browser:
        await run_in_threadpool(functools.partial(_stop, p, browser))
        print("Playwright stopped.")

# ---- Browser extraction ----
def _extract_using_browser_blocking(browser, url: str, domain_cfg: dict | None = None, wait_for_price_sec: int = 20):
    result = {"name": None, "price_text": None, "old_price_text": None, "html": None}
    ctx, page = None, None
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        page.goto(url, timeout=60000)
        try: page.wait_for_load_state('networkidle', timeout=30000)
        except PlaywrightTimeout: pass
        page.wait_for_timeout(500)
        # domain-specific selectors
        if domain_cfg:
            # name
            for sel in domain_cfg.get("name", []):
                el = page.locator(sel).first
                if el.count() == 0: continue
                end_time = time.time() + wait_for_price_sec
                while time.time() < end_time:
                    try:
                        txt = el.inner_text(timeout=2000).strip()
                    except: txt = ""
                    if txt and text_has_digits_and_not_placeholder(txt) is False:
                        result["name"] = txt
                        break
                    time.sleep(0.3)
                if result["name"]: break
            # price
            for sel in domain_cfg.get("price", []):
                el = page.locator(sel).first
                if el.count() == 0: continue
                end_time = time.time() + wait_for_price_sec
                while time.time() < end_time:
                    try:
                        txt = el.inner_text(timeout=2000).strip()
                    except: txt = ""
                    if text_has_digits_and_not_placeholder(txt):
                        result["price_text"] = txt
                        break
                    time.sleep(0.3)
                if result["price_text"]: break
            # old price
            for sel in domain_cfg.get("old_price", []):
                el = page.locator(sel).first
                if el.count() > 0:
                    txt = el.inner_text(timeout=2000).strip()
                    if text_has_digits_and_not_placeholder(txt):
                        result["old_price_text"] = txt
                        break
        result["html"] = page.content()
        return result
    finally:
        if page: page.close()
        if ctx: ctx.close()

async def extract_with_playwright_direct(url: str, domain_cfg: dict | None = None, wait_for_price_sec: int = 20):
    browser = getattr(app.state, "browser", None)
    sem = getattr(app.state, "play_semaphore", None)
    if not browser:
        raise RuntimeError("Playwright browser not available")
    if sem: await run_in_threadpool(sem.acquire)
    try:
        func = functools.partial(_extract_using_browser_blocking, browser, url, domain_cfg, wait_for_price_sec)
        return await run_in_threadpool(func)
    finally:
        if sem: await run_in_threadpool(sem.release)

# ---- Robust fetch ----
def parse_using_requests(url: str, timeout: int = 20):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

async def robust_fetch_html(url: str, domain_cfg: dict | None = None, playwright_attempts: int = PLAYWRIGHT_ATTEMPTS, requests_attempts: int = 2):
    if PLAYWRIGHT_ENABLED and getattr(app.state, "browser", None):
        for attempt in range(playwright_attempts):
            try:
                extracted = await extract_with_playwright_direct(url, domain_cfg=domain_cfg, wait_for_price_sec=20)
                html = extracted.get("html") or ""
                if html and len(html) > 200:
                    return html, extracted
            except Exception as e:
                print(f"Playwright attempt {attempt+1} failed: {e}")
            await run_in_threadpool(time.sleep, 0.5 * (attempt+1))
    last_exc = None
    for i in range(requests_attempts):
        try:
            html = await run_in_threadpool(parse_using_requests, url, 20)
            if html and len(html) > 100:
                return html, {}
        except Exception as e:
            last_exc = e
        await run_in_threadpool(time.sleep, 0.5 * (i+1))
    if last_exc: raise last_exc
    return "", {}

# ---- Parse endpoint ----
@app.post("/parse", response_model=ParseResponse)
async def parse_product(req: ParseRequest):
    url = req.url
    domain_cfg = None
    for domain_key, cfg in SITE_SELECTORS.items():
        if domain_key in url:
            domain_cfg = cfg
            break
    try:
        html, extracted = await robust_fetch_html(url, domain_cfg)
        name, currentPrice, oldPrice, inStock = None, None, None, True
        if extracted.get("name"):
            cand = extracted["name"].strip()
            if is_valid_name_candidate(cand):
                name = cand
        if extracted.get("price_text"):
            cp = clean_price_text(extracted["price_text"])
            if cp:
                currentPrice = cp
        if extracted.get("old_price_text"):
            op = clean_price_text(extracted["old_price_text"])
            if op:
                oldPrice = op
        soup = BeautifulSoup(html, "html.parser")
        # fallback: universal search
        if not currentPrice:
            for sel in ["[itemprop*='price']", "meta[property*='price']"]:
                tag = soup.select_one(sel)
                if tag:
                    cp = clean_price_text(tag_text_or_attr(tag))
                    if cp: currentPrice = cp; break
        if not name:
            for sel in ["[itemprop*='name']", "title", "h1", "h2", "h3"]:
                tag = soup.select_one(sel)
                if tag:
                    txt = tag_text_or_attr(tag)
                    if is_valid_name_candidate(txt):
                        name = txt
                        break
        name = name or "Невідома назва"
        currentPrice = currentPrice or "Невідома ціна"
        return ParseResponse(name=name, currentPrice=currentPrice, oldPrice=oldPrice, inStock=inStock)
    except Exception as e:
        print("Error in parse_product:", e)
        traceback.print_exc()
        return ParseResponse(name="Невідома назва", currentPrice="Невідома ціна", oldPrice=None, inStock=False)
