# -*- coding: utf-8 -*-
"""
Парсит содержимое всех кейсов с cs2case.win: для каждого кейса — список
скинов с картинкой, ценой, шансом дропа и редкостью.

Данные лежат в HTML страницы кейса, в скрытом <div id="caseContentsData"> —
готовый JSON, никакого JS-рендеринга. Индекс кейсов берётся с главной
(ссылки /cases/view/<id> в карточках).

Картинки складываются в общий пул skins/<hash>.webp (имя файла на сайте —
content-хеш, поэтому дубликаты между кейсами не качаются повторно), а в
by-case/<категория>/<кейс>/ раскладываются жёсткие ссылки с читаемыми именами.

Запуск:  python3 parse_skins.py            # всё
         python3 parse_skins.py --meta     # только метаданные, без картинок
         python3 parse_skins.py --cat "Стандартные Кейсы"
"""

import argparse
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "https://cs2case.win"
OUT = os.path.dirname(os.path.abspath(__file__))
SKINS_DIR = os.path.join(OUT, "skins")
BYCASE_DIR = os.path.join(OUT, "by-case")
WORKERS = 8
RETRIES = 3

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


def fetch(url, binary=False):
    """GET с ретраями и экспоненциальной паузой."""
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


# ── индекс кейсов ────────────────────────────────────────────────────────
CARD_RE = re.compile(
    r'<a\s+href="(/cases/view/(\d+))"[^>]*'
    r'data-category="([^"]*)"[^>]*data-name="([^"]*)"'
    r'(?:[^>]*data-price_rub="([^"]*)")?'
    r'(?:[^>]*data-price_usd="([^"]*)")?',
)


def case_index():
    page = fetch(BASE + "/")
    out, seen = [], set()
    for m in CARD_RE.finditer(page):
        cid = m.group(2)
        if cid in seen:
            continue
        seen.add(cid)
        out.append(
            {
                "id": int(cid),
                "url": BASE + m.group(1),
                "category": m.group(3),
                "name": m.group(4),
                "price_rub": _num(m.group(5)),
                "price_usd": _num(m.group(6)),
            }
        )
    return out


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ── содержимое кейса ─────────────────────────────────────────────────────
CONTENTS_RE = re.compile(
    r'id=["\']caseContentsData["\'][^>]*>(.*?)</div>', re.S
)


def case_contents(case):
    page = fetch(case["url"])
    m = CONTENTS_RE.search(page)
    if not m:
        raise RuntimeError("нет блока caseContentsData (разметка изменилась?)")
    items = json.loads(html.unescape(m.group(1)))
    for it in items:
        it["image"] = it["image"].replace("\\/", "/")
    return items


# ── файловые имена ───────────────────────────────────────────────────────
def safe(s, limit=90):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit] or "unnamed"


def download_image(url):
    """Качает картинку в общий пул. Возвращает (имя_файла, скачано_ли)."""
    fn = url.split("/")[-1].split("?")[0]
    path = os.path.join(SKINS_DIR, fn)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return fn, False
    data = fetch(url, binary=True)
    if not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        raise RuntimeError(f"{fn}: не webp ({len(data)} B)")
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return fn, True


def link_into_case(case, items):
    """Жёсткие ссылки с читаемыми именами: by-case/<кат>/<кейс>/NN-name.webp"""
    d = os.path.join(BYCASE_DIR, safe(case["category"], 60), safe(case["name"], 60))
    os.makedirs(d, exist_ok=True)
    ordered = sorted(items, key=lambda x: -(x.get("price_usd") or 0))
    for i, it in enumerate(ordered, 1):
        src = os.path.join(SKINS_DIR, it["file"])
        if not os.path.exists(src):
            continue
        dst = os.path.join(d, f"{i:03d}-{safe(it['market_hash_name'])}.webp")
        if os.path.exists(dst):
            continue
        try:
            os.link(src, dst)
        except OSError:
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())


# ── основной проход ──────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", action="store_true", help="только JSON, без картинок")
    ap.add_argument("--cat", help="только эта категория")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()

    os.makedirs(SKINS_DIR, exist_ok=True)
    log("Загружаю индекс кейсов…")
    cases = case_index()
    if args.cat:
        cases = [c for c in cases if c["category"] == args.cat]
    log(f"Кейсов: {len(cases)}")
    if not cases:
        sys.exit("Ничего не найдено — проверь --cat")

    # шаг 1: метаданные всех кейсов
    failed_cases = []
    done = [0]

    def load(c):
        try:
            c["contents"] = case_contents(c)
            for it in c["contents"]:
                it["file"] = it["image"].split("/")[-1]
            with _print_lock:
                done[0] += 1
                n = done[0]
            log(f"[{n:>3}/{len(cases)}] {len(c['contents']):>4} поз.  "
                f"{c['category']} / {c['name']}")
        except Exception as e:
            c["contents"] = []
            failed_cases.append((c["name"], str(e)))
            log(f"[FAIL] {c['name']}: {e}")
        return c

    with cf.ThreadPoolExecutor(args.workers) as ex:
        cases = list(ex.map(load, cases))

    all_items = {}
    for c in cases:
        for it in c["contents"]:
            all_items.setdefault(it["file"], it["image"])
    total_pos = sum(len(c["contents"]) for c in cases)
    log(f"\nПозиций всего: {total_pos}, уникальных картинок: {len(all_items)}")

    with open(os.path.join(OUT, "cases.json"), "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
    log("Метаданные → cases.json")

    if args.meta:
        return

    # шаг 2: картинки
    log(f"\nКачаю картинки ({len(all_items)})…")
    stats = {"new": 0, "skip": 0}
    failed_imgs = []
    urls = list(all_items.values())

    def grab(u):
        try:
            _, fresh = download_image(u)
            k = "new" if fresh else "skip"
            with _print_lock:
                stats[k] += 1
                n = stats["new"] + stats["skip"]
                if n % 250 == 0:
                    log(f"  … {n}/{len(urls)}")
        except Exception as e:
            failed_imgs.append(str(e))

    with cf.ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(grab, urls))

    log(f"Скачано новых: {stats['new']}, уже были: {stats['skip']}, "
        f"ошибок: {len(failed_imgs)}")

    # шаг 3: раскладка по кейсам
    log("\nРаскладываю по кейсам…")
    for c in cases:
        if c["contents"]:
            link_into_case(c, c["contents"])
    log(f"Готово → by-case/")

    if failed_cases:
        log(f"\nНе прочитались кейсы ({len(failed_cases)}):")
        for n, e in failed_cases[:20]:
            log(f"  {n}: {e}")
    if failed_imgs:
        log(f"\nНе скачались картинки ({len(failed_imgs)}):")
        for e in failed_imgs[:20]:
            log(f"  {e}")


if __name__ == "__main__":
    main()
