"""Pozemky Prešov – denný tracker stavebných pozemkov.

Stiahne inzeráty zo zdrojov v config.yaml (Bazoš, Nehnuteľnosti.sk, realitky), vyfiltruje ich podľa config.yaml,
uloží do docs/listings.json (z toho číta prehľad na GitHub Pages)
a pošle denný e-mail s novinkami.

Spustenie:  python scraper.py            (ostrý beh)
            python scraper.py --dry-run  (nič neposiela, e-mail uloží do email_preview.html)
            python scraper.py --fixtures tests/  (offline test na uložených HTML)
"""
import argparse, json, os, re, smtplib, sys, time, unicodedata
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from urllib.parse import urljoin

import requests, yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
DATA = ROOT / "docs" / "listings.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

SOURCES = {}  # naplní sa z config.yaml


def load_sources(cfg):
    for src in cfg["sources"]:
        SOURCES[src["name"]] = {**src, "id": re.compile(src["id_regex"])}


# ---------- pomocné ----------
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def num(s):
    s = re.sub(r"[^\d,]", "", s or "").replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def fetch(url, timeout=30):
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "sk"}, timeout=timeout)
    r.raise_for_status()
    return r.text


# ---------- parsovanie ----------
def extract_cards(html, source):
    """Nájde inzeráty bez závislosti na CSS triedach: pre každý odkaz na detail
    vystúpi v DOM na najväčší blok, ktorý obsahuje len tento jeden inzerát a cenu."""
    cfg = SOURCES[source]
    soup = BeautifulSoup(html, "html.parser")
    cards = {}
    for a in soup.find_all("a", href=True):
        m = cfg["id"].search(a["href"])
        if not m or m.group(1) in cards:
            continue
        node, best = a, None
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            node = node.parent
            ids = {mm.group(1) for x in node.find_all("a", href=True) if (mm := cfg["id"].search(x["href"]))}
            if len(ids) > 1:
                break
            if "€" in node.get_text() or "dohodou" in norm(node.get_text()):
                best = node
        if best is not None:
            cards[m.group(1)] = parse_card(best, urljoin(cfg["base"], a["href"]), source, m.group(1))
    return [c for c in cards.values() if c]


AREA_RE = re.compile(r"(\d[\d\s\u00a0.]*\d|\d+)\s*(?:m2|m²|m\^2|metrov)", re.I)


def parse_card(el, url, source, lid):
    lines = [l.strip() for l in el.get_text("\n").split("\n") if l.strip()]
    h = el.find(["h2", "h3"])
    title = h.get_text(" ", strip=True) if h else max((a.get_text(" ", strip=True) for a in el.find_all("a")), key=len, default="")
    if not title:
        return None
    price = ppm2 = None
    for l in lines:
        m = re.search(r"(\d[\d\s\u00a0,.]*)\s*€\s*/\s*m", l)
        if m:
            ppm2 = ppm2 or num(m.group(1).replace(".", ""))
        elif re.fullmatch(r"[\d\s\u00a0.]+€", l) and price is None:
            price = num(l)
    desc = max(lines, key=len) if lines else ""
    area = None
    for l in lines:  # nehnutelnosti má výmeru na samostatnom riadku
        if re.fullmatch(r"\d[\d\s\u00a0]*\s*m²", l):
            area = num(l)
            break
    full = " ".join(lines)
    if area is None:
        m = AREA_RE.search(title) or AREA_RE.search(full)
        if m:
            area = num(m.group(1).replace(".", ""))
    location = ""
    if source != "bazos":
        location = next((l for l in lines if "okres" in norm(l)), "")
    else:
        for i, l in enumerate(lines):
            if re.fullmatch(r"\d{3}\s?\d{2}", l):
                location = f"{lines[i-1]} {l}" if i else l
                break
    posted = None
    m = re.search(r"\[(\d{1,2})\.(\d{1,2})\.\s*(\d{4})\]", el.get_text(" "))
    if m:
        posted = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return {"id": f"{source}:{lid}", "source": source, "url": url, "title": title,
            "desc": desc[:400], "text": full[:1500], "location": location, "price": price, "ppm2": ppm2,
            "area": area, "posted": posted}


