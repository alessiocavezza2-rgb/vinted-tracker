"""VintedTracker - monitora prezzi e velocita' di vendita su Vinted per brand/categoria.

Uso:
    py tracker.py fetch    # scarica gli annunci e aggiorna il database
    py tracker.py build    # ricalcola le statistiche e genera docs/index.html
    py tracker.py run      # fetch + build

Come funziona la stima "venduto":
    Ogni giorno si scaricano le prime N pagine di ogni ricerca ordinate per "piu' recenti".
    Un annuncio visto in un giorno precedente, piu' recente del piu' vecchio annuncio
    scaricato oggi, ma assente oggi, e' sparito dal catalogo (venduto, ritirato o eliminato).
    Gli annunci gia' esistenti al primo giorno di raccolta (eta' sconosciuta) non contano
    per le statistiche di velocita'.
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

# <a href="/items/123-slug" ... title="Titolo, Brand: X, Condizioni: Y, Taglia: Z, 12.00 €, 13.20 €">
ITEM_RE = re.compile(r'<a href="/items/(\d+)[^"]*"[^>]*title="([^"]*)"')
TITLE_RE = re.compile(
    r'^(?P<title>.*?), Brand: (?P<brand>[^,]*), Condizioni: (?P<cond>[^,]*), '
    r'Taglia: (?P<size>[^,]*), (?P<price>[\d.,]+) €', re.S)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s)


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    (ROOT / "logs").mkdir(exist_ok=True)
    with open(ROOT / "logs" / "tracker.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


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
            first_seen TEXT, last_seen TEXT, last_covered TEXT, gone_at TEXT,
            baseline INTEGER DEFAULT 0,
            PRIMARY KEY (id, query_key)
        );
        CREATE INDEX IF NOT EXISTS items_query ON items(query_key, gone_at);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_key TEXT, started TEXT, pages INTEGER, n_items INTEGER,
            min_id INTEGER, max_id INTEGER, complete INTEGER
        );
    """)
    return con


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
        items[iid] = {
            "title": t.group("title").strip()[:200],
            "brand": t.group("brand").strip(),
            "condition": t.group("cond").strip(),
            "size": t.group("size").strip(),
            "price": price,
        }
    return items


def fetch_query(session, q):
    site = CONFIG["site"]
    lo, hi = CONFIG["delay_seconds"]
    found = {}
    pages_done = 0
    for page in range(1, CONFIG["pages_per_query"] + 1):
        params = [("order", "newest_first"), ("page", page)]
        params += [("brand_ids[]", b) for b in q.get("brand_ids", [])]
        params += [("catalog[]", c) for c in q.get("catalog_ids", [])]
        if q.get("search_text"):
            params.append(("search_text", q["search_text"]))
        r = session.get(f"{site}/catalog", params=params, timeout=60)
        if r.status_code != 200:
            log(f"  {q['key']} pagina {page}: HTTP {r.status_code}, mi fermo")
            break
        page_items = parse_items(r.text)
        pages_done += 1
        if not page_items:
            break
        found.update(page_items)
        time.sleep(random.uniform(lo, hi))
        if len(page_items) < 90:      # ultima pagina: catalogo esaurito
            return found, pages_done, True
    return found, pages_done, pages_done < CONFIG["pages_per_query"]


