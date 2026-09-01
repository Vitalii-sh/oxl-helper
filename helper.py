#!/usr/bin/env python3
"""
OLX watcher — перевіряє збережені пошуки на olx.ua і повідомляє про нові оголошення.

Залежності:  pip install requests

Змінні оточення для сповіщень у Telegram (опційно):
    TG_TOKEN    — токен бота від @BotFather
    TG_CHAT_ID  — твій chat_id

Запуск:
    python3 olx_watcher.py            # безкінечний цикл з паузою 10–15 хв
    python3 olx_watcher.py --once     # одна перевірка (для cron)
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

# ---------------------------------------------------------------- налаштування

# Додавай сюди скільки завгодно пошуків: "назва": "url"
# Порада: додай у кінець URL &search%5Border%5D=created_at%3Adesc,
# щоб найновіші були на першій сторінці.
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

# Telegram. Значення нижче використовуються, якщо не задані змінні оточення
# TG_TOKEN / TG_CHAT_ID — вони мають пріоритет.
TG_TOKEN = ""
TG_CHAT_ID = ""

STATE_FILE = Path(__file__).with_name("olx_seen.json")
MAX_MESSAGES_PER_RUN = 10  # захист від спаму, якщо стан загубився

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ мережа


def fetch(session, url, attempts=3):
    for i in range(attempts):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            log(f"HTTP {r.status_code} для {url}")
        except requests.RequestException as e:
            log(f"помилка запиту: {e}")
        time.sleep(5 * (i + 1))
    return None


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
    """Запасний варіант: рекурсивно шукаємо об'єкти, схожі на оголошення."""
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
        "created": ad.get("created_time") or ad.get("last_refresh_time") or "",
    }


FALLBACK_RE = re.compile(r'href="(/d/[^"?#]*?-ID([0-9A-Za-z]+)\.html)"')


def parse_page(page):
    state = extract_state(page)
    if state:
        ads = [normalize(a) for a in ads_from_state(state)]
        ads = [a for a in ads if a["id"] and a["url"]]
        if ads:
            return ads
    # якщо OLX змінив розмітку — хоча б посилання витягнемо
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
                "created": "",
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
    text = "\n".join(parts)
    if not send_telegram(text):
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


def check_all(session, state, notify_first=False):
    for name, url in SEARCHES.items():
        page = fetch(session, url)
        if page is None:
            log(f"{name}: сторінку не отримав, пропускаю цей цикл")
            continue

        ads = parse_page(page)
        if not ads:
            log(f"{name}: жодного оголошення не розпарсив (можливо, капча або зміна верстки)")
            continue

        known = set(state.get(name, []))
        fresh = [a for a in ads if a["id"] not in known]

        if name not in state and not notify_first:
            state[name] = [a["id"] for a in ads]
            save_state(state)
            log(f"{name}: перший запуск, запам'ятав {len(ads)} оголошень без сповіщень")
            continue

        for ad in fresh[:MAX_MESSAGES_PER_RUN]:
            notify(name, ad)
        if len(fresh) > MAX_MESSAGES_PER_RUN:
            log(f"{name}: ще {len(fresh) - MAX_MESSAGES_PER_RUN} нових не показав")

        # зберігаємо останні 500 ID, щоб файл не ріс безкінечно
        merged = [a["id"] for a in ads] + state.get(name, [])
        state[name] = list(dict.fromkeys(merged))[:500]
        save_state(state)
        log(f"{name}: на сторінці {len(ads)}, нових {len(fresh)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="одна перевірка і вихід")
    ap.add_argument("--min", type=float, default=10, help="мін. пауза, хв")
    ap.add_argument("--max", type=float, default=15, help="макс. пауза, хв")
    ap.add_argument(
        "--notify-first",
        action="store_true",
        help="слати сповіщення вже на першому запуску",
    )
    args = ap.parse_args()

    session = requests.Session()
    state = load_state()

    while True:
        try:
            check_all(session, state, notify_first=args.notify_first)
        except Exception as e:  # цикл не має падати через одну помилку
            log(f"несподівана помилка: {type(e).__name__}: {e}")

        if args.once:
            return
        pause = random.uniform(args.min * 60, args.max * 60)
        log(f"сплю {pause / 60:.1f} хв")
        time.sleep(pause)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
