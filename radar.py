"""
radar.py - radar d'acquisto per il VintedTracker.

Risponde a una domanda sola: questo capo, a questo prezzo, conviene comprarlo?

Usa il database che il bot riempie ogni mattina (vinted.db) per calcolare, gruppo
per gruppo, a quanto i capi si rivendono davvero, e da li' ricava il prezzo
massimo che puoi pagare perche' l'operazione valga la fatica.

Il riferimento di rivendita e' la MEDIANA dei venduti, non la media: cosi' due
capi da collezione finiti a 400 euro non ti fanno credere che la tua giacca
media valga il triplo di quello che vale.

Uso:
    python radar.py                      standard + occasioni delle ultime 48 ore
    python radar.py --ore 168            finestra di una settimana
    python radar.py --prezzo 30 --gruppo tnf_giacche_uomo
                                         giudica un capo che hai davanti
    python radar.py --prezzo 30 --gruppo tnf_giacche_uomo --titolo "Summit Gore-Tex"
                                         usa il sottogruppo di pregio, se lo riconosce
    python radar.py --box                solo le box dei fornitori
    python radar.py --html               scrive anche docs/radar.html (per il telefono)
"""

import argparse
import json
import re
import sqlite3
import statistics as st
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "vinted.db"

# ---------------------------------------------------------------- gli standard
# Questi sono i numeri che decidono "conviene o no". Cambiali qui se cambi idea.

SPEDIZIONE_ACQUISTO = 4.50   # quanto paghi di spedizione quando COMPRI su Vinted
PROTEZIONE_FISSA = 0.70      # Protezione Acquisti: parte fissa
PROTEZIONE_PERC = 0.05       # Protezione Acquisti: percentuale sul prezzo

MARGINE_COMPRA = 20.0        # euro netti sotto i quali non vale l'ora di lavoro
MOLTIPLIC_COMPRA = 1.8       # quante volte devi rifare il costo pieno
MARGINE_VALUTA = 12.0        # fra questo e il precedente: da guardare bene
MOLTIPLIC_VALUTA = 1.5

VENDITE_SOLIDE = 20          # da qui in su mi fido della stima
VENDITE_MINIME = 8           # sotto questo numero non mostro nemmeno il gruppo
VENDITE_FASCIA = 10          # vendite minime perche' una fascia sia consigliabile

MAX_OCC_HTML = 60            # quante occasioni finiscono nella pagina (le migliori)
VERIFICA_DEFAULT = 20        # quante verificare davvero su Vinted prima di consigliarle

# Quando il 75o percentile vale piu' di 3 volte il 25o, quel gruppo non ha UN
# prezzo: ne ha due. E' il caso delle Summit Series, vendute a 29-60 euro quando
# sono modelli comuni e a 160-400 quando sono pezzi ricercati, senza niente in
# mezzo. In quei casi la mediana cade nel vuoto fra i due mercati e non va usata:
# il radar prende la meta' bassa, cioe' l'ipotesi prudente.
DISPERSIONE_MAX = 3.0

# fasce chiuse: la coda oltre i 120 euro e' esclusa di proposito, perche' pochi
# capi da collezione a tre cifre falsano qualunque media
FASCE = [(0, 30), (30, 45), (45, 60), (60, 80), (80, 120)]

# Dentro un gruppo ci sono mondi diversi: un guscio Summit Series Gore-Tex non
# vale come un pile generico. Questi sottogruppi hanno riferimenti propri.
SOTTOGRUPPI = {
    "tnf_giacche_uomo": [
        ("Summit / Gore-Tex / XCR", r"summit|gore[\s-]?tex|xcr"),
        # il pile sta prima del piumino: e' il capo piu' comune nel mercato a
        # basso prezzo e vale meno della meta' di una giacca. Senza questa riga
        # il radar lo valuta come giacca generica e consiglia acquisti in perdita.
        ("Pile / fleece", r"pile\b|polaire|fleece|forro ?polar|trui\b"),
        ("Nuptse / 700 / piumino", r"nuptse|\b700\b|piumin|down jacket|doudoune"
                                   r"|steppjacke|daunen|plumifero"),
    ],
    "carhartt_giacche_uomo": [
        ("WIP", r"\bw\.?i\.?p\.?\b"),
        ("Detroit / Michigan / Active", r"detroit|michigan|active"),
    ],
    "levis_jeans_uomo": [
        ("501", r"\b501\b"),
        ("Big E / Orange Tab", r"big ?e\b|orange ?tab"),
    ],
    "levis_giacche_denim_uomo": [
        ("Sherpa / Trucker / Type 3", r"sherpa|trucker|type ?3"),
    ],
    "nike_giacche_uomo": [
        ("vintage / 90s / Y2K", r"vintage|\b90s?\b|y2k"),
    ],
}


def costo_pieno(prezzo):
    """Quello che esce davvero dal portafoglio per comprare un capo su Vinted."""
    return prezzo * (1 + PROTEZIONE_PERC) + PROTEZIONE_FISSA + SPEDIZIONE_ACQUISTO


def prezzo_massimo(ricavo):
    """Il prezzo di acquisto piu' alto che rispetta ENTRAMBI gli standard."""
    fisso = PROTEZIONE_FISSA + SPEDIZIONE_ACQUISTO
    da_margine = (ricavo - MARGINE_COMPRA - fisso) / (1 + PROTEZIONE_PERC)
    da_moltipl = (ricavo / MOLTIPLIC_COMPRA - fisso) / (1 + PROTEZIONE_PERC)
    return max(0.0, min(da_margine, da_moltipl))