# ---------- filtrovanie ----------
def classify(item, cfg):
    """Vráti (zóna, obec) alebo (None, dôvod) ak sa má inzerát zahodiť."""
    title = norm(item["title"])
    # bazoš má v lokalite len región ("Prešov 082 21"), na obec sa nehodí
    # bazoš má v lokalite len región ("Prešov 082 21"), na obec sa nehodí
    loc = re.sub(r"okres \w+", "", norm(item["location"])) if item["source"] != "bazos" else ""
    desc = norm(item["desc"])

    if re.search(cfg["title_reject"], title) and not re.search(cfg["must_have"], title):
        return None, "typ pozemku"
    if not re.search(cfg["must_have"], title + " " + norm(item.get("text") or desc)):
        return None, "nie je stavebný"

    for text in (title, loc):
        for obec, pat in cfg["blacklist"].items():
            if re.search(pat, text):
                return None, f"blacklist {obec}"

    # 1) titulok, 2) lokalita, 3) popis – Prešov len ak nie je spomenutá iná obec
    for text in (title, loc, desc):
        hits = [(z["name"], o) for z in cfg["zones"] for o, p in z["obce"].items() if re.search(p, text)]
        non_po = [h for h in hits if h[1] != "Prešov"]
        if non_po:
            return non_po[0]
        if hits and text is not desc and not re.search(r"\bv obci\b|\bobec\b|\bkm od\b", title):
            return hits[0]
    return "Na posúdenie", "neznáma obec"


def finalize_price(item):
    """Ak je cena zjavne za m² (bazoš: '92 €'), dopočíta odhad."""
    item["price_estimated"] = False
    p, a = item.get("price"), item.get("area")
    if p and p < 1000:
        item["ppm2"] = item.get("ppm2") or p
        item["price"] = round(p * a) if a else None
        item["price_estimated"] = bool(a)
    if item.get("price") and a and not item.get("ppm2"):
        item["ppm2"] = round(item["price"] / a, 1)
    return item


def passes_numbers(item, cfg):
    if item.get("price") and item["price"] > cfg["max_price"]:
        return False
    if item.get("area") and item["area"] < cfg["min_area"]:
        return False
    return True


def find_duplicate(item, store):
    for o in store.values():
        if o["id"] == item["id"] or o["source"] == item["source"] or o.get("dup_of"):
            continue
        if (o.get("obec") == item.get("obec") and o.get("area") and item.get("area")
                and abs(o["area"] - item["area"]) <= 5
                and abs((o.get("price") or 0) - (item.get("price") or 0)) <= 1000):
            return o["id"]
    return None


# ---------- e-mail ----------
def fmt(n, unit=""):
    return f"{n:,.0f}".replace(",", " ") + unit if n else "?"


def build_email(new, health, total_active, pages_url, drops=()):
    today = date.today().strftime("%d.%m.%Y")
    when = "ráno" if datetime.now(timezone.utc).hour < 12 else "večer"
    order = ["Blízko Prešova", "Sabinov – PNV", "Na posúdenie"]
    parts = [f"<h2 style='margin:0 0 4px'>Pozemky – {today}</h2>",
             f"<p style='color:#555;margin:0 0 16px'>{len(new)} nových, {total_active} aktívnych celkom"
             + (f" · <a href='{pages_url}'>otvoriť prehľad</a>" if pages_url else "") + "</p>"]
    for zone in order:
        items = sorted([i for i in new if i["zone"] == zone], key=lambda x: x.get("price") or 9e9)
        if not items:
            continue
        parts.append(f"<h3 style='border-bottom:2px solid #2E6B4F;padding-bottom:4px'>{zone} ({len(items)})</h3>")
        for i in items:
            est = " (odhad)" if i.get("price_estimated") else ""
            parts.append(
                "<div style='margin:0 0 14px'>"
                f"<a href='{escape(i['url'])}' style='font-weight:bold;color:#1E2B24'>{escape(i['title'])}</a><br>"
                f"<b>{fmt(i.get('price'), ' €')}{est}</b> · {fmt(i.get('area'), ' m²')} · "
                f"{fmt(i.get('ppm2'), ' €/m²')} · {escape(i.get('obec') or '')} · {i['source']}<br>"
                f"<span style='color:#666;font-size:13px'>{escape(i['desc'][:220])}…</span></div>")
    if drops:
        parts.append("<h3 style='border-bottom:2px solid #D9A21B;padding-bottom:4px'>Znížená cena</h3>")
        for i in drops:
            parts.append(f"<div style='margin:0 0 8px'><a href='{escape(i['url'])}'>{escape(i['title'])}</a>: "
                         f"<s>{fmt(i['old_price'], ' €')}</s> → <b>{fmt(i['price'], ' €')}</b></div>")
    if not new and not drops:
        parts.append("<p>Dnes nič nové.</p>")
    bad = [s for s, n in health.items() if n == 0]
    if bad:
        parts.append(f"<p style='color:#b00'>⚠ Zdroj {', '.join(bad)} dnes nevrátil žiadne inzeráty – "
                     "web sa asi zmenil a parser treba opraviť.</p>")
    subject = f"Pozemky PO {when} – {len(new)} nových ({today})" + (" ⚠" if bad else "")
    return subject, "<div style='font-family:Arial,sans-serif;max-width:640px'>" + "".join(parts) + "</div>"


