"""VintedTracker - monitora prezzi e velocita' di vendita su Vinted per brand/categoria.

Uso:
    py tracker.py fetch    # fotografia del mercato + campionamento nuovi annunci
    py tracker.py check    # verifica lo stato (venduto/attivo) degli annunci campionati
    py tracker.py build    # ricalcola le statistiche e genera docs/index.html
    py tracker.py run      # fetch + check + build

Metodo:
    Il catalogo anonimo di Vinted mostra al massimo 10 pagine e per le ricerche popolari
    poche pagine coprono solo qualche ora di nuovi annunci: non si puo' dedurre il "venduto"
    dalla sparizione dalle pagine. Quindi:
    - ogni giorno le prime pagine danno la fotografia del mercato (prezzi richiesti, concorrenza);
    - dalla pagina 1 si campionano i N annunci piu' nuovi (eta' nota) e si seguono nel tempo;
    - a 2 e 7 giorni si apre la pagina di ciascun annuncio campionato: il plugin
      "buyer_item_status" con titolo "Venduto" indica la vendita; can_buy=true attivo; 404 eliminato.
"""
import html
import json
import random
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DB_PATH = ROOT / "vinted.db"
DOCS = ROOT / "docs"
TEMPLATE = ROOT / "dashboard_template.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

ITEM_RE = re.compile(r'<a href="/items/(\d+)[^"]*"[^>]*title="([^"]*)"')
TITLE_RE = re.compile(
    r'^(?P<title>.*?), Brand: (?P<brand>[^,]*), Condizioni: (?P<cond>[^,]*), '
    r'Taglia: (?P<size>[^,]*), (?P<price>[\d.,]+) €', re.S)
CAN_BUY_RE = re.compile(r'\\"can_buy\\":(true|false)')
PRICE_RE = re.compile(r'\\"price\\":\{\\"amount\\":\\"([\d.]+)\\",\\"currency_code\\":\\"([A-Z]{3})\\"')
STATUS_TITLE_RE = re.compile(r'\\"title\\":\\"([^"\\]*)\\"')

SOLD_STATUSES = ("sold", "closed")


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s)


def days_between(a, b):
    return (parse_iso(b) - parse_iso(a)).total_seconds() / 86400