def verdetto(ricavo, prezzo):
    costo = costo_pieno(prezzo)
    margine = ricavo - costo
    molt = ricavo / costo if costo else 0.0
    if margine >= MARGINE_COMPRA and molt >= MOLTIPLIC_COMPRA:
        return "COMPRA", margine, molt
    if margine >= MARGINE_VALUTA and molt >= MOLTIPLIC_VALUTA:
        return "VALUTA", margine, molt
    return "LASCIA", margine, molt


def percentile(valori, p):
    v = sorted(valori)
    if not v:
        return 0.0
    return v[min(len(v) - 1, int(len(v) * p))]


# ---------------------------------------------------------------- mercato

def carica(con):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "select id, query_key, title, brand, condition, size, price, sold_price,"
        " status, first_seen from items")]


def scheda(rs, nome, padre=None):
    """I riferimenti di un insieme di annunci omogenei."""
    venduti = [float(r["sold_price"] or r["price"]) for r in rs if r["status"] == "sold"]
    if len(venduti) < VENDITE_MINIME:
        return None

    fascia = None
    for lo, hi in FASCE:
        v = [r for r in rs if r["status"] == "sold"
             and lo <= float(r["sold_price"] or r["price"]) < hi]
        a = [r for r in rs if r["status"] == "active" and lo <= float(r["price"]) < hi]
        tot = len(v) + len(a)
        if len(v) < VENDITE_FASCIA or tot < 30:
            continue
        incasso = sum(float(r["sold_price"] or r["price"]) for r in v) / len(v)
        quota = len(v) / tot
        resa = quota * incasso
        if fascia is None or resa > fascia["resa"]:
            fascia = {"da": lo, "a": hi, "incasso": incasso, "quota": quota,
                      "resa": resa, "n_venduti": len(v)}

    v = sorted(venduti)
    mediana = st.median(v)
    p25, p75 = percentile(v, 0.25), percentile(v, 0.75)
    spaccato = bool(p25) and p75 / p25 >= DISPERSIONE_MAX

    if spaccato:
        # due mercati diversi nello stesso gruppo: conta solo quello basso
        bassa = [x for x in v if x <= mediana]
        alta = [x for x in v if x > mediana]
        ricavo = st.median(bassa) if bassa else mediana
        ricavo_alto = st.median(alta) if alta else p75
    else:
        ricavo = mediana
        ricavo_alto = p75

    return {
        "nome": nome,
        "padre": padre,
        "n_annunci": len(rs),
        "n_venduti": len(venduti),
        "solido": len(venduti) >= VENDITE_SOLIDE,
        "ricavo": ricavo,
        "ricavo_alto": ricavo_alto,
        "spaccato": spaccato,
        "mediana": mediana,
        "fascia": fascia,
        "paga_max": prezzo_massimo(ricavo),
    }


def riferimenti(righe):
    """Schede di tutti i gruppi e dei loro sottogruppi di pregio."""
    gruppi = {}
    for r in righe:
        gruppi.setdefault(r["query_key"], []).append(r)

    out = {}
    for key, rs in sorted(gruppi.items()):
        s = scheda(rs, key)
        if s:
            out[key] = s
        for etichetta, pattern in SOTTOGRUPPI.get(key, []):
            rx = re.compile(pattern, re.I)
            sub = [r for r in rs if rx.search(r["title"] or "")]
            ss = scheda(sub, f"{key} / {etichetta}", padre=key)
            if ss:
                ss["pattern"] = rx
                out[ss["nome"]] = ss
    return out


def riferimento_per(r, rif):
    """La scheda piu' specifica che si applica a un annuncio."""
    migliore = rif.get(r["query_key"])
    for nome, s in rif.items():
        if s.get("padre") == r["query_key"] and s["pattern"].search(r["title"] or ""):
            migliore = s
            break
    return migliore


