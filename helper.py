#!/usr/bin/env python3
"""
OLX watcher — моніторинг нових оголошень з фільтрами та керуванням з Telegram.

Встановлення:
    pip install requests playwright
    python -m playwright install chromium

Запуск:
    python olx_watcher_pw.py --pages 3
    python olx_watcher_pw.py --once --pages 12   # разово запамʼятати всю видачу

Команди в чаті бота:
    /help                     довідка
    /status                   поточні налаштування
    /price 10000 20000        змінити діапазон ціни
    /block Київ, Одеса        додати міста в чорний список
    /unblock Київ             прибрати місто зі списку
    /cities                   показати чорний список
    /pets on|off              перевірка параметра "Домашні улюбленці"
    /check                    перевірити просто зараз, не чекаючи паузи
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

# URL БЕЗ параметрів ціни (їх ставить /price) і БЕЗ фільтра тварин:
# оголошення без заповненого поля "Домашні улюбленці" теж мають потрапляти,
# а відсікаємо лише ті, де явно вказано "Ні" — це робить перевірка на сторінці.
SEARCHES = {
    "Оренда 2-3к": (
        "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/"
        "?currency=UAH"
        "&search%5Bfilter_enum_number_of_rooms_string%5D%5B0%5D=dvuhkomnatnye"
        "&search%5Bfilter_enum_number_of_rooms_string%5D%5B1%5D=trehkomnatnye"
        "&search%5Border%5D=created_at%3Adesc"
    ),
}

TG_TOKEN = "8966149413:AAEXWgluzbXu9QfTvJ0io2BZqRpPem4Y6Ys"
TG_CHAT_ID = "443496009"

HERE = Path(__file__).parent
STATE_FILE = HERE / "olx_seen.json"
CONFIG_FILE = HERE / "olx_config.json"
PROFILE_DIR = HERE / "olx_profile"
MAX_MESSAGES_PER_RUN = 10

DEFAULT_CONFIG = {
    "min_price": 10000,
    "max_price": 20000,
    "blocked_cities": ["Київ", "Чернігів", "Одеса", "Харків", "Кривий Ріг"],
    "check_pets": False,
    "only_fresh": True,
    "tg_offset": 0,
}

FRESH_MARKERS = ("Сьогодні", "Сегодня", "Вчора", "Вчера")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------- конфіг


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            log("конфіг пошкоджений, беру типові налаштування")
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def norm_city(s):
    return (s or "").strip().lower().replace("'", "ʼ").replace("’", "ʼ")


# ---------------------------------------------------------------- побудова URL


def build_url(base, cfg, page=1):
    parts = [base]
    if cfg.get("min_price"):
        parts.append(f"&search%5Bfilter_float_price:from%5D={int(cfg['min_price'])}")
    if cfg.get("max_price"):
        parts.append(f"&search%5Bfilter_float_price:to%5D={int(cfg['max_price'])}")
    if page > 1:
        parts.append(f"&page={page}")
    return "".join(parts)


# ------------------------------------------------------------------ браузер


class Browser:
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
        self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        log("браузер запущено" + (" (headless)" if self.headless else ""))

    def goto(self, url, wait_selector=None, timeout=60_000):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except PWTimeout:
            log("  сторінка не завантажилась вчасно")
            return False
        if wait_selector:
            try:
                self.page.wait_for_selector(wait_selector, timeout=20_000)
            except PWTimeout:
                pass
        return True

    def extract_ads(self):
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
            log(f"  не вдалося прочитати DOM: {e}")
            return []

    def ad_pets_value(self, url):
        """Значення параметра 'Домашні улюбленці' зі сторінки оголошення."""
        if not self.goto(url, wait_selector="h4", timeout=45_000):
            return None
        try:
            body = self.page.evaluate("() => document.body.innerText")
        except Exception:
            return None
        m = re.search(r"Домашні улюбленці\s*[:\-]?\s*([^\n]{0,60})", body, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"Домашние животные\s*[:\-]?\s*([^\n]{0,60})", body, re.I)
        return m.group(1).strip() if m else None

    def dump(self, name="olx_debug.html"):
        try:
            (HERE / name).write_text(self.page.content(), encoding="utf-8")
            log(f"  розмітку збережено у {name}")
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


# ------------------------------------------------------------------ фільтри


def city_of(place):
    """'Харків, Салтівський - Сьогодні о 10:45' -> 'Харків'"""
    if not place:
        return ""
    head = re.split(r"\s+-\s+", place)[0]
    return head.split(",")[0].strip()


def price_of(text):
    """'18 000 грн. Договірна' -> 18000. Якщо цифр немає — None."""
    digits = re.sub(r"[^\d]", "", (text or "").split("грн")[0])
    return int(digits) if digits else None


def is_fresh(ad):
    return any(m in (ad.get("place") or "") for m in FRESH_MARKERS)


def city_blocked(ad, cfg):
    city = norm_city(city_of(ad.get("place")))
    return any(norm_city(b) == city for b in cfg.get("blocked_cities", []))


def price_ok(ad, cfg):
    p = price_of(ad.get("price"))
    if p is None:
        return True  # "Договірна" без числа — не відкидаємо
    if cfg.get("min_price") and p < int(cfg["min_price"]):
        return False
    if cfg.get("max_price") and p > int(cfg["max_price"]):
        return False
    return True


PETS_NO_RE = re.compile(r"\bНі\b|\bНет\b", re.I)


# --------------------------------------------------------------- сповіщення


def tg_api(method, **payload):
    token = os.getenv("TG_TOKEN") or TG_TOKEN
    if not token:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=25
        )
        data = r.json()
        if not data.get("ok"):
            log(f"Telegram {method}: {data.get('description')}")
            return None
        return data.get("result")
    except (requests.RequestException, ValueError) as e:
        log(f"Telegram недоступний: {e}")
        return None


def send_telegram(text):
    chat_id = os.getenv("TG_CHAT_ID") or TG_CHAT_ID
    if not chat_id:
        return False
    return (
        tg_api(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        is not None
    )


def notify(search_name, ad):
    esc = html_mod.escape
    parts = [f"<b>{esc(ad['title'])}</b>"]
    if ad.get("price"):
        parts.append(esc(ad["price"]))
    if ad.get("place"):
        parts.append(esc(ad["place"]))
    if ad.get("pets"):
        parts.append(f"🐾 {esc(ad['pets'])}")
    parts.append(ad["url"])
    parts.append(f"<i>{esc(search_name)}</i>")
    if not send_telegram("\n".join(parts)):
        log("  НОВЕ: " + " | ".join([ad["title"], ad.get("price", ""), ad["url"]]))


# ------------------------------------------------------------ команди бота

HELP = """<b>Команди</b>
/status — поточні налаштування
/price 10000 20000 — діапазон ціни
/block Київ, Одеса — додати міста в чорний список
/unblock Київ — прибрати місто
/cities — показати чорний список
/pets on|off — перевіряти параметр «Домашні улюбленці»
/check — перевірити зараз, не чекаючи паузи"""


def status_text(cfg):
    cities = ", ".join(cfg["blocked_cities"]) or "порожній"
    return (
        f"<b>Налаштування</b>\n"
        f"Ціна: {cfg['min_price']}–{cfg['max_price']} грн\n"
        f"Чорний список міст: {cities}\n"
        f"Перевірка тварин: {'увімкнена' if cfg['check_pets'] else 'вимкнена'}\n"
        f"Тільки свіжі (сьогодні/вчора): {'так' if cfg['only_fresh'] else 'ні'}"
    )


def handle_command(text, cfg):
    """Повертає (відповідь, треба_перевірити_зараз)."""
    text = (text or "").strip()
    low = text.lower()

    if low.startswith("/help") or low.startswith("/start"):
        return HELP, False

    if low.startswith("/status"):
        return status_text(cfg), False

    if low.startswith("/check"):
        return "Перевіряю зараз ...", True

    if low.startswith("/price"):
        nums = re.findall(r"\d+", text)
        if len(nums) < 2:
            return "Формат: /price 10000 20000", False
        lo, hi = sorted((int(nums[0]), int(nums[1])))
        cfg["min_price"], cfg["max_price"] = lo, hi
        save_config(cfg)
        return f"Діапазон ціни: {lo}–{hi} грн", False

    if low.startswith("/block"):
        arg = text[len("/block"):].strip()
        if not arg:
            return "Формат: /block Київ, Одеса", False
        added = []
        for c in re.split(r"[,;]| {2,}", arg):
            c = c.strip()
            if c and not any(norm_city(c) == norm_city(b) for b in cfg["blocked_cities"]):
                cfg["blocked_cities"].append(c)
                added.append(c)
        save_config(cfg)
        if not added:
            return "Такі міста вже у списку", False
        return "Додав: " + ", ".join(added), False

    if low.startswith("/unblock"):
        arg = text[len("/unblock"):].strip()
        if not arg:
            return "Формат: /unblock Київ", False
        removed = []
        for c in re.split(r"[,;]| {2,}", arg):
            c = c.strip()
            before = len(cfg["blocked_cities"])
            cfg["blocked_cities"] = [
                b for b in cfg["blocked_cities"] if norm_city(b) != norm_city(c)
            ]
            if len(cfg["blocked_cities"]) < before:
                removed.append(c)
        save_config(cfg)
        if not removed:
            return "Такого міста у списку не було", False
        return "Прибрав: " + ", ".join(removed), False

    if low.startswith("/cities"):
        cities = cfg["blocked_cities"]
        return ("Чорний список: " + ", ".join(cities)) if cities else "Список порожній", False

    if low.startswith("/pets"):
        if "off" in low or "вимк" in low:
            cfg["check_pets"] = False
        elif "on" in low or "увімк" in low:
            cfg["check_pets"] = True
        else:
            return "Формат: /pets on   або   /pets off", False
        save_config(cfg)
        return f"Перевірка тварин: {'увімкнена' if cfg['check_pets'] else 'вимкнена'}", False

    if low.startswith("/"):
        return "Не знаю такої команди. /help — список", False
    return None, False


def poll_commands(cfg):
    """Забирає нові повідомлення з Telegram. True — просили перевірити зараз."""
    updates = tg_api("getUpdates", offset=cfg.get("tg_offset", 0), timeout=0, limit=20)
    if not updates:
        return False
    check_now = False
    for u in updates:
        cfg["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message") or {}
        text = msg.get("text")
        if not text:
            continue
        reply, now = handle_command(text, cfg)
        if now:
            check_now = True
        if reply:
            log(f"  команда: {text.splitlines()[0][:60]}")
            send_telegram(reply)
    save_config(cfg)
    return check_now


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


def collect(browser, base, cfg, pages):
    ads, seen_ids = [], set()
    for n in range(1, pages + 1):
        if not browser.goto(build_url(base, cfg, n), wait_selector='[data-cy="l-card"]'):
            continue
        batch = browser.extract_ads()
        if not batch:
            if n == 1:
                log("  жодного оголошення не розпарсив — капча або зміна верстки")
                browser.dump()
            break
        new_here = [a for a in batch if a["id"] not in seen_ids]
        log(f"  стор. {n}: карток {len(batch)}, унікальних {len(new_here)}")
        if not new_here:
            break
        for a in new_here:
            seen_ids.add(a["id"])
            ads.append(a)
        if n < pages:
            time.sleep(random.uniform(2, 5))
    return ads


def check_all(browser, state, cfg, notify_first=False, pages=1):
    for name, base in SEARCHES.items():
        ads = collect(browser, base, cfg, pages)
        if not ads:
            continue

        known = set(state.get(name, []))
        fresh = [a for a in ads if a["id"] not in known]

        # усе побачене одразу вважаємо відомим, навіть відсіяне
        merged = [a["id"] for a in ads] + state.get(name, [])
        state[name] = list(dict.fromkeys(merged))[:3000]
        save_state(state)

        if name not in known and not known and not notify_first:
            log(f"{name}: перший запуск, запамʼятав {len(ads)} оголошень без сповіщень")
            continue

        stats = {"дата": 0, "місто": 0, "ціна": 0, "тварини": 0}

        def drop(items, keep_fn, reason):
            kept, dropped = [], []
            for a in items:
                (kept if keep_fn(a) else dropped).append(a)
            for a in dropped:
                log(f"    − [{reason}] {a['title'][:55]} | {a.get('price','')} | {a.get('place','')}")
            stats[reason] += len(dropped)
            return kept

        if cfg.get("only_fresh"):
            fresh = drop(fresh, is_fresh, "дата")

        fresh = drop(fresh, lambda a: not city_blocked(a, cfg), "місто")
        fresh = drop(fresh, lambda a: price_ok(a, cfg), "ціна")

        # найдорожча перевірка — в самому кінці, і лише для тих, що дійшли
        passed = []
        if cfg.get("check_pets"):
            for ad in fresh[:MAX_MESSAGES_PER_RUN]:
                value = browser.ad_pets_value(ad["url"])
                if value and PETS_NO_RE.search(value):
                    stats["тварини"] += 1
                    log(f"    − [тварини: {value}] {ad['title'][:55]}")
                    continue
                ad["pets"] = value or ""
                passed.append(ad)
                time.sleep(random.uniform(1.5, 3.5))
        else:
            passed = fresh[:MAX_MESSAGES_PER_RUN]

        for ad in passed:
            notify(name, ad)

        skipped = ", ".join(f"{k}: {v}" for k, v in stats.items() if v)
        log(
            f"{name}: зібрано {len(ads)}, надіслано {len(passed)}"
            + (f" (відсіяно — {skipped})" if skipped else "")
        )


def sleep_with_commands(cfg, seconds):
    """Спимо, але кожні 5 с перевіряємо команди в Telegram."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            if poll_commands(cfg):
                return
        except Exception as e:
            log(f"  помилка опитування Telegram: {e}")
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--min", type=float, default=10, help="мін. пауза, хв")
    ap.add_argument("--max", type=float, default=15, help="макс. пауза, хв")
    ap.add_argument("--notify-first", action="store_true")
    ap.add_argument("--pages", type=int, default=1)
    args = ap.parse_args()

    cfg = load_config()
    save_config(cfg)
    state = load_state()

    log(f"ціна {cfg['min_price']}–{cfg['max_price']}, "
        f"чорний список: {', '.join(cfg['blocked_cities']) or 'порожній'}")

    browser = Browser(headless=args.headless)
    browser.start()

    try:
        while True:
            try:
                poll_commands(cfg)
                check_all(browser, state, cfg, notify_first=args.notify_first, pages=args.pages)
            except Exception as e:
                log(f"несподівана помилка: {type(e).__name__}: {e}")

            if args.once:
                return
            pause = random.uniform(args.min * 60, args.max * 60)
            log(f"сплю {pause / 60:.1f} хв (команди приймаю)")
            sleep_with_commands(cfg, pause)
    finally:
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
