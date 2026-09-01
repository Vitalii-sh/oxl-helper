#!/usr/bin/env python3
"""
OLX watcher (Playwright) — обхід антибота через справжній браузер.

Встановлення:
    pip install requests playwright
    python -m playwright install chromium

Запуск:
    python olx_watcher_pw.py                 # видимий браузер, цикл 10-15 хв
    python olx_watcher_pw.py --headless      # без вікна (може ловити капчу)
    python olx_watcher_pw.py --once          # одна перевірка
    python olx_watcher_pw.py --notify-first  # слати сповіщення вже на першому запуску

Профіль браузера з cookie зберігається в папці olx_profile поруч зі скриптом,
тому челендж проходиться один раз, а не щоразу.
"""

import argparse
import html as html_mod
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- налаштування

SEARCHES = {
    "Оренда 2-3к, 10-20к грн, можна з котом": (
        "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/"
        "?currency=UAH"
        "&search%5Bfilter_float_price:from%5D=10000"
        "&search%5Bfilter_float_price:to%5D=20000"
        "&search%5Bfilter_enum_number_of_rooms_string%5D%5B0%5D=dvuhkomnatnye"
        "&search%5Bfilter_enum_number_of_rooms_string%5D%5B1%5D=trehkomnatnye"
        "&search%5Bfilter_enum_pets%5D%5B0%5D=yes_cat"
        "&search%5Border%5D=created_at%3Adesc"
    ),
}

# Telegram. Змінні оточення TG_TOKEN / TG_CHAT_ID мають пріоритет.
TG_TOKEN = "8966149413:AAEXWgluzbXu9QfTvJ0io2BZqRpPem4Y6Ys"
TG_CHAT_ID = "443496009"

STATE_FILE = Path(__file__).with_name("olx_seen.json")
PROFILE_DIR = Path(__file__).with_name("olx_profile")
MAX_MESSAGES_PER_RUN = 10

# Слати тільки оголошення з позначкою "Сьогодні" / "Вчора".
# Постав False, якщо хочеш бачити геть усе нове для тебе.
ONLY_FRESH = True
FRESH_MARKERS = ("Сьогодні", "Сегодня", "Вчора", "Вчера")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ браузер


class Browser:
    """Один довгоживучий браузер на весь час роботи скрипта."""

    def __init__(self, headless=False):
        self.headless = headless
        self._pw = None
        self.ctx = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        # прибираємо найочевиднішу ознаку автоматизації
        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        log("браузер запущено" + (" (headless)" if self.headless else ""))

    def get(self, url):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            log("сторінка не завантажилась за 60 с")
            return None
        landed = self.page.url
        if landed.split("#")[0] != url.split("#")[0]:
            log(f"  увага: запит {url}")
            log(f"  відкрилось  {landed}")
        try:
            self.page.wait_for_selector('[data-cy="l-card"]', timeout=20_000)
        except PWTimeout:
            # може бути капча або порожня видача — віддамо що є, розбереться парсер
            pass
        # трохи «людської» поведінки
        try:
            self.page.mouse.wheel(0, random.randint(400, 1200))
            self.page.wait_for_timeout(random.randint(700, 1800))
        except Exception:
            pass
        return self.page.content()

    def extract_ads(self):
        """Читаємо картки прямо з DOM — не залежимо від внутрішнього JSON OLX."""
        js = """
        () => Array.from(document.querySelectorAll('[data-cy="l-card"]')).map(el => {
            const a = el.querySelector('a[href]');
            const href = a ? a.href : '';
            const m = href.match(/-ID([0-9A-Za-z]+)\\.html/);
            const pick = sel => {
                const n = el.querySelector(sel);
                return n ? n.innerText.trim().replace(/\\s+/g, ' ') : '';
            };
            return {
                id: String(el.getAttribute('id') || (m ? m[1] : href)),
                title: pick('h4') || pick('h6') || pick('[data-cy="ad-card-title"]'),
                url: href.split('?')[0],
                price: pick('[data-testid="ad-price"]'),
                place: pick('[data-testid="location-date"]'),
            };
        }).filter(x => x.url && x.id)
        """
        try:
            return self.page.evaluate(js)
        except Exception as e:
            log(f"не вдалося прочитати DOM: {e}")
            return []

    def dump(self, name="olx_debug.html"):
        try:
            Path(__file__).with_name(name).write_text(
                self.page.content(), encoding="utf-8"
            )
            log(f"розмітку збережено у {name}")
        except Exception:
            pass

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


