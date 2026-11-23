#!/usr/bin/env python3
"""Schede di pubblicazione: cosa scrivere, a che prezzo, con quali foto.

Pagina a parte dal radar: il radar dice cosa COMPRARE, questa dice come
PUBBLICARE quello che hai gia' in casa. Legge la tabella 'mine' (il bot la
riempie a ogni passaggio leggendo il profilo pubblico) piu' i capi non ancora
online scritti a mano in da_pubblicare.json.

Quello che e' misurato e quello che non lo e':
  MISURATO  il prezzo (mediana dei venduti del gruppo, centinaia di casi) e
            il fatto che la condizione dichiarata pesa poco rispetto alla
            fascia di prezzo.
  NON MISURATO  il testo del titolo e della descrizione. Le parole non
            spostano le vendite: a parita' di prezzo i titoli col nome del
            modello vendono quanto gli altri, o meno. Qui il valore e' avere
            il testo pronto da incollare dal telefono, non una formula magica.

    python pubblica.py            schede a terminale
    python pubblica.py --html     scrive anche docs/pubblica.html
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# python -I non mette la cartella dello script nel path: senza questa riga
# l'import di radar fallisce proprio quando il comando e' quello prudente.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar  # noqa: E402

ROOT = radar.ROOT
DOCS = ROOT / "docs"

# refusi gia' visti negli annunci veri
REFUSI = {"giubotto": "giubbotto", "giubitto": "giubbotto", "Nort face": "The North Face",
          "nord face": "The North Face"}

FOTO = [
    "intero davanti, appeso, su parete liscia e senza roba intorno",
    "intero dietro",
    "etichetta della marca",
    "etichetta con taglia e composizione",
    "zip, bottoni e polsini da vicino",
    "interno e fodera",
    "il difetto in primo piano, se c'e'",
    "dettaglio del tessuto a luce naturale",
]


def ripulisci(titolo):
    for sbagliato, giusto in REFUSI.items():
        titolo = re.sub(sbagliato, giusto, titolo, flags=re.I)
    return re.sub(r"\s{2,}", " ", titolo).strip(" ,")


def descrizione(capo):
    """Lo scheletro da riempire: le misure le sa solo chi ha il capo in mano."""
    return "\n".join([
        f"{capo['brand']}, taglia {capo['size']}.",
        "",
        "Misure a capo disteso:",
        "- spalle: ... cm",
        "- torace: ... cm",
        "- lunghezza: ... cm",
        "- manica: ... cm",
        "",
        f"Condizioni: {capo['condition'].lower()}. ...",
        "",
        "Spedizione in 24-48 ore. Scrivimi pure per altre foto o misure.",
    ])


def schede(con, rif):
    con.row_factory = sqlite3.Row
    capi = [dict(r) for r in con.execute("select * from mine")]
    if not capi:
        return []
    ultimo_giro = max((c["last_seen"] or "") for c in capi)

    out = []
    for c in capi:
        if c["sold"] or (c["last_seen"] or "") < ultimo_giro:
            continue          # venduto o non piu' online: non c'e' niente da pubblicare
        gruppo = radar.MARCA_GRUPPO.get((c["brand"] or "").strip().lower())
        info = radar.riferimento_per({"query_key": gruppo, "title": c["title"]}, rif) if gruppo else None
        cons, fascia = radar.prezzo_consigliato(info)
        out.append({
            "id": c["id"], "titolo": ripulisci(c["title"]), "marca": c["brand"],
            "taglia": c["size"], "condizione": c["condition"], "prezzo": c["price"],
            "consiglio": cons, "fascia": fascia, "fav": c["favourites"],
            "gruppo": info["nome"] if info else None,
            "n_venduti": info["n_venduti"] if info else None,
            "descrizione": descrizione(c), "online": True,
        })

    f = ROOT / "da_pubblicare.json"
    if f.exists():
        for v in json.loads(f.read_text(encoding="utf-8")):
            marca = v.get("marca", "")
            gruppo = radar.MARCA_GRUPPO.get(marca.strip().lower())
            info = radar.riferimento_per({"query_key": gruppo, "title": v.get("titolo", "")},
                                         rif) if gruppo else None
            cons, fascia = radar.prezzo_consigliato(info)
            capo = {"brand": marca, "size": v.get("taglia", "?"),
                    "condition": v.get("condizione", "Buone")}
            out.append({
                "id": None, "titolo": ripulisci(v.get("titolo", "")), "marca": marca,
                "taglia": capo["size"], "condizione": capo["condition"],
                "prezzo": None, "consiglio": v.get("prezzo") or cons, "fascia": fascia,
                "fav": None, "gruppo": info["nome"] if info else None,
                "n_venduti": info["n_venduti"] if info else None,
                "descrizione": v.get("descrizione") or descrizione(capo),
                "online": False, "nota": v.get("nota", ""),
            })
    return out


def stampa(sch):
    print()
    print("=" * 100)
    print("SCHEDE DI PUBBLICAZIONE")
    print("=" * 100)
    for s in sch:
        print()
        print(f"  {'ONLINE' if s['online'] else 'DA PUBBLICARE'}  {s['titolo'][:70]}")
        if s["consiglio"]:
            ora = f" (ora {s['prezzo']:.0f})" if s["prezzo"] else ""
            print(f"    prezzo      {s['consiglio']:.0f} EUR{ora}"
                  f"   mediana di {s['gruppo']} su {s['n_venduti']} vendite")
        else:
            print("    prezzo      marca fuori dal campione del bot: nessun riferimento")
        print(f"    condizione  {s['condizione']}")
        if s.get("nota"):
            print(f"    nota        {s['nota']}")


def scrivi_html(sch):
    DOCS.mkdir(exist_ok=True)
    oggi = datetime.now().strftime("%d/%m/%Y alle %H:%M")

    corpo = []
    for n, s in enumerate(sch):
        chiave = f"c{s['id']}" if s["id"] else f"n{n}"
        if s["consiglio"]:
            ora = (f"<small>ora {s['prezzo']:.0f} &euro;</small>" if s["prezzo"]
                   else "<small>non ancora online</small>")
            prezzo = (f"<div class=kpi><div class=lbl>pubblica a</div>"
                      f"<div class=val>{s['consiglio']:.0f} &euro;</div></div>"
                      f"<div class=kpi><div class=lbl>adesso</div><div class=val>{ora}</div></div>")
            fonte = (f"mediana di {s['gruppo']} su {s['n_venduti']} vendite"
                     if s["gruppo"] else "")
        else:
            prezzo = ("<div class=kpi><div class=lbl>pubblica a</div>"
                      "<div class=val>&mdash;</div></div>")
            fonte = "marca fuori dal campione del bot: il prezzo lo decidi tu"

        link = (f" &middot; <a target=_blank rel=noopener "
                f"href='https://www.vinted.it/items/{s['id']}'>apri l'annuncio</a>"
                if s["id"] else "")

        foto = "".join(
            f"<label class=voce><input type=checkbox data-foto='{chiave}-{i}'>"
            f"<span>{t}</span></label>" for i, t in enumerate(FOTO))

        corpo.append(
            f"<h3 class=capo-nome>{'' if s['online'] else '<b>da pubblicare</b> &middot; '}"
            f"{s['titolo'][:52]}</h3>"
            "<div class=card>"
            f"<p class=fonte>{s['marca']} &middot; taglia {s['taglia']} "
            f"&middot; {s['condizione']}{link}</p>"
            f"<div class=kpis-2>{prezzo}</div>"
            + (f"<p class=fonte>{fonte}</p>" if fonte else "")
            + (f"<p class=nota-capo>{s['nota']}</p>" if s.get("nota") else "")
            + "<div class=campo><div class=etichetta>titolo</div>"
            f"<input class=testo data-salva='{chiave}-t' value=\"{s['titolo']}\">"
            f"<button class='btn ghost copia' type=button data-copia='{chiave}-t'>copia</button></div>"
            "<div class=campo><div class=etichetta>descrizione &mdash; scrivi le misure e i difetti</div>"
            f"<textarea class=testo rows=11 data-salva='{chiave}-d'>{s['descrizione']}</textarea>"
            f"<button class='btn ghost copia' type=button data-copia='{chiave}-d'>copia</button></div>"
            f"<div class=campo><div class=etichetta>foto</div>{foto}</div>"
            "</div>")

    righe = "".join(corpo) or "<div class=vuoto>Nessun capo da pubblicare.</div>"

    html = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#141112">
<title>Schede di pubblicazione</title>
<style>{radar.CSS}{CSS_IN_PIU}</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div class="logo">V</div>
  <div><h1>Pubblicazione</h1>
    <p class="sub">Aggiornato il {oggi} &middot; <a href="calendario.html">calendario</a> &middot; <a href="radar.html">radar</a> &middot;
    <a href="index.html">dashboard</a></p></div>
</header>

<div class="notice">
<b>Cosa e' misurato qui.</b> Solo il prezzo: e' la mediana dei capi di quel
gruppo che si sono davvero venduti, su centinaia di casi.<br><br>
<b>Cosa non lo e'.</b> Il titolo e la descrizione. Ho controllato se certe parole
vendono di piu' e, a parita' di prezzo, non succede: su The North Face fra 20 e 35 &euro;
i titoli col nome del modello vendono il 9,5 % contro il 13,1 % degli altri. Era il prezzo
a fare la differenza, non le parole. Quindi qui il testo serve solo ad averlo pronto da
incollare, non a vendere di piu'.<br><br>
Quello che scrivi resta su questo telefono e si salva mentre scrivi.
</div>

{righe}

<p class="muted">I capi non ancora online stanno in <code>da_pubblicare.json</code>:
aggiungine uno con <code>titolo</code>, <code>marca</code>, <code>taglia</code> e
<code>nota</code>, e compare qui con il suo prezzo calcolato.</p>

</div>
<script>{JS}</script>
</body>
</html>"""
    out = DOCS / "pubblica.html"
    out.write_text(html, encoding="utf-8")
    return out


