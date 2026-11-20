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

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--bd:#e2e2e4;--mu:#65686d;--ok:#0a7d32;--mid:#b26a00;--card:#f6f6f7}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16181c;--fg:#e8e8e8;--bd:#2f3237;--mu:#9aa0a6;--ok:#4ade80;--mid:#fbbf24;--card:#1e2126}}
:root[data-theme=dark]{--bg:#16181c;--fg:#e8e8e8;--bd:#2f3237;--mu:#9aa0a6;
--ok:#4ade80;--mid:#fbbf24;--card:#1e2126}
*{box-sizing:border-box}
body{margin:0 auto;padding:16px;max-width:920px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,system-ui,Segoe UI,sans-serif}
h1{font-size:21px;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:17px;margin:30px 0 6px;padding-bottom:5px;border-bottom:1px solid var(--bd)}
.sub{color:var(--mu);font-size:13px;margin-bottom:18px}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:14px}
th,td{padding:8px 7px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--mu);font-weight:600}
.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.big{font-weight:700}
small{color:var(--mu);font-size:12px}
a{color:inherit;text-decoration:none;border-bottom:1px solid var(--bd)}
tr.compra .big,tr.compra .badge{color:var(--ok)}
tr.valuta .big,tr.valuta .badge{color:var(--mid)}
.badge{font-size:11px;font-weight:700;letter-spacing:.04em;white-space:nowrap}
.nota{background:var(--card);border-radius:9px;padding:12px 14px;font-size:13px;
color:var(--mu);margin:10px 0 0}
.nota b{color:var(--fg)}
.regola{background:var(--card);border-radius:9px;padding:14px;font-size:14px;margin:0 0 6px}
.regola b{color:var(--fg)}
.vuoto{color:var(--mu)}
"""


def scrivi_html(rif, occ, box, ore, ultimo):
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    oggi = datetime.now().strftime("%d/%m/%Y alle %H:%M")

    righe_std = "".join(
        f"<tr><td>{i['nome']}"
        + ("" if i["solido"] else " <small>stima incerta</small>")
        + (f"<br><small>pubblica a {i['fascia']['da']}-{i['fascia']['a']} &euro; "
           f"&middot; li' si vende il {100 * i['fascia']['quota']:.0f} %</small>"
           if i["fascia"] else "")
        + f"</td><td class=n>{i['ricavo']:.0f} &euro;</td>"
        f"<td class=n>{i['ricavo_alto']:.0f} &euro;</td>"
        f"<td class='n big'>{i['paga_max']:.0f} &euro;</td>"
        f"<td class=n>{i['n_venduti']}</td></tr>"
        for nome, i in sorted(rif.items(), key=lambda kv: -kv[1]["paga_max"]))

    if occ:
        righe_occ = "".join(
            f"<tr class='{o['verdetto'].lower()}'>"
            f"<td><a href='https://www.vinted.it/items/{o['id']}'>{(o['title'] or '')[:48]}</a>"
            f"<br><small>{o['riferimento']} &middot; {o['condition']} &middot; {o['size']}"
            + ("" if o["solido"] else " &middot; stima incerta")
            + "</small></td>"
            f"<td class=n>{float(o['price']):.0f} &euro;</td>"
            f"<td class=n>{costo_pieno(float(o['price'])):.0f} &euro;</td>"
            f"<td class=n>{o['ricavo']:.0f} &euro;</td>"
            f"<td class='n big'>{o['margine']:.0f} &euro;</td>"
            f"<td class=n>{o['moltiplicatore']:.1f}&times;</td>"
            f"<td class=badge>{o['verdetto']}</td></tr>"
            for o in occ)
    else:
        eta = ""
        if ultimo:
            ore_fa = (datetime.now(timezone.utc) - ultimo).total_seconds() / 3600
            eta = (f" L'annuncio piu' recente nel database e' di {ore_fa:.0f} ore fa: "
                   f"se non torna niente, lancia <code>python tracker.py fetch</code>.")
        righe_occ = f"<tr><td colspan=7 class=vuoto>Nessuna occasione in questa finestra.{eta}</td></tr>"

    corpo = []
    for b in box:
        if b["margine"] is None:
            continue
        cls, et = esito_box(b)
        esaurita = "" if b["disponibile"] else " <small>(esaurita)</small>"
        corpo.append(
            f"<tr class='{cls}'><td>{b['label']}{esaurita}"
            f"<br><small>taglio migliore: {b['taglio']} &middot; {b['prezzo_box']:.0f} &euro; "
            f"&middot; confronto: {b['composizione']}</small></td>"
            f"<td class=n>{b['per_capo']:.1f} &euro;</td>"
            f"<td class=n>{100 * b['quota_vendibili']:.0f} %</td>"
            f"<td class=n>{b['costo_per_vendibile']:.1f} &euro;</td>"
            f"<td class=n>{b['ricavo']:.0f} &euro;</td>"
            f"<td class='n big'>{b['margine']:.0f} &euro;</td>"
            f"<td class=n>{b['moltiplicatore']:.1f}&times;</td>"
            f"<td class=badge>{et}</td></tr>")
    righe_box = "".join(corpo) or "<tr><td colspan=8 class=vuoto>Nessuna box confrontabile.</td></tr>"

    html = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar acquisti</title>
<style>{CSS}</style>
</head>
<body>
<h1>Radar acquisti</h1>
<div class="sub">Aggiornato il {oggi} &middot; finestra {ore} ore &middot; <a href="index.html">&larr; dashboard</a></div>

<div class="regola">
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

<h2>Quanto puoi pagare</h2>
<div class="wrap">
<table>
<tr><th>gruppo</th><th class="n">rivendi a</th><th class="n">se raro</th>
<th class="n">paga max</th><th class="n">vendite</th></tr>
{righe_std}
</table>
</div>
<div class="nota"><b>Rivendi a</b> e' la mediana dei prezzi a cui quei capi si sono davvero
venduti &mdash; non la media, cosi' due pezzi da collezione finiti a 400 &euro; non falsano
il quadro. <b>Se raro</b> e' quanto fa lo stesso gruppo quando il pezzo e' ricercato: alcuni gruppi
hanno due mercati con un vuoto in mezzo, e in quel caso <i>rivendi a</i> usa solo quello comune. Dove c'e' scritto <i>stima incerta</i> le vendite nel campione sono meno di
{VENDITE_SOLIDE}.</div>

<h2>Occasioni</h2>
<div class="wrap">
<table>
<tr><th>capo</th><th class="n">prezzo</th><th class="n">ti costa</th><th class="n">rivendi</th>
<th class="n">margine</th><th class="n">&times;</th><th>esito</th></tr>
{righe_occ}
</table>
</div>
<div class="nota">Il prezzo e' solo il primo filtro. Prima di comprare apri l'annuncio e guarda
foto, difetti e misure: la taglia stimata ha gia' sbagliato due volte.</div>

<h2>Box dei fornitori</h2>
<div class="wrap">
<table>
<tr><th>box</th><th class="n">&euro;/capo</th><th class="n">vendibili</th>
<th class="n">costo utile</th><th class="n">rivendi</th><th class="n">margine</th>
<th class="n">&times;</th><th>esito</th></tr>
{righe_box}
</table>
</div>
<div class="nota">Sulla box reale da 136 &euro; erano vendibili 8 capi su 12, cioe' il 67 %:
il costo vero per capo era <b>17 &euro;</b>, non 11,33 &euro;. Le quote della colonna
<i>vendibili</i> sono stime per tipo di box, non dati misurati: correggile in
<code>radar.py</code> quando avrai aperto altre box.</div>

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
    box = giudica_box(rif)
    stampa_standard(rif)
    stampa_occasioni(occ, a.ore, ultimo)
    stampa_box(box)
    if a.html:
        print()
        print(f"scritto {scrivi_html(rif, occ, box, a.ore, ultimo)}")


if __name__ == "__main__":
    main()
