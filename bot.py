import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import requests
from curl_cffi import requests as cffi

# ================== НАСТРОЙКИ ==================
INTERVAL_SECONDS = 60      # как часто проверять цены
RUN_MINUTES = 55           # сколько минут работает один запуск на GitHub
PAGES_PER_CATEGORY = 3     # сколько страниц раздела смотреть
DEFAULT_PERCENT = 30       # порог падения цены по умолчанию, %
# ===============================================

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = str(os.environ["TG_CHAT_ID"])
TG = f"https://api.telegram.org/bot{TG_TOKEN}/"
API = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url="
DB_FILE = Path("prices.json")
CFG_FILE = Path("config.json")

HELP = (
    "Команды:\n"
    "/add ссылка — добавить раздел Ozon (можно ссылку из приложения)\n"
    "/list — список разделов\n"
    "/del номер — удалить раздел\n"
    "/percent 70 — присылать при падении цены от 70%\n"
    "/minprice 500 — только товары, которые раньше стоили от 500 ₽\n"
    "/pages 10 — сколько страниц каждого раздела проверять\n\n"
    "Как добавить раздел: открой его на Ozon, нажми «Поделиться», "
    "скопируй ссылку и отправь мне: /add ссылка"
)


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def tg(method, **params):
    try:
        return requests.post(TG + method, json=params, timeout=40).json()
    except Exception as e:
        print("Telegram error:", e)
        return {}


def say(text, preview=False):
    tg("sendMessage", chat_id=TG_CHAT_ID, text=text, parse_mode="HTML",
       disable_web_page_preview=not preview)


# ---------- Управление из Telegram ----------
def normalize_link(text):
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    url = m.group(0)
    if "ozon.ru" not in urlparse(url).netloc:
        return None
    if urlparse(url).path.startswith("/t/"):  # короткая ссылка из приложения
        try:
            url = cffi.get(url, impersonate="chrome", timeout=30).url
        except Exception:
            return None
    u = urlparse(url)
    if "ozon.ru" not in u.netloc:
        return None
    drop = {"page", "at", "from_sku", "abt_att", "origin_referer", "miniapp"}
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in drop]
    return u.path + ("?" + urlencode(q) if q else "")


def handle_commands(cfg):
    res = tg("getUpdates", offset=cfg["offset"], timeout=0)
    for upd in res.get("result", []):
        cfg["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TG_CHAT_ID:
            continue
        cmd, _, arg = (msg.get("text") or "").strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()

        if cmd in ("/start", "/help"):
            say(HELP)
        elif cmd == "/add":
            path = normalize_link(arg)
            if not path:
                say("Не вижу ссылку на ozon.ru. Пример: /add https://www.ozon.ru/category/elektronika-15500/")
            elif path in cfg["categories"]:
                say("Этот раздел уже добавлен.")
            else:
                cfg["categories"].append(path)
                say(f"Добавил (№{len(cfg['categories'])}). Первый круг только запомнит цены, "
                    "уведомления придут при следующих падениях.")
        elif cmd == "/list":
            if cfg["categories"]:
                rows = "\n".join(f"{i}. {html.escape(c)}" for i, c in enumerate(cfg["categories"], 1))
                say(f"Разделы:\n{rows}\n\nПорог: {cfg['percent']}%")
            else:
                say("Разделов пока нет. Добавь: /add ссылка")
        elif cmd == "/del":
            try:
                removed = cfg["categories"].pop(int(arg) - 1)
                say(f"Удалил: {html.escape(removed)}")
            except Exception:
                say("Укажи номер из /list, например: /del 1")
        elif cmd == "/percent":
            try:
                p = int(arg)
                assert 1 <= p <= 95
                cfg["percent"] = p
                say(f"Порог: {p}%")
            except Exception:
                say("Укажи число от 1 до 95, например: /percent 40")
        elif cmd == "/minprice":
            try:
                cfg["minprice"] = max(0, int(arg))
                say(f"Учитываю только товары, которые раньше стоили от {cfg['minprice']} ₽")
            except Exception:
                say("Укажи число, например: /minprice 500")
        elif cmd == "/pages":
            try:
                n = int(arg)
                assert 1 <= n <= 30
                cfg["pages"] = n
                say(f"Проверяю по {n} стр. каждого раздела")
            except Exception:
                say("Укажи число от 1 до 30, например: /pages 10")


# ---------- Парсинг Ozon ----------
def to_int(text):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def fetch_page(path, page):
    if page > 1:
        path += ("&" if "?" in path else "?") + f"page={page}"
    r = cffi.get(API + quote(path, safe=""), impersonate="chrome", timeout=30)
    r.raise_for_status()
    return r.json()


def parse_items(data):
    items = []
    for key, raw in (data.get("widgetStates") or {}).items():
        if not key.startswith("tileGrid"):
            continue
        try:
            grid = json.loads(raw)
        except Exception:
            continue
        for it in grid.get("items", []):
            link = (it.get("action") or {}).get("link", "")
            m = re.search(r"/product/[^?]*?-(\d+)/", link)
            if not m:
                continue
            title, price = None, None
            for atom in it.get("mainState", []):
                if atom.get("type") == "priceV2":
                    for p in atom["priceV2"].get("price", []):
                        if p.get("textStyle") == "PRICE":
                            price = to_int(p.get("text"))
                elif atom.get("type") == "textAtom" and not title:
                    title = atom["textAtom"].get("text")
            if price and title:
                items.append({"id": m.group(1), "title": title, "price": price,
                              "url": f"https://www.ozon.ru/product/{m.group(1)}/"})
    return items


def send_drop(item, old):
    fmt = lambda n: f"{n:,}".replace(",", " ")
    pct = round((1 - item["price"] / old) * 100)
    say(f"🚨🚨 <b>ЦЕНА УПАЛА НА {pct}%!!!</b> 🚨🚨\n\n"
        f"<b>{html.escape(item['title'])}</b>\n"
        f"<s>{fmt(old)} ₽</s> → <b>{fmt(item['price'])} ₽</b>\n"
        f"{item['url']}", preview=True)
    time.sleep(1)


def check_once(db, cfg):
    sent = 0
    for path in cfg["categories"]:
        for page in range(1, cfg["pages"] + 1):
            try:
                items = parse_items(fetch_page(path, page))
            except Exception as e:
                print(f"Ошибка {path} стр.{page}: {e}")
                break
            print(f"{path} стр.{page}: {len(items)} товаров")
            if not items:
                break
            for it in items:
                old = db.get(it["id"])
                if (old and old >= cfg["minprice"]
                        and it["price"] <= old * (1 - cfg["percent"] / 100)):
                    send_drop(it, old)
                    sent += 1
                db[it["id"]] = it["price"]
            time.sleep(1)
    return sent


def main():
    db = load(DB_FILE, {})
    cfg = load(CFG_FILE, {})
    cfg.setdefault("categories", [])
    cfg.setdefault("percent", DEFAULT_PERCENT)
    cfg.setdefault("offset", 0)
    cfg.setdefault("minprice", 0)
    cfg.setdefault("pages", PAGES_PER_CATEGORY)

    end = time.time() + RUN_MINUTES * 60
    while True:
        started = time.time()
        handle_commands(cfg)
        sent = check_once(db, cfg)
        DB_FILE.write_text(json.dumps(db))
        CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False))
        print(f"Круг завершён. Отправлено: {sent}")
        if time.time() + INTERVAL_SECONDS >= end:
            break
        time.sleep(max(5, INTERVAL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