CSS_IN_PIU = """
.capo-nome{font-size:13px;margin:18px 0 6px;color:var(--ink-2);font-weight:600;
text-transform:none;letter-spacing:0}
.capo-nome b{color:var(--accent)}
.kpis-2{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-bottom:8px}
.fonte{font-size:12px;color:var(--muted);margin:0 0 10px}
.nota-capo{font-size:13px;color:var(--ink);background:var(--accent-soft);
border-radius:12px;padding:9px 11px;margin:0 0 10px}
.campo{margin-top:12px}
.etichetta{font-size:10px;color:var(--muted);text-transform:uppercase;
letter-spacing:.04em;margin-bottom:4px}
.testo{width:100%;font:inherit;font-size:14px;padding:10px 12px;border-radius:12px;
border:1px solid var(--border);background:var(--page);color:var(--ink);resize:vertical}
textarea.testo{line-height:1.5}
.copia{margin-top:6px;padding:8px 14px;font-size:13px;border-radius:10px}
.copia.ok{background:var(--accent);color:var(--accent-ink)}
.voce{display:flex;gap:9px;align-items:flex-start;padding:6px 0;font-size:13px;
border-bottom:1px solid var(--grid)}
.voce:last-child{border-bottom:0}
.voce input{margin-top:2px;width:18px;height:18px;accent-color:var(--accent);flex:0 0 auto}
.voce input:checked + span{color:var(--muted);text-decoration:line-through}
"""