def send_email(subject, html):
    user, pw, to = os.environ["SMTP_USER"], os.environ["SMTP_PASS"], os.environ["MAIL_TO"]
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())


# ---------- hlavný beh ----------
def collect(cfg, fixtures=None):
    found, health = [], {}
    if fixtures:
        for f in sorted(Path(fixtures).glob("*.html")):
            src = f.stem.split("_")[0]
            items = extract_cards(f.read_text(encoding="utf-8"), src)
            found += items
            health[src] = health.get(src, 0) + len(items)
        return found, health

    for src in cfg["sources"]:
        name = src["name"]
        health[name] = 0
        for url in src["urls"]:
            seen = set()
            for p in range(src.get("max_pages", 1)):
                u = url.format(page=p + 1, offset=f"{p*20}/" if p else "")
                if p and u == url.format(page=1, offset=""):
                    break  # URL bez stránkovania
                try:
                    html = fetch(u, src.get("timeout", 30))
                except Exception as e:
                    print(f"{name} chyba: {e}", file=sys.stderr); break
                t = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
                if src.get("expect_title") and not (t and re.search(src["expect_title"], norm(t.group(1)))):
                    print(f"{name}: neočakávaná stránka ({u}) – preskakujem", file=sys.stderr); break
                items = extract_cards(html, name)
                ids = {i["id"] for i in items}
                if not items or ids <= seen:
                    break
                seen |= ids
                found += items; health[name] += len(items)
                time.sleep(2)
    return found, health


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fixtures")
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    load_sources(cfg)
    store = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    today = date.today().isoformat()

    raw, health = collect(cfg, args.fixtures)
    new, drops, seen_now, dropped = [], [], set(), {}
    for item in {i["id"]: i for i in raw}.values():
        zone, obec = classify(item, cfg)
        if zone is None:
            dropped[obec] = dropped.get(obec, 0) + 1
            continue
        finalize_price(item)
        if not passes_numbers(item, cfg):
            dropped["cena/výmera"] = dropped.get("cena/výmera", 0) + 1
            continue
        item.update(zone=zone, obec=obec if zone != "Na posúdenie" else "")
        seen_now.add(item["id"])
        old = store.get(item["id"])
        if old:
            if item.get("price") and old.get("price") and item["price"] != old["price"]:
                old.setdefault("price_history", []).append([today, old["price"]])
                if item["price"] < old["price"] and not old.get("dup_of"):
                    drops.append({**old, "old_price": old["price"], "price": item["price"]})
            old.update({k: v for k, v in item.items() if v is not None}, last_seen=today, active=True)
        else:
            item.update(first_seen=today, last_seen=today, active=True, price_history=[])
            item["dup_of"] = find_duplicate(item, store)
            store[item["id"]] = item
            if not item["dup_of"]:
                new.append(item)

    for o in store.values():
        if o["id"] not in seen_now and o.get("active"):
            days = (date.today() - date.fromisoformat(o["last_seen"])).days
            if days >= cfg["inactive_after_days"]:
                o["active"] = False

    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "docs" / "meta.json").write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "health": health}), encoding="utf-8")

    active = sum(1 for o in store.values() if o.get("active") and not o.get("dup_of"))
    subject, html = build_email(new, health, active, os.environ.get("PAGES_URL", ""), drops)
    print(f"Stiahnuté: {health} | nové: {len(new)} | aktívne: {active} | vyhodené: {dropped}")
    if args.dry_run or args.fixtures:
        (ROOT / "email_preview.html").write_text(html, encoding="utf-8")
        print("Predmet:", subject)
    else:
        send_email(subject, html)


if __name__ == "__main__":
    main()