def cmd_fetch():
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html,*/*",
                            "Accept-Language": "it-IT,it;q=0.9"})
    session.get(CONFIG["site"] + "/", timeout=60)   # cookie di sessione anonima
    con = db()
    for q in CONFIG["queries"]:
        started = now_iso()
        try:
            found, pages, complete = fetch_query(session, q)
        except requests.RequestException as e:
            log(f"  {q['key']}: errore rete {e}")
            continue
        if not found:
            log(f"  {q['key']}: nessun annuncio, salto")
            continue
        min_id = 0 if complete else min(found)
        max_id = max(found)
        prev = con.execute("SELECT max_id FROM runs WHERE query_key=? AND n_items>0 "
                           "ORDER BY id DESC LIMIT 1", (q["key"],)).fetchone()
        prev_max = prev["max_id"] if prev else None
        upsert_run(con, q["key"], started, found, min_id, max_id, prev_max)
        con.execute("INSERT INTO runs(query_key, started, pages, n_items, min_id, max_id, complete) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (q["key"], started, pages, len(found), min_id, max_id, int(complete)))
        con.commit()
        gone = con.execute("SELECT COUNT(*) FROM items WHERE query_key=? AND gone_at=?",
                           (q["key"], started)).fetchone()[0]
        log(f"  {q['key']}: {len(found)} annunci su {pages} pagine, spariti oggi: {gone}")
    con.close()


def upsert_run(con, key, ts, found, min_id, max_id, prev_max):
    # 1) annunci gia' tracciati e ancora attivi, dentro la copertura di oggi
    rows = con.execute("SELECT id, price FROM items WHERE query_key=? AND gone_at IS NULL AND id>=?",
                       (key, min_id)).fetchall()
    for row in rows:
        it = found.get(row["id"])
        if it is None:
            con.execute("UPDATE items SET gone_at=?, last_covered=? WHERE id=? AND query_key=?",
                        (ts, ts, row["id"], key))
        else:
            con.execute("UPDATE items SET last_seen=?, last_covered=?, price=?, title=?, condition=?, size=? "
                        "WHERE id=? AND query_key=?",
                        (ts, ts, it["price"], it["title"], it["condition"], it["size"], row["id"], key))
    # 2) annunci nuovi (o riapparsi dopo essere spariti: li trattiamo come nuovi record? no: li teniamo)
    for iid, it in found.items():
        exists = con.execute("SELECT gone_at FROM items WHERE id=? AND query_key=?", (iid, key)).fetchone()
        if exists is None:
            # baseline = eta' sconosciuta: primo giorno di raccolta, o annuncio piu' vecchio
            # del piu' recente visto ieri (riapparso in cima grazie a un boost)
            baseline = 1 if (prev_max is None or iid <= prev_max) else 0
            con.execute("INSERT INTO items(id, query_key, title, brand, condition, size, price, first_price, "
                        "first_seen, last_seen, last_covered, gone_at, baseline) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                        (iid, key, it["title"], it["brand"], it["condition"], it["size"], it["price"], it["price"],
                         ts, ts, ts, baseline))
        elif exists["gone_at"] is not None:
            # era sparito ma e' tornato (prenotato e poi liberato): riattivo
            con.execute("UPDATE items SET gone_at=NULL, last_seen=?, last_covered=?, price=? WHERE id=? AND query_key=?",
                        (ts, ts, it["price"], iid, key))
        elif con.execute("SELECT 1 FROM items WHERE id=? AND query_key=? AND last_seen<>?", (iid, key, ts)).fetchone():
            # visto ma fuori copertura (id < min_id, es. boost): aggiorno solo la vista
            con.execute("UPDATE items SET last_seen=?, price=? WHERE id=? AND query_key=?", (ts, it["price"], iid, key))


# ---------------------------------------------------------------- stats

def quantiles(values):
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return {"p25": vs[0], "med": vs[0], "p75": vs[0]}
    q = statistics.quantiles(vs, n=4, method="inclusive")
    return {"p25": round(q[0], 1), "med": round(q[1], 1), "p75": round(q[2], 1)}


def histogram(values):
    step, cap = CONFIG["price_bucket"], CONFIG["price_cap"]
    n = cap // step + 1
    bins = [0] * n
    for v in values:
        bins[min(int(v // step), n - 1)] += 1
    return bins


def days_between(a, b):
    return (parse_iso(b) - parse_iso(a)).total_seconds() / 86400


def query_stats(con, q, latest_ts, fast_days):
    key = q["key"]
    rows = con.execute("SELECT * FROM items WHERE query_key=?", (key,)).fetchall()
    active = [r for r in rows if r["gone_at"] is None and r["last_covered"] == latest_ts]
    gone = [r for r in rows if r["gone_at"] is not None and not r["baseline"]]
    fast = [r for r in gone if days_between(r["first_seen"], r["gone_at"]) <= fast_days]
    # coorte per la % venduti: annunci di eta' nota, spariti entro fast_days oppure
    # osservati per almeno fast_days giorni (cosi' chi e' ancora in vendita conta come "non veloce")
    fast_ids = {r["id"] for r in fast}
    cohort = [r for r in rows if not r["baseline"] and (
        r["id"] in fast_ids or days_between(r["first_seen"], r["last_covered"]) >= fast_days)]
    cohort_fast = [r for r in cohort if r["id"] in fast_ids]

    def by_group(field):
        out = {}
        for r in active:
            out.setdefault(r[field] or "?", {"active": [], "fast": []})["active"].append(r["price"])
        for r in fast:
            out.setdefault(r[field] or "?", {"active": [], "fast": []})["fast"].append(r["price"])
        res = []
        for g, d in out.items():
            res.append({"name": g, "n_active": len(d["active"]), "active": quantiles(d["active"]),
                        "n_fast": len(d["fast"]), "fast": quantiles(d["fast"])})
        res.sort(key=lambda x: -(x["n_active"] + x["n_fast"]))
        return res

    discounted = [r for r in fast if r["first_price"] and r["price"] < r["first_price"]]
    return {
        "key": key, "brand": q["brand"], "category": q["category"],
        "url": build_url(q),
        "n_active": len(active),
        "active": quantiles([r["price"] for r in active]),
        "n_fast": len(fast),
        "fast": quantiles([r["price"] for r in fast]),
        "pct_fast": round(100 * len(cohort_fast) / len(cohort)) if cohort else None,
        "n_cohort": len(cohort),
        "median_days_gone": round(statistics.median(
            [days_between(r["first_seen"], r["gone_at"]) for r in gone]), 1) if gone else None,
        "pct_discounted_fast": round(100 * len(discounted) / len(fast)) if fast else None,
        "hist_active": histogram([r["price"] for r in active]),
        "hist_fast": histogram([r["price"] for r in fast]),
        "by_condition": by_group("condition"),
        "by_size": by_group("size")[:8],
        "recent_fast": [{"title": r["title"], "price": r["price"], "size": r["size"], "condition": r["condition"],
                         "days": round(days_between(r["first_seen"], r["gone_at"]), 1), "id": r["id"]}
                        for r in sorted(fast, key=lambda r: r["gone_at"], reverse=True)[:12]],
    }


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
    n_runs = con.execute("SELECT COUNT(DISTINCT substr(started,1,10)) FROM runs").fetchone()[0]
    data = {
        "generated": now_iso(),
        "first_run": first_run,
        "days_collected": n_runs,
        "fast_days": fast_days,
        "price_bucket": CONFIG["price_bucket"],
        "price_cap": CONFIG["price_cap"],
        "n_items_total": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "queries": stats,
    }
    con.close()
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    log(f"dashboard generata: {len(stats)} ricerche, {data['n_items_total']} annunci, {n_runs} giorni di dati")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("fetch", "run"):
        log("=== fetch ===")
        cmd_fetch()
    if cmd in ("build", "run"):
        cmd_build()
    if cmd not in ("fetch", "build", "run"):
        print(__doc__)