# Tutto quello che l'utente scrive vive nel localStorage del telefono: la pagina
# e' statica su GitHub Pages e si rigenera tre volte al giorno, quindi il testo
# NON puo' stare nell'HTML o sparirebbe al primo aggiornamento.
JS = """
(function(){
  var K='pubblica-testi', C='pubblica-foto';
  function leggi(k){try{return JSON.parse(localStorage.getItem(k))||{};}catch(e){return {};}}
  function scrivi(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
  var testi=leggi(K), foto=leggi(C);

  document.querySelectorAll('[data-salva]').forEach(function(el){
    var id=el.dataset.salva;
    if(typeof testi[id]==='string') el.value=testi[id];
    el.addEventListener('input',function(){testi[id]=el.value;scrivi(K,testi);});
  });

  document.querySelectorAll('[data-foto]').forEach(function(el){
    var id=el.dataset.foto;
    el.checked=!!foto[id];
    el.addEventListener('change',function(){
      if(el.checked){foto[id]=1;}else{delete foto[id];}
      scrivi(C,foto);
    });
  });

  document.addEventListener('click',function(e){
    var b=e.target.closest('.copia');
    if(!b) return;
    var campo=document.querySelector('[data-salva="'+b.dataset.copia+'"]');
    if(!campo) return;
    var finito=function(){
      b.classList.add('ok'); var prima=b.textContent; b.textContent='copiato';
      setTimeout(function(){b.classList.remove('ok');b.textContent=prima;},1200);
    };
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(campo.value).then(finito,function(){
        campo.select(); document.execCommand('copy'); finito();
      });
    } else { campo.select(); document.execCommand('copy'); finito(); }
  });
})();
"""


def main():
    ap = argparse.ArgumentParser(description="Schede di pubblicazione del VintedTracker")
    ap.add_argument("--html", action="store_true", help="scrive anche docs/pubblica.html")
    a = ap.parse_args()

    if not radar.DB_PATH.exists():
        sys.exit(f"manca {radar.DB_PATH}: lancia prima 'python tracker.py fetch'")

    con = sqlite3.connect(radar.DB_PATH)
    rif = radar.riferimenti(radar.carica(con))
    sch = schede(con, rif)
    stampa(sch)
    if a.html:
        print()
        print(f"scritto {scrivi_html(sch)}")


if __name__ == "__main__":
    main()