def occasioni(righe, rif, ore):
    """Annunci ancora in vendita, comparsi di recente, che passano gli standard."""
    limite = datetime.now(timezone.utc) - timedelta(hours=ore)
    trovate = []
    for r in righe:
        if r["status"] != "active":
            continue
        info = riferimento_per(r, rif)
        if not info:
            continue
        try:
            visto = datetime.fromisoformat((r["first_seen"] or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if visto < limite:
            continue
        prezzo = float(r["price"])
        v, margine, molt = verdetto(info["ricavo"], prezzo)
        if v == "LASCIA":
            continue
        trovate.append({**r, "verdetto": v, "margine": margine, "moltiplicatore": molt,
                        "ricavo": info["ricavo"], "riferimento": info["nome"],
                        "solido": info["solido"], "spaccato": info["spaccato"],
                        "ricavo_alto": info["ricavo_alto"]})
    trovate.sort(key=lambda x: -x["margine"])
    return trovate


def eta_ore(first_seen):
    """Da quanto il bot vede questo annuncio: le occasioni vecchie sono morte."""
    try:
        t = datetime.fromisoformat((first_seen or "").replace("Z", "+00:00"))
    except ValueError:
        return "data ignota"
    ore = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    return f"{ore:.0f} h fa" if ore >= 1 else "adesso"


def ancora_in_vendita(occ, quante):
    """Apre gli annunci migliori e scarta quelli già spariti.

    Il database dice 'active' perché il bot gira tre volte al giorno, ma
    un'occasione vera dura minuti: un capo a 9 euro e' venduto prima che il
    bot torni a guardare. Senza questo controllo la prima scheda della pagina
    e' quasi sempre un 404, e il radar perde credibilita' sul colpo migliore.

    Gli stessi tempi di attesa del tracker (item_delay_seconds): un annuncio
    ogni 15-25 secondi, mai in parallelo.
    """
    import random
    import time
    import urllib.error
    import urllib.request

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    lo, hi = cfg.get("item_delay_seconds", [15, 25])
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

    vivi, morti, fermati = [], 0, False
    for i, o in enumerate(occ):
        if fermati or i >= quante:
            vivi.append(o)          # non verificato: resta, ma senza garanzia
            continue
        req = urllib.request.Request(
            f"https://www.vinted.it/items/{o['id']}",
            headers={"User-Agent": ua, "Accept": "text/html",
                     "Accept-Language": "it-IT,it;q=0.9"})
        try:
            urllib.request.urlopen(req, timeout=30).read(1)
            o["verificato"] = True
            vivi.append(o)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                morti += 1         # venduto o ritirato: fuori dalla pagina
            elif e.code in (403, 429):
                # Vinted ci sta dicendo di smettere: smettiamo.
                print(f"  verifica interrotta: HTTP {e.code} dopo {i} annunci")
                fermati = True
                vivi.append(o)
            else:
                vivi.append(o)
        except OSError as e:
            print(f"  {o['id']}: {type(e).__name__}, lo lascio nella lista")
            vivi.append(o)
        if not fermati and i + 1 < min(quante, len(occ)):
            time.sleep(random.uniform(lo, hi))
    if morti:
        print(f"verifica: {morti} occasioni su {min(quante, len(occ))} erano gia' sparite")
    return vivi


def ultimo_fetch(righe):
    """Quando il bot ha visto per l'ultima volta un annuncio nuovo."""
    date = []
    for r in righe:
        try:
            date.append(datetime.fromisoformat((r["first_seen"] or "").replace("Z", "+00:00")))
        except ValueError:
            pass
    return max(date) if date else None


# ---------------------------------------------------------------- box

# Che cosa c'e' dentro ogni box, secondo la descrizione del fornitore, e con che
# peso. Serve a non valutare una box di felpe come se fosse piena di giacche:
# e' l'errore che faceva la prima versione di questo radar.
# La Brand Box, per dirne una, dichiara "sweatshirts, sweaters, T-shirts, polo
# shirts, trousers, jackets and more" a 2-3 capi per kg: capi leggeri, non gusci.
BOX_COMPOSIZIONE = {
    # il nome del prodotto dal fornitore e' "jacket fleece box": dentro c'e'
    # anche il pile, che vale 25 EUR, non 35. Senza questo peso la box sembra
    # la migliore del catalogo e invece sta sul filo.
    "The North Face & Columbia": [("tnf_giacche_uomo / Pile / fleece", 0.5),
                                  ("tnf_giacche_uomo", 0.5)],
    "Giacche miste A/B": [("tnf_giacche_uomo", 0.5), ("carhartt_giacche_uomo", 0.2),
                          ("nike_giacche_uomo", 0.3)],
    "Brand Box grado A": [("carhartt_felpe_uomo", 0.15), ("nike_felpe_uomo", 0.15),
                          ("nike_tshirt_uomo", 0.15), ("adidas_tshirt_uomo", 0.10),
                          ("levis_jeans_uomo", 0.15), ("carhartt_jeans_uomo", 0.10),
                          ("carhartt_giacche_uomo", 0.10), ("tnf_giacche_uomo", 0.10)],
    "Levi's": [("levis_jeans_uomo", 0.7), ("levis_giacche_denim_uomo", 0.3)],
    "Nike & adidas": [("nike_giacche_uomo", 0.3), ("nike_felpe_uomo", 0.25),
                      ("adidas_felpe_uomo", 0.2), ("nike_tshirt_uomo", 0.15),
                      ("adidas_tshirt_uomo", 0.10)],
}

# quanta parte di una box e' roba che vendi davvero.
# Stime per tipo, non dati misurati: sulla box reale da 136 euro sono stati 8 su 12.
BOX_VENDIBILI = {
    "Brand Box grado A": 0.80,
    "The North Face & Columbia": 0.80,
    "Levi's": 0.70,
    "Giacche miste A/B": 0.67,
    "Vintage Box": 0.67,
    "Nike & adidas": 0.60,
    "Second hand mix": 0.50,
}


def giudica_box(rif):
    """Le box dei fornitori: costo per capo vendibile contro rivendita mediana."""
    f = ROOT / "suppliers.json"
    if not f.exists():
        return []
    box = json.loads(f.read_text(encoding="utf-8"))

    out = []
    for b in box:
        per_capo = b.get("per_piece")
        comp = BOX_COMPOSIZIONE.get(b["label"], [])

        # rivendita media pesata su cosa c'e' davvero dentro la box
        ricavo, peso_noto, pezzi = 0.0, 0.0, []
        for gruppo, peso in comp:
            info = rif.get(gruppo)
            if not info:
                continue
            ricavo += peso * info["ricavo"]
            peso_noto += peso
            pezzi.append(f"{gruppo.split('_', 1)[-1]} {100 * peso:.0f}%")
        ricavo = ricavo / peso_noto if peso_noto else 0.0

        riga = {
            "label": b["label"],
            "disponibile": bool(b.get("available")),
            "per_capo": per_capo,
            "composizione": ", ".join(pezzi),
            "copertura": peso_noto,
            "taglio": (b.get("best") or {}).get("title"),
            "prezzo_box": (b.get("best") or {}).get("price"),
            "margine": None,
        }
        if per_capo and ricavo:
            quota_ok = BOX_VENDIBILI.get(b["label"], 0.67)
            costo_vendibile = per_capo / quota_ok
            riga.update({
                "ricavo": ricavo,
                "costo_per_vendibile": costo_vendibile,
                "margine": ricavo - costo_vendibile,
                "moltiplicatore": ricavo / costo_vendibile,
                "quota_vendibili": quota_ok,
            })
        out.append(riga)
    return out


def esito_box(b):
    if b["margine"] >= MARGINE_COMPRA and b["moltiplicatore"] >= MOLTIPLIC_COMPRA:
        return "compra", "COMPRA"
    if b["margine"] >= MARGINE_VALUTA and b["moltiplicatore"] >= MOLTIPLIC_VALUTA:
        return "valuta", "VALUTA"
    return "", "LASCIA"


# ---------------------------------------------------------------- stampa

def stampa_standard(rif):
    print()
    print("=" * 100)
    print("GLI STANDARD - quanto puoi pagare, gruppo per gruppo")
    print("=" * 100)
    print(f"Un capo comprato a X ti costa X + 5% + {PROTEZIONE_FISSA:.2f} di Protezione Acquisti"
          f" + {SPEDIZIONE_ACQUISTO:.2f} di spedizione.")
    print(f"COMPRA solo se guadagni almeno {MARGINE_COMPRA:.0f} EUR netti E rifai almeno "
          f"{MOLTIPLIC_COMPRA} volte il costo pieno.")
    print()
    print(f"{'gruppo':<40} | {'rivendi a':>9} | {'se raro':>8} | {'PAGA MAX':>8} | "
          f"{'vendite':>7} | pubblica a")
    print("-" * 100)
    for nome, i in sorted(rif.items(), key=lambda kv: -kv[1]["paga_max"]):
        stella = "!" if i["spaccato"] else (" " if i["solido"] else "~")
        if i["fascia"]:
            f = i["fascia"]
            pub = f"{f['da']}-{f['a']} EUR ({100 * f['quota']:.0f}% venduti)"
        else:
            pub = "-"
        nome = nome if len(nome) <= 39 else nome[:36] + "..."
        print(f"{stella}{nome:<39} | {i['ricavo']:>7.0f} E | {i['ricavo_alto']:>6.0f} E |"
              f" {i['paga_max']:>6.0f} E | {i['n_venduti']:>7} | {pub}")
    print()
    print("'rivendi a'  = il prezzo prudente a cui quei capi si vendono davvero.")
    print("'se raro'    = quanto fa lo stesso gruppo quando il pezzo e' ricercato.")
    print("'PAGA MAX'   = oltre questo prezzo l'affare non rispetta piu' gli standard.")
    print("'pubblica a' = la fascia che rende piu' di tutte quando poi lo rivendi tu.")
    print("~ davanti al nome = meno di 20 vendite nel campione, stima con le molle.")
    print("! davanti al nome = quel gruppo ha DUE mercati, uno comune e uno da collezione,")
    print("                    con un vuoto in mezzo. 'rivendi a' usa solo quello comune:")
    print("                    il pezzo raro lo riconosci tu, non la statistica.")


def stampa_occasioni(occ, ore, ultimo):
    print()
    print("=" * 100)
    print(f"OCCASIONI - capi comparsi nelle ultime {ore} ore che passano gli standard")
    print("=" * 100)
    if not occ:
        print("Nessuna.")
        if ultimo:
            eta = (datetime.now(timezone.utc) - ultimo).total_seconds() / 3600
            print(f"L'annuncio piu' recente nel database e' di {eta:.0f} ore fa"
                  f" ({ultimo.astimezone().strftime('%d/%m alle %H:%M')}).")
            if eta > ore:
                print("Il database e' piu' vecchio della finestra: lancia prima")
                print("  python tracker.py fetch")
                print(f"oppure allarga la finestra con --ore {int(eta) + 24}.")
        return
    print(f"{'':<2}{'prezzo':>7} | {'ti costa':>8} | {'rivendi':>8} | {'margine':>8} | {'x':>4} | capo")
    print("-" * 100)
    for o in occ:
        segno = "**" if o["verdetto"] == "COMPRA" else "  "
        note = []
        if not o["solido"]:
            note.append("stima incerta")
        if o["spaccato"]:
            note.append(f"se e' un pezzo ricercato vale fino a {o['ricavo_alto']:.0f} EUR")
        dubbio = ("  (" + "; ".join(note) + ")") if note else ""
        print(f"{segno}{float(o['price']):>5.0f} E | {costo_pieno(float(o['price'])):>6.0f} E |"
              f" {o['ricavo']:>6.0f} E | {o['margine']:>6.0f} E | {o['moltiplicatore']:>4.1f} |"
              f" {(o['title'] or '')[:44]}")
        print(f"      https://www.vinted.it/items/{o['id']}"
              f"   [{o['riferimento']} - {o['condition']} - {o['size']}]{dubbio}")
    print()
    print("** = COMPRA (passa entrambi gli standard)    senza = VALUTA (ci passa per un soffio)")
    print("Il prezzo e' solo il primo filtro: guarda sempre foto, difetti e misure.")


def stampa_box(box):
    print()
    print("=" * 100)
    print("LE BOX DEI FORNITORI - conviene comprarne una?")
    print("=" * 100)
    print(f"{'box':<28} | {'EUR/capo':>8} | {'vendib.':>7} | {'costo utile':>11} | "
          f"{'rivendi':>7} | {'margine':>7} | {'x':>4} | esito")
    print("-" * 100)
    for b in box:
        if b["margine"] is None:
            if not b["disponibile"]:
                stato = "esaurita"
            elif not b.get("per_capo"):
                stato = "peso per capo sconosciuto (manca per_kg o il taglio in Kg)"
            else:
                stato = "nessun gruppo di confronto nel database"
            print(f"{b['label']:<28} |  {stato}")
            continue
        _, et = esito_box(b)
        flag = "" if b["disponibile"] else " (esaurita)"
        print(f"{b['label']:<28} | {b['per_capo']:>6.1f} E | {100 * b['quota_vendibili']:>6.0f}% |"
              f" {b['costo_per_vendibile']:>9.1f} E | {b['ricavo']:>5.0f} E |"
              f" {b['margine']:>5.0f} E | {b['moltiplicatore']:>4.1f} | {et}{flag}")
    print()
    print("'vendib.'     = quanta parte della box e' roba di marca che vendi davvero.")
    print("                Sono stime per tipo di box: sulla tua erano 8 capi su 12, il 67 %.")
    print("'costo utile' = il costo vero diviso per i soli capi che finiscono in vendita.")
    print("'rivendi'     = mediana dei venduti del gruppo a cui la box somiglia.")


# ---------------------------------------------------------------- html

# stessa tavolozza della dashboard (dashboard_template.html): se cambia una,
# cambia anche l'altra, altrimenti le due pagine smettono di somigliarsi.
CSS = """
:root{color-scheme:light;
--page:#efebe6;--surface:#faf8f5;--surface-2:#ede8e2;--ink:#141112;--ink-2:#4f4845;--muted:#8a817b;
--grid:#e0dad3;--border:rgba(20,17,18,.09);
--accent:#7a1c2b;--accent-ink:#fff;--accent-soft:#f1dfe0;--red:#c8102e;
--good:#0a7a2f;--warn:#a3520b;--bad:#c8102e;
--shadow:0 1px 2px rgba(20,17,18,.05),0 6px 18px rgba(20,17,18,.07)}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--page:#121011;--surface:#1c1819;--surface-2:#272223;--ink:#f5f1ee;--ink-2:#c9c1bc;--muted:#8f8681;
--grid:#322c2d;--border:rgba(255,255,255,.08);
--accent:#e3213f;--accent-ink:#fff;--accent-soft:#3a1a20;--red:#ff3b52;
--good:#34d399;--warn:#fbbf24;--bad:#ff6b6b;
--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 18px rgba(0,0,0,.3)}}
:root[data-theme="dark"]{color-scheme:dark;
--page:#121011;--surface:#1c1819;--surface-2:#272223;--ink:#f5f1ee;--ink-2:#c9c1bc;--muted:#8f8681;
--grid:#322c2d;--border:rgba(255,255,255,.08);
--accent:#e3213f;--accent-ink:#fff;--accent-soft:#3a1a20;--red:#ff3b52;
--good:#34d399;--warn:#fbbf24;--bad:#ff6b6b;
--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 18px rgba(0,0,0,.3)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
padding-block:0 calc(24px + env(safe-area-inset-bottom))}
.wrap{max-width:720px;margin:0 auto;padding-inline:16px}
header.top{padding:18px 0 8px;display:flex;align-items:center;gap:10px}
header.top .logo{width:34px;height:34px;border-radius:10px;background:#141112;color:var(--red);
display:grid;place-items:center;font-weight:800;font-size:16px;flex:0 0 auto}
h1{font-size:22px;margin:0;letter-spacing:-.01em}
h2{font-size:17px;margin:18px 0 8px}
.sub{color:var(--muted);font-size:12px;margin:0}
a{color:var(--accent)}
.card{background:var(--surface);border-radius:16px;padding:14px;margin-bottom:10px;box-shadow:var(--shadow)}
.notice{background:var(--accent-soft);color:var(--ink);border-radius:12px;padding:10px 12px;
font-size:13px;margin-bottom:12px}
.muted{color:var(--muted);font-size:13px}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px;border-bottom:1px solid var(--grid);vertical-align:top}
tr:last-child td{border-bottom:0}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
td.n,th.n{text-align:right;white-space:nowrap}
.big{font-weight:800;font-size:15px}
small{color:var(--muted);font-size:12px;line-height:1.35;display:inline-block}
.verdict{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}
.verdict.good{background:var(--accent);color:var(--accent-ink)}
.verdict.ok{background:var(--accent-soft);color:var(--accent)}
.verdict.meh{background:var(--surface-2);color:var(--muted)}
tr.compra .big{color:var(--accent)}
tr.valuta .big{color:var(--warn)}
/* occasioni: una scheda per capo, col link diretto all'annuncio */
.occ{border-bottom:1px solid var(--grid);padding:12px 0}
.occ:first-child{padding-top:2px}
.occ:last-child{border-bottom:0;padding-bottom:2px}
.occ .top{display:flex;gap:8px;align-items:flex-start;justify-content:space-between}
.occ .t{font-size:14px;font-weight:600;min-width:0}
.occ .meta{color:var(--muted);font-size:12px;margin-top:2px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0}
.kpi{min-width:0;background:var(--surface-2);border-radius:12px;padding:7px 9px}
.kpi .lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.kpi .val{font-size:17px;font-weight:800;line-height:1.2;letter-spacing:-.01em;
font-variant-numeric:tabular-nums}
.kpi.hi{background:var(--accent-soft)}
.kpi.hi .val{color:var(--accent)}
.kpi .val small{font-size:11px;font-weight:500}
.btn{border:0;border-radius:12px;padding:12px 16px;font:inherit;font-weight:600;cursor:pointer;
background:var(--accent);color:var(--accent-ink);display:inline-flex;align-items:center;
justify-content:center;gap:8px;text-decoration:none}
.btn.block{width:100%}
.btn.ghost{background:var(--surface-2);color:var(--ink)}
.vuoto{color:var(--muted);font-size:13px}
.azioni{display:flex;gap:8px}
.azioni .btn{flex:1}
.occ[hidden]{display:none}
.scartati{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:4px}
.scartati .btn{padding:7px 11px;font-size:13px;border-radius:10px}
.ripristina{border:0;background:none;color:var(--accent);font:inherit;font-size:13px;
padding:4px 0;cursor:pointer;text-align:left}
.elenco-scartati{margin-top:8px;font-size:13px}
.elenco-scartati div{padding:5px 0;border-bottom:1px solid var(--grid);
display:flex;gap:10px;justify-content:space-between;align-items:center}
.elenco-scartati div:last-child{border-bottom:0}
@media(max-width:420px){.kpis{grid-template-columns:repeat(2,1fr)}
.azioni{flex-direction:column}}
"""

# Lo scarto vive nel localStorage del telefono: la pagina e' statica su GitHub
# Pages, non c'e' nessun server a cui dirlo. Quindi la lista resta su quel
# dispositivo, sopravvive ai tre aggiornamenti al giorno (stessa origine) e il
# bot non ne sa niente: se cambi telefono, ricomincia da zero.
JS = """
(function(){
  var K='radar-scartati';
  function leggi(){try{return JSON.parse(localStorage.getItem(K))||{};}catch(e){return {};}}
  function scrivi(v){try{localStorage.setItem(K,JSON.stringify(v));}catch(e){}}
  var fuori=leggi();

  var barra=document.getElementById('barra-scartati');
  var conta=document.getElementById('conta-scartati');
  var elenco=document.getElementById('elenco-scartati');
  var bottone=document.getElementById('mostra-scartati');

  function aggiorna(){
    var schede=document.querySelectorAll('.occ[data-id]');
    var n=0;
    elenco.innerHTML='';
    schede.forEach(function(s){
      var id=s.dataset.id;
      if(fuori[id]){
        s.hidden=true; n++;
        var r=document.createElement('div');
        var t=document.createElement('span');
        t.textContent=(s.querySelector('.t')||{}).textContent ?
          s.querySelector('.t').childNodes[0].textContent.trim() : id;
        var b=document.createElement('button');
        b.className='ripristina'; b.type='button'; b.textContent='rimetti';
        b.onclick=function(){delete fuori[id];scrivi(fuori);aggiorna();};
        r.appendChild(t); r.appendChild(b); elenco.appendChild(r);
      } else { s.hidden=false; }
    });
    conta.textContent = n===0 ? 'Nessun capo scartato' :
      (n===1 ? '1 capo scartato' : n+' capi scartati');
    bottone.hidden = n===0;
    if(n===0){elenco.hidden=true;bottone.textContent='mostra';}
  }

  document.addEventListener('click',function(e){
    var b=e.target.closest('.scarta');
    if(b){
      var id=b.closest('.occ').dataset.id;
      fuori[id]=Date.now();
      scrivi(fuori); aggiorna();
      return;
    }
    if(e.target===bottone){
      elenco.hidden=!elenco.hidden;
      bottone.textContent=elenco.hidden?'mostra':'nascondi';
    }
  });

  aggiorna();
})();
"""


def scrivi_html(rif, occ, box, ore, ultimo):
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    oggi = datetime.now().strftime("%d/%m/%Y alle %H:%M")

    righe_std = "".join(
        f"<tr><td>{i['nome']}"
        + ("" if i["solido"] else " <small>stima incerta</small>")
        + f"<br><small>{i['n_venduti']} vendite &middot; se raro {i['ricavo_alto']:.0f} &euro;"
        + (f"<br>pubblica a {i['fascia']['da']}-{i['fascia']['a']} &euro;, "
           f"li' si vende il {100 * i['fascia']['quota']:.0f} %" if i["fascia"] else "")
        + f"</small></td><td class=n>{i['ricavo']:.0f} &euro;</td>"
        f"<td class='n big'>{i['paga_max']:.0f} &euro;</td></tr>"
        for nome, i in sorted(rif.items(), key=lambda kv: -kv[1]["paga_max"]))

    # sul telefono una lista di 244 schede e' illeggibile: le occasioni sono
    # gia' ordinate per margine, quindi le prime sono quelle che contano.
    resto = max(0, len(occ) - MAX_OCC_HTML)
    occ = occ[:MAX_OCC_HTML]

    if occ:
        pillola = {"COMPRA": "good", "VALUTA": "ok"}
        righe_occ = "".join(
            f"<div class='occ {o['verdetto'].lower()}' data-id='{o['id']}'>"
            f"<div class=top><div class=t>{(o['title'] or 'senza titolo')[:70]}"
            f"<div class=meta>{o['riferimento']} &middot; {o['condition']} &middot; {o['size']}"
            + ("" if o["solido"] else " &middot; stima incerta")
            + f" &middot; visto {eta_ore(o['first_seen'])}"
            + (" &middot; <b>link verificato</b>" if o.get("verificato") else "")
            + "</div></div>"
            f"<span class='verdict {pillola.get(o['verdetto'], 'meh')}'>{o['verdetto']}</span></div>"
            "<div class=kpis>"
            f"<div class=kpi><div class=lbl>chiede</div><div class=val>{float(o['price']):.0f} &euro;</div></div>"
            f"<div class=kpi><div class=lbl>ti costa</div><div class=val>{costo_pieno(float(o['price'])):.0f} &euro;</div></div>"
            f"<div class=kpi><div class=lbl>rivendi</div><div class=val>{o['ricavo']:.0f} &euro;</div></div>"
            f"<div class='kpi hi'><div class=lbl>margine</div>"
            f"<div class=val>{o['margine']:.0f} &euro; <small>{o['moltiplicatore']:.1f}&times;</small></div></div>"
            "</div>"
            "<div class=azioni>"
            f"<a class='btn' target=_blank rel=noopener "
            f"href='https://www.vinted.it/items/{o['id']}'>Apri l'annuncio &rarr;</a>"
            "<button class='btn ghost scarta' type=button>Scarta</button>"
            "</div>"
            "</div>"
            for o in occ)
        if resto:
            righe_occ += (f"<div class=vuoto>Altre {resto} occasioni piu' sottili non sono "
                          "in questa lista: qui ci sono le migliori per margine.</div>")
    else:
        eta = ""
        if ultimo:
            ore_fa = (datetime.now(timezone.utc) - ultimo).total_seconds() / 3600
            eta = (f" L'annuncio piu' recente nel database e' di {ore_fa:.0f} ore fa: "
                   f"se non torna niente, lancia <code>python tracker.py fetch</code>.")
        righe_occ = f"<div class=vuoto>Nessuna occasione in questa finestra.{eta}</div>"

    corpo = []
    for b in box:
        if b["margine"] is None:
            continue
        cls, et = esito_box(b)
        esaurita = "" if b["disponibile"] else " <small>(esaurita)</small>"
        corpo.append(
            f"<tr class='{cls}'><td>{b['label']}{esaurita}"
            f"<br><small>{b['taglio']} a {b['prezzo_box']:.0f} &euro; &middot; "
            f"{100 * b['quota_vendibili']:.0f} % vendibile &middot; {b['moltiplicatore']:.1f}&times;"
            f"<br>confronto: {b['composizione']}</small></td>"
            f"<td class=n>{b['per_capo']:.1f} &euro;</td>"
            f"<td class=n>{b['costo_per_vendibile']:.1f} &euro;</td>"
            f"<td class=n>{b['ricavo']:.0f} &euro;</td>"
            f"<td class='n big'>{b['margine']:.0f} &euro;</td>"
            f"<td class=n><span class='verdict "
            f"{ {'compra': 'good', 'valuta': 'ok'}.get(cls, 'meh') }'>{et}</span></td></tr>")
    righe_box = "".join(corpo) or "<tr><td colspan=6 class=vuoto>Nessuna box confrontabile.</td></tr>"

    html = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#141112">
<title>Radar acquisti</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div class="logo">V</div>
  <div><h1>Radar acquisti</h1>
    <p class="sub">Aggiornato il {oggi} &middot; finestra {ore} ore &middot; <a href="index.html">&larr; dashboard</a></p></div>
</header>

<div class="notice">
<b>Come leggerlo.</b> Un capo comprato a X non costa X: costa
X + 5 % + {PROTEZIONE_FISSA:.2f} &euro; di Protezione Acquisti
+ {SPEDIZIONE_ACQUISTO:.2f} &euro; di spedizione.<br><br>
<b>COMPRA</b> se guadagni almeno {MARGINE_COMPRA:.0f} &euro; netti <i>e</i> rifai almeno
{MOLTIPLIC_COMPRA}&times; il costo pieno.<br>
<b>VALUTA</b> fra {MARGINE_VALUTA:.0f} e {MARGINE_COMPRA:.0f} &euro; di margine: guardalo bene,
magari il capo e' migliore della media.<br>
<b>LASCIA</b> sotto {MARGINE_VALUTA:.0f} &euro;: non ripaga l'ora che ci metti fra misure,
foto e descrizione.
</div>

<h2>Occasioni</h2>
<div class="card">
{righe_occ}
</div>
<div class="card">
  <div class="scartati">
    <span class="muted" id="conta-scartati"></span>
    <button class="btn ghost" type="button" id="mostra-scartati" hidden>mostra</button>
  </div>
  <div class="elenco-scartati" id="elenco-scartati" hidden></div>
</div>
<p class="muted">Il prezzo e' solo il primo filtro. Apri l'annuncio e guarda foto, difetti e
misure prima di comprare: la taglia stimata ha gia' sbagliato due volte.</p>

<h2>Quanto puoi pagare</h2>
<div class="card"><div class="tbl-wrap">
<table>
<tr><th>gruppo</th><th class="n">rivendi a</th><th class="n">paga max</th></tr>
{righe_std}
</table>
</div></div>
<p class="muted"><b>Rivendi a</b> e' la mediana dei prezzi a cui quei capi si sono davvero
venduti &mdash; non la media, cosi' due pezzi da collezione finiti a 400 &euro; non falsano
il quadro. <b>Se raro</b> e' quanto fa lo stesso gruppo quando il pezzo e' ricercato: alcuni
gruppi hanno due mercati con un vuoto in mezzo, e in quel caso <i>rivendi a</i> usa solo
quello comune. Dove c'e' scritto <i>stima incerta</i> le vendite nel campione sono meno di
{VENDITE_SOLIDE}.</p>

<h2>Box dei fornitori</h2>
<div class="card"><div class="tbl-wrap">
<table>
<tr><th>box</th><th class="n">&euro;/capo</th><th class="n">costo utile</th>
<th class="n">rivendi</th><th class="n">margine</th><th class="n">esito</th></tr>
{righe_box}
</table>
</div></div>
<p class="muted">Sulla box reale da 136 &euro; erano vendibili 8 capi su 12, cioe' il 67 %:
il costo vero per capo era <b>17 &euro;</b>, non 11,33 &euro;. Le quote <i>vendibile</i> sono
stime per tipo di box, non dati misurati: correggile in <code>radar.py</code> quando avrai
aperto altre box.</p>

</div>
<script>{JS}</script>
</body>
</html>"""
    out = docs / "radar.html"
    out.write_text(html, encoding="utf-8")
    return out


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Radar d'acquisto del VintedTracker")
    ap.add_argument("--ore", type=int, default=48,
                    help="finestra per le occasioni, in ore (default 48)")
    ap.add_argument("--prezzo", type=float, help="giudica un capo a questo prezzo")
    ap.add_argument("--gruppo", help="gruppo di riferimento da usare con --prezzo")
    ap.add_argument("--titolo", default="", help="titolo del capo, per riconoscere il sottogruppo")
    ap.add_argument("--box", action="store_true", help="mostra solo le box dei fornitori")
    ap.add_argument("--html", action="store_true", help="scrive anche docs/radar.html")
    ap.add_argument("--verifica", type=int, nargs="?", const=VERIFICA_DEFAULT, default=0,
                    metavar="N",
                    help=f"apre le prime N occasioni su Vinted e scarta quelle "
                         f"gia' sparite (default {VERIFICA_DEFAULT}, un annuncio "
                         f"ogni 15-25 secondi)")
    a = ap.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"manca {DB_PATH}: lancia prima 'python tracker.py fetch'")

    con = sqlite3.connect(DB_PATH)
    righe = carica(con)
    rif = riferimenti(righe)

    if a.prezzo is not None:
        if not a.gruppo:
            print("Indica --gruppo fra questi:")
            for k in sorted(rif):
                print(f"  {k}")
            sys.exit(1)
        info = riferimento_per({"query_key": a.gruppo, "title": a.titolo}, rif)
        if not info:
            sys.exit(f"{a.gruppo}: nessun riferimento (campione troppo piccolo o nome sbagliato)")
        v, margine, molt = verdetto(info["ricavo"], a.prezzo)
        print()
        print(f"Capo in vendita a {a.prezzo:.0f} EUR")
        print(f"  riferimento usato   {info['nome']}"
              f"{'' if info['solido'] else '   (stima incerta: %d vendite)' % info['n_venduti']}")
        print(f"  ti costa davvero    {costo_pieno(a.prezzo):.2f} EUR")
        print(f"  lo rivendi a        {info['ricavo']:.0f} EUR"
              f"   (se e' in ottime condizioni: {info['ricavo_alto']:.0f} EUR)")
        if info["fascia"]:
            f = info["fascia"]
            print(f"  pubblicalo a        {f['da']}-{f['a']} EUR")
        print(f"  margine netto       {margine:.0f} EUR")
        print(f"  moltiplicatore      {molt:.1f}x")
        print(f"  paga al massimo     {info['paga_max']:.0f} EUR")
        print()
        print(f"  ESITO: {v}")
        print()
        return

    if a.box:
        stampa_box(giudica_box(rif))
        return

    ultimo = ultimo_fetch(righe)
    occ = occasioni(righe, rif, a.ore)
    if a.verifica:
        # verifico solo quelle che finiscono nella pagina, non tutte e 244
        occ = ancora_in_vendita(occ[:MAX_OCC_HTML], a.verifica) + occ[MAX_OCC_HTML:]
    box = giudica_box(rif)
    stampa_standard(rif)
    stampa_occasioni(occ, a.ore, ultimo)
    stampa_box(box)
    if a.html:
        print()
        print(f"scritto {scrivi_html(rif, occ, box, a.ore, ultimo)}")


if __name__ == "__main__":
    main()