LAST_RUN = ROOT / "last_run.log"     # log dell'ultima esecuzione, committato dal workflow


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    for path in (ROOT / "logs" / "tracker.log", LAST_RUN):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def snippet(text):
    """Testo visibile di una pagina, compresso, per diagnosticare blocchi/captcha."""
    return re.sub(r"\s+", " ", re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", text, flags=re.S))[:300]


def sleep(rng):
    time.sleep(random.uniform(*rng))


# ---------------------------------------------------------------- database

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER NOT NULL,
            query_key TEXT NOT NULL,
            title TEXT, brand TEXT, condition TEXT, size TEXT,
            price REAL, first_price REAL,
            first_seen TEXT, last_seen TEXT,
            baseline INTEGER DEFAULT 0,      -- eta' sconosciuta (esisteva gia' al primo passaggio)
            sample INTEGER DEFAULT 0,        -- 1 = annuncio seguito nel tempo
            status TEXT DEFAULT 'active',    -- active | reserved | sold | closed | deleted | hidden
            status_at TEXT,                  -- ultimo controllo della pagina
            checks INTEGER DEFAULT 0,
            next_check TEXT,
            sold_price REAL,
            PRIMARY KEY (id, query_key)
        );
        CREATE INDEX IF NOT EXISTS items_check ON items(sample, next_check);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_key TEXT, started TEXT, pages INTEGER, n_items INTEGER,
            max_id INTEGER, n_new INTEGER, n_sampled INTEGER
        );
    """)
    return con


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "it-IT,it;q=0.9"})
    s.get(CONFIG["site"] + "/", timeout=60)   # cookie di sessione anonima
    return s


# ---------------------------------------------------------------- fetch

def parse_items(page_html):
    items = {}
    for m in ITEM_RE.finditer(page_html):
        iid = int(m.group(1))
        t = TITLE_RE.match(html.unescape(m.group(2)))
        if not t:
            continue
        try:
            price = float(t.group("price").replace(",", "."))
        except ValueError:
            continue
        items[iid] = {"title": t.group("title").strip()[:200], "brand": t.group("brand").strip(),
                      "condition": t.group("cond").strip(), "size": t.group("size").strip(), "price": price}
    return items


def fetch_query(s, q):
    found = {}
    pages = 0
    for page in range(1, CONFIG["pages_per_query"] + 1):
        params = [("order", "newest_first"), ("page", page)]
        params += [("brand_ids[]", b) for b in q.get("brand_ids", [])]
        params += [("catalog[]", c) for c in q.get("catalog_ids", [])]
        if q.get("search_text"):
            params.append(("search_text", q["search_text"]))
        r = s.get(f"{CONFIG['site']}/catalog", params=params, timeout=60)
        if r.status_code != 200:
            log(f"  {q['key']} pagina {page}: HTTP {r.status_code} | {snippet(r.text)}")
            break
        page_items = parse_items(r.text)
        pages += 1
        if not page_items:
            if page == 1:
                log(f"  {q['key']} pagina 1 vuota (HTTP 200, {len(r.text)} byte) | {snippet(r.text)}")
            break
        found.update(page_items)
        sleep(CONFIG["delay_seconds"])
        if len(page_items) < 90:
            break
    return found, pages


def cmd_fetch():
    s = session()
    con = db()
    empty = 0
    for q in CONFIG["queries"]:
        ts = iso(now())
        found, pages = {}, 0
        for attempt in range(3):
            try:
                found, pages = fetch_query(s, q)
            except requests.RequestException as e:
                log(f"  {q['key']}: errore rete {e}")
            if found:
                break
            log(f"  {q['key']}: nessun annuncio (tentativo {attempt + 1}), attendo 90 s e rinnovo la sessione")
            time.sleep(90)
            s = session()
        if not found:
            empty += 1
            if empty >= 3:
                log("fetch: Vinted non risponde con dati, interrompo (probabile blocco dell'IP)")
                con.close()
                sys.exit(2)
            continue
        prev = con.execute("SELECT max_id FROM runs WHERE query_key=? AND n_items>0 ORDER BY id DESC LIMIT 1",
                           (q["key"],)).fetchone()
        prev_max = prev["max_id"] if prev else None
        n_new, n_sampled = upsert(con, q["key"], ts, found, prev_max)
        con.execute("INSERT INTO runs(query_key, started, pages, n_items, max_id, n_new, n_sampled) VALUES (?,?,?,?,?,?,?)",
                    (q["key"], ts, pages, len(found), max(found), n_new, n_sampled))
        con.commit()
        log(f"  {q['key']}: {len(found)} annunci su {pages} pagine, nuovi {n_new}, campionati {n_sampled}")
    con.close()


def upsert(con, key, ts, found, prev_max):
    new_ids = []
    for iid, it in found.items():
        row = con.execute("SELECT 1 FROM items WHERE id=? AND query_key=?", (iid, key)).fetchone()
        if row:
            con.execute("UPDATE items SET last_seen=?, price=?, title=?, condition=?, size=? WHERE id=? AND query_key=?",
                        (ts, it["price"], it["title"], it["condition"], it["size"], iid, key))
        else:
            baseline = 1 if (prev_max is None or iid <= prev_max) else 0
            con.execute("INSERT INTO items(id, query_key, title, brand, condition, size, price, first_price, first_seen, last_seen, baseline) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, key, it["title"], it["brand"], it["condition"], it["size"], it["price"], it["price"], ts, ts, baseline))
            if not baseline:
                new_ids.append(iid)
    # campione: i piu' nuovi tra i nuovi (eta' minima), seguiti nel tempo
    new_ids.sort(reverse=True)
    sampled = new_ids[:CONFIG["sample_per_query"]]
    first_check = iso(parse_iso(ts) + timedelta(days=CONFIG["check_days"][0], hours=-6))
    for iid in sampled:
        con.execute("UPDATE items SET sample=1, next_check=? WHERE id=? AND query_key=?", (first_check, iid, key))
    return len(new_ids), len(sampled)


# ---------------------------------------------------------------- check

def parse_item_page(text):
    """Ritorna (status, price_eur|None)."""
    price = None
    m = PRICE_RE.search(text)
    if m and m.group(2) == "EUR":
        price = float(m.group(1))
    i = text.find('buyer_item_status')
    if i >= 0:
        seg = text[max(0, i - 400):i]
        titles = STATUS_TITLE_RE.findall(seg)
        title = titles[-1].lower() if titles else ""
        if "vendut" in title:
            return "sold", price
        if "prenotat" in title or "riservat" in title:
            return "reserved", price
        if "nascost" in title:
            return "hidden", price
        return "closed", price
    cb = CAN_BUY_RE.search(text)
    if cb:
        if cb.group(1) == "true":
            reserved = re.search(r'\\"is_reserved\\":true', text)
            return ("reserved" if reserved else "active"), price
        return "closed", price
    return "unknown", price


def cmd_check():
    con = db()
    ts = iso(now())
    due = con.execute("SELECT * FROM items WHERE sample=1 AND status IN ('active','reserved') AND next_check<=? "
                      "ORDER BY next_check LIMIT ?", (ts, CONFIG["max_item_checks_per_run"])).fetchall()
    if not due:
        log("check: nessun annuncio da verificare")
        return
    log(f"check: {len(due)} annunci da verificare")
    s = session()
    counts = {}
    for n, row in enumerate(due, 1):
        url = f"{CONFIG['site']}/items/{row['id']}"
        try:
            r = s.get(url, timeout=60)
            if r.status_code == 429:
                log("  429 rate limit: pausa 120 s")
                time.sleep(120)
                r = s.get(url, timeout=60)
                if r.status_code == 429:
                    log("  ancora 429: interrompo i controlli per oggi")
                    break
            if r.status_code == 404:
                status, price = "deleted", None
            elif r.status_code == 403:
                log(f"  403 (blocco anti-bot) dopo {n - 1} controlli: interrompo i controlli per oggi | {snippet(r.text)[:120]}")
                break
            elif r.status_code != 200:
                log(f"  {row['id']}: HTTP {r.status_code}, salto")
                sleep(CONFIG["item_delay_seconds"])
                continue
            else:
                status, price = parse_item_page(r.text)
        except requests.RequestException as e:
            log(f"  {row['id']}: errore rete {e}")
            continue
        counts[status] = counts.get(status, 0) + 1
        checks = row["checks"] + 1
        nxt = None
        if status in ("active", "reserved", "unknown") and checks < len(CONFIG["check_days"]):
            nxt = iso(parse_iso(row["first_seen"]) + timedelta(days=CONFIG["check_days"][checks], hours=-6))
        if status == "unknown":
            status = row["status"]           # pagina non leggibile: mantengo lo stato precedente
        con.execute("UPDATE items SET status=?, status_at=?, checks=?, next_check=?, price=COALESCE(?, price), "
                    "sold_price=CASE WHEN ? IN ('sold','closed') THEN COALESCE(?, price) ELSE sold_price END "
                    "WHERE id=? AND query_key=?",
                    (status, ts, checks, nxt, price, status, price, row["id"], row["query_key"]))
        if n % 25 == 0:
            con.commit()
        sleep(CONFIG["item_delay_seconds"])
    con.commit()
    con.close()
    log(f"check: esiti {counts}")


# ---------------------------------------------------------------- fornitori

def fetch_suppliers():
    """Disponibilita' e prezzi dei box dei fornitori (Shopify espone /products/<slug>.js)."""
    out = []
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/javascript, */*"})
    for sup in CONFIG.get("suppliers", []):
        for prod in sup["products"]:
            url = sup["base"] + prod["slug"]
            try:
                r = s.get(url + ".js", timeout=45)
                j = r.json() if r.status_code == 200 else None
            except (requests.RequestException, ValueError):
                j = None
            if not j:
                log(f"  fornitore {prod['slug']}: non leggibile")
                continue
            variants = [{"title": v.get("title"), "price": (v.get("price") or 0) / 100, "available": bool(v.get("available"))}
                        for v in j.get("variants", [])]
            free = [v for v in variants if v["available"]]
            best = min(free, key=lambda v: v["price"]) if free else None
            kg = None
            if best:
                m = re.search(r"(\d+)\s*Kg", best["title"], re.I)
                kg = int(m.group(1)) if m else None
            out.append({
                "supplier": sup["name"], "label": prod["label"], "title": j.get("title"), "url": url,
                "available": bool(j.get("available")), "variants": variants,
                "best": best, "best_kg": kg,
                "per_piece": round(best["price"] / (kg * prod["per_kg"]), 1) if best and kg else None,
            })
            sleep(CONFIG["delay_seconds"])
    log(f"fornitori: {sum(1 for x in out if x['available'])}/{len(out)} box disponibili")
    (ROOT / "suppliers.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ---------------------------------------------------------------- stats

def quantiles(values):
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return {"p25": vs[0], "med": vs[0], "p75": vs[0]}
    q = statistics.quantiles(vs, n=4, method="inclusive")
    return {"p25": round(q[0], 1), "med": round(q[1], 1), "p75": round(q[2], 1)}


def wilson(k, n, z=1.96):
    """Semi-ampiezza (in punti %) dell'intervallo di confidenza al 95% di una proporzione."""
    if not n:
        return None
    p = k / n
    return round(100 * z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5 / (1 + z * z / n))


def histogram(values):
    step, cap = CONFIG["price_bucket"], CONFIG["price_cap"]
    n = cap // step + 1
    bins = [0] * n
    for v in values:
        bins[min(int(v // step), n - 1)] += 1
    return bins


def query_stats(con, q, latest_ts, fast_days):
    key = q["key"]
    rows = con.execute("SELECT * FROM items WHERE query_key=?", (key,)).fetchall()
    active = [r for r in rows if r["last_seen"] == latest_ts and r["status"] in ("active", "reserved")]
    followed = [r for r in rows if r["sample"] and r["status_at"] and r["status"] not in ("deleted", "hidden")]
    age = lambda r: days_between(r["first_seen"], r["status_at"])
    sold = [r for r in followed if r["status"] in SOLD_STATUSES]
    fast_any = [r for r in sold if age(r) <= fast_days + 0.5]     # per prezzi/istogrammi: anche lotti non maturi
    # coorte: solo lotti "maturi" (pubblicati da almeno N giorni + margine, cosi' il controllo a N giorni
    # e' gia' avvenuto per tutti): venduti entro N giorni, oppure osservati a >= N giorni (venduti dopo o ancora in vendita)
    now_iso = iso(now())
    quick_days = CONFIG["check_days"][0]

    def cohort_for(n_days):
        return [r for r in followed if days_between(r["first_seen"], now_iso) >= n_days + 0.75
                and ((r["status"] in SOLD_STATUSES and age(r) <= n_days + 0.5) or age(r) >= n_days - 0.5)]
    cohort = cohort_for(fast_days)
    cohort_quick = cohort_for(quick_days)
    fast = [r for r in cohort if r["status"] in SOLD_STATUSES and age(r) <= fast_days + 0.5]
    quick = [r for r in cohort_quick if r["status"] in SOLD_STATUSES and age(r) <= quick_days + 0.5]
    sp = lambda r: r["sold_price"] if r["sold_price"] else r["price"]

    def by_group(field):
        out = {}
        for r in active:
            out.setdefault(r[field] or "?", {"active": [], "fast": []})["active"].append(r["price"])
        for r in fast_any:
            out.setdefault(r[field] or "?", {"active": [], "fast": []})["fast"].append(sp(r))
        res = [{"name": g, "n_active": len(d["active"]), "active": quantiles(d["active"]),
                "n_fast": len(d["fast"]), "fast": quantiles(d["fast"])} for g, d in out.items()]
        res.sort(key=lambda x: -(x["n_active"] + x["n_fast"]))
        return res

    discounted = [r for r in fast_any if r["first_price"] and sp(r) < r["first_price"]]
    return {
        "key": key, "brand": q["brand"], "category": q["category"], "url": build_url(q),
        "n_active": len(active),
        "active": quantiles([r["price"] for r in active]),
        "n_fast": len(fast_any),
        "fast": quantiles([sp(r) for r in fast_any]),
        "pct_fast": round(100 * len(fast) / len(cohort)) if cohort else None,
        "ci_fast": wilson(len(fast), len(cohort)),
        "n_cohort": len(cohort),
        "pct_quick": round(100 * len(quick) / len(cohort_quick)) if cohort_quick else None,
        "ci_quick": wilson(len(quick), len(cohort_quick)),
        "n_cohort_quick": len(cohort_quick),
        "quick_days": CONFIG["check_days"][0],
        "n_followed": len([r for r in rows if r["sample"]]),
        "pct_discounted_fast": round(100 * len(discounted) / len(fast_any)) if fast_any else None,
        "hist_active": histogram([r["price"] for r in active]),
        "hist_fast": histogram([sp(r) for r in fast_any]),
        "by_condition": by_group("condition"),
        "by_size": by_group("size")[:8],
        "recent_fast": [{"title": r["title"], "price": sp(r), "size": r["size"], "condition": r["condition"],
                         "days": round(age(r), 1), "id": r["id"]}
                        for r in sorted(fast_any, key=lambda r: r["status_at"], reverse=True)[:12]],
    }


def advice(stats):
    """Classifica le ricerche: valore atteso per annuncio = probabilita' di vendita x prezzo di vendita."""
    out = []
    for s in stats:
        # uso l'orizzonte a 7 giorni solo quando ha abbastanza casi, altrimenti quello a 2 giorni
        use_fast = s["pct_fast"] is not None and s["n_cohort"] >= CONFIG["min_cohort"]
        pct = s["pct_fast"] if use_fast else s["pct_quick"]
        n = s["n_cohort"] if use_fast else s["n_cohort_quick"]
        horizon = CONFIG["fast_days"] if use_fast else CONFIG["check_days"][0]
        price = s["fast"]["med"] if s["fast"] else None
        if pct is None or price is None or n < CONFIG["min_cohort"]:
            out.append({"key": s["key"], "score": None, "verdict": "dati insufficienti", "note": f"solo {n} casi maturi (ne servono {CONFIG['min_cohort']})", "pct": pct, "n": n, "horizon": horizon, "price": price})
            continue
        score = round(pct / 100 * price, 1)
        ask = s["active"]["med"] if s["active"] else None
        gap = round(100 * (1 - price / ask)) if ask else None
        notes = []
        if price >= 25 and pct >= 25: verdict = "punta"
        elif price >= 20 and pct >= 15: verdict = "discreto"
        elif pct >= 30: verdict = "veloce ma vale poco"
        else: verdict = "evita"
        if price < 10: notes.append("prezzo troppo basso per ripagare il lavoro")
        if gap is not None and gap >= 25: notes.append(f"si vende {gap}% sotto il prezzo richiesto: prezza basso subito")
        if s["pct_discounted_fast"] is not None and s["pct_discounted_fast"] >= 25: notes.append(f"{s['pct_discounted_fast']}% dei venduti ha dovuto ribassare")
        if s["fast"] and s["fast"]["p75"] >= 1.6 * s["fast"]["med"]: notes.append("prezzi molto dispersi: contano modello e condizioni")
        out.append({"key": s["key"], "score": score, "verdict": verdict, "note": "; ".join(notes), "pct": pct, "n": n, "horizon": horizon, "price": price})
    out.sort(key=lambda a: -(a["score"] or -1))
    return out


def build_url(q):
    parts = [f"brand_ids[]={b}" for b in q.get("brand_ids", [])]
    parts += [f"catalog[]={c}" for c in q.get("catalog_ids", [])]
    if q.get("search_text"):
        parts.append("search_text=" + requests.utils.quote(q["search_text"]))
    return f"{CONFIG['site']}/catalog?order=newest_first&" + "&".join(parts)


def cmd_build():
    con = db()
    fast_days = CONFIG["fast_days"]
    first_run = con.execute("SELECT MIN(started) FROM runs").fetchone()[0]
    stats = []
    for q in CONFIG["queries"]:
        latest = con.execute("SELECT started FROM runs WHERE query_key=? AND n_items>0 ORDER BY id DESC LIMIT 1",
                             (q["key"],)).fetchone()
        if latest is None:
            continue
        stats.append(query_stats(con, q, latest["started"], fast_days))
    n_days = con.execute("SELECT COUNT(DISTINCT substr(started,1,10)) FROM runs").fetchone()[0]
    data = {
        "generated": iso(now()), "first_run": first_run, "days_collected": n_days,
        "fast_days": fast_days, "quick_days": CONFIG["check_days"][0],
        "price_bucket": CONFIG["price_bucket"], "price_cap": CONFIG["price_cap"],
        "n_items_total": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "n_followed": con.execute("SELECT COUNT(*) FROM items WHERE sample=1").fetchone()[0],
        "n_sold": con.execute("SELECT COUNT(*) FROM items WHERE status IN ('sold','closed')").fetchone()[0],
        "queries": stats,
        "advice": advice(stats),
        "suppliers": json.loads((ROOT / "suppliers.json").read_text(encoding="utf-8")) if (ROOT / "suppliers.json").exists() else [],
    }
    con.close()
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    log(f"dashboard generata: {len(stats)} ricerche, {data['n_items_total']} annunci, "
        f"{data['n_followed']} seguiti, {data['n_sold']} venduti, {n_days} giorni di dati")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("fetch", "run"):
        log("=== fetch ===")
        cmd_fetch()
    if cmd in ("fetch", "run"):
        fetch_suppliers()
    if cmd in ("check", "run"):
        log("=== check ===")
        cmd_check()
    if cmd in ("build", "run"):
        cmd_build()
    if cmd not in ("fetch", "check", "build", "run"):
        print(__doc__)