# ------------------------------------------------------- розбір сторінки OLX


def extract_state(page):
    """OLX кладе весь стан сторінки у window.__PRERENDERED_STATE__."""
    idx = page.find("__PRERENDERED_STATE__")
    if idx == -1:
        return None
    i = page.find("=", idx) + 1
    while i < len(page) and page[i] in " \n\r\t":
        i += 1
    if i >= len(page):
        return None

    if page[i] == '"':
        m = re.compile(r'"(?:\\.|[^"\\])*"', re.S).match(page, i)
        if not m:
            return None
        try:
            return json.loads(json.loads(m.group(0)))
        except json.JSONDecodeError:
            return None

    if page[i] == "{":
        depth, in_str, esc = 0, False, False
        for j in range(i, len(page)):
            c = page[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(page[i : j + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def dig(node, *keys):
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def walk_for_ads(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("title"), str) and node.get("id") and node.get("url"):
            out[str(node["id"])] = node
        for v in node.values():
            walk_for_ads(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_for_ads(v, out)


def ads_from_state(state):
    ads = dig(state, "listing", "listing", "ads")
    if isinstance(ads, list) and ads:
        return ads
    found = {}
    walk_for_ads(state, found)
    return list(found.values())


def price_of(ad):
    p = ad.get("price")
    if isinstance(p, dict):
        for k in ("displayValue", "regularPrice", "value"):
            v = p.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and v.get("value"):
                return f"{v['value']} {v.get('currencyCode', '')}".strip()
            if isinstance(v, (int, float)):
                return str(v)
    elif isinstance(p, str):
        return p
    for prm in ad.get("params") or []:
        if isinstance(prm, dict) and prm.get("key") == "price":
            val = prm.get("value")
            if isinstance(val, dict):
                return val.get("label") or val.get("value")
            if isinstance(val, str):
                return val
    return "ціна не вказана"


def normalize(ad):
    url = ad.get("url") or ""
    if url.startswith("/"):
        url = "https://www.olx.ua" + url
    city = dig(ad, "location", "city", "name") or ""
    district = dig(ad, "location", "district", "name") or ""
    place = ", ".join(x for x in (city, district) if x)
    return {
        "id": str(ad.get("id")),
        "title": (ad.get("title") or "").strip(),
        "url": url,
        "price": price_of(ad),
        "place": place,
    }


FALLBACK_RE = re.compile(r'href="(/d/[^"?#]*?-ID([0-9A-Za-z]+)\.html)"')


def parse_page(page):
    state = extract_state(page)
    if state:
        ads = [normalize(a) for a in ads_from_state(state)]
        ads = [a for a in ads if a["id"] and a["url"]]
        if ads:
            return ads
    seen, ads = set(), []
    for path, ad_id in FALLBACK_RE.findall(page):
        if ad_id in seen:
            continue
        seen.add(ad_id)
        ads.append(
            {
                "id": ad_id,
                "title": "нове оголошення",
                "url": "https://www.olx.ua" + path,
                "price": "",
                "place": "",
            }
        )
    return ads


# --------------------------------------------------------------- сповіщення


def send_telegram(text):
    token = os.getenv("TG_TOKEN") or TG_TOKEN
    chat_id = os.getenv("TG_CHAT_ID") or TG_CHAT_ID
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if r.status_code != 200:
            log(f"Telegram відповів {r.status_code}: {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        log(f"Telegram недоступний: {e}")
        return False


def notify(search_name, ad):
    esc = html_mod.escape
    parts = [f"<b>{esc(ad['title'])}</b>"]
    if ad["price"]:
        parts.append(esc(str(ad["price"])))
    if ad["place"]:
        parts.append(esc(ad["place"]))
    parts.append(ad["url"])
    parts.append(f"<i>{esc(search_name)}</i>")
    if not send_telegram("\n".join(parts)):
        log("НОВЕ: " + " | ".join([ad["title"], str(ad["price"]), ad["place"], ad["url"]]))


# ------------------------------------------------------------------- стан


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log("не зміг прочитати файл стану, починаю з чистого")
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ------------------------------------------------------------------ логіка


def page_url(url, n):
    if n <= 1:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}page={n}"


def check_all(browser, state, notify_first=False, pages=1):
    for name, url in SEARCHES.items():
        ads, seen_ids = [], set()
        for n in range(1, pages + 1):
            page = browser.get(page_url(url, n))
            if page is None:
                log(f"{name}: сторінку {n} не отримав")
                continue
            batch = browser.extract_ads() or parse_page(page)
            if not batch:
                if n == 1:
                    log(f"{name}: жодного оголошення не розпарсив — капча або зміна верстки.")
                    browser.dump()
                    if not browser.headless:
                        log("Перевір вікно браузера: якщо там перевірка — пройди її вручну.")
                break  # далі сторінок немає
            new_here = [a for a in batch if a["id"] not in seen_ids]
            log(f"  стор. {n}: карток {len(batch)}, унікальних {len(new_here)}")
            if not new_here:
                break  # OLX повернув ту саму сторінку — далі йти нема сенсу
            for a in new_here:
                seen_ids.add(a["id"])
                ads.append(a)
            if n < pages:
                time.sleep(random.uniform(2, 5))

        if not ads:
            continue
        if pages > 1:
            log(f"{name}: зібрано {len(ads)} оголошень з {pages} стор.")

        known = set(state.get(name, []))
        fresh = [a for a in ads if a["id"] not in known]

        if ONLY_FRESH:
            skipped = len(fresh)
            fresh = [
                a for a in fresh if any(m in (a.get("place") or "") for m in FRESH_MARKERS)
            ]
            skipped -= len(fresh)
            if skipped:
                log(f"{name}: {skipped} старих за датою пропущено")

        if name not in state and not notify_first:
            state[name] = [a["id"] for a in ads]
            save_state(state)
            log(f"{name}: перший запуск, запам'ятав {len(ads)} оголошень без сповіщень")
            continue

        for ad in fresh[:MAX_MESSAGES_PER_RUN]:
            notify(name, ad)
        if len(fresh) > MAX_MESSAGES_PER_RUN:
            log(f"{name}: ще {len(fresh) - MAX_MESSAGES_PER_RUN} нових не показав")

        merged = [a["id"] for a in ads] + state.get(name, [])
        state[name] = list(dict.fromkeys(merged))[:3000]
        save_state(state)
        log(f"{name}: на сторінці {len(ads)}, нових {len(fresh)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="одна перевірка і вихід")
    ap.add_argument("--headless", action="store_true", help="без вікна браузера")
    ap.add_argument("--min", type=float, default=10, help="мін. пауза, хв")
    ap.add_argument("--max", type=float, default=15, help="макс. пауза, хв")
    ap.add_argument("--notify-first", action="store_true")
    ap.add_argument("--pages", type=int, default=1, help="скільки сторінок видачі обходити")
    args = ap.parse_args()

    state = load_state()
    browser = Browser(headless=args.headless)
    browser.start()

    try:
        while True:
            try:
                check_all(browser, state, notify_first=args.notify_first, pages=args.pages)
            except Exception as e:
                log(f"несподівана помилка: {type(e).__name__}: {e}")

            if args.once:
                return
            pause = random.uniform(args.min * 60, args.max * 60)
            log(f"сплю {pause / 60:.1f} хв")
            time.sleep(pause)
    finally:
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
