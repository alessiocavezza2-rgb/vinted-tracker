#!/usr/bin/env python3
"""Calendario dei capi: una griglia del mese, con la foto nel giorno giusto.

Terza faccia della stessa agenda. Il radar ha la lista "Da fare" in ordine di
giorno; qui gli stessi promemoria stanno dentro un mese vero, con la miniatura
del capo nel riquadro del suo giorno: toccando il giorno esce cosa fare.

Niente di nuovo viene calcolato qui dentro. Le regole, i giorni e i prezzi sono
quelli di radar.calendario(): se cambiano le soglie, cambiano in radar.py e
questa pagina segue da sola.

La griglia la disegna il browser, non Python, per un motivo preciso: i
promemoria si possono SPOSTARE a un altro giorno, e uno spostamento deve
muovere la foto nella casella subito, senza aspettare il prossimo passaggio
del bot. Gli spostamenti stanno nel localStorage del telefono (radar-rinvii),
come lo scarto del radar: la pagina e' statica, non c'e' nessun server.

Lo stato "fatto" usa la STESSA chiave del radar (radar-fatti, stessa origine su
GitHub Pages): una cosa spuntata qui risulta spuntata anche nel radar.

    python calendario.py            riepilogo a terminale
    python calendario.py --html     scrive anche docs/calendario.html
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

# python -I non mette la cartella dello script nel path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar  # noqa: E402

ROOT = radar.ROOT
DOCS = ROOT / "docs"

# cosa vuol dire ogni colore, scritto una volta sola
LEGENDA = [
    ("subito", "!", "oggi, senza rinviare"),
    ("ripubblica", "R", "due settimane online: ripubblica"),
    ("alza", "+", "prezzo sotto la fascia che rende"),
    ("verifica", "?", "non e' piu' online: controlla"),
    ("nota", "·", "appuntamento scritto a mano"),
    ("aspetta", "…", "non toccarlo, lavora da solo"),
]
ORDINE = {u: i for i, (u, _, _) in enumerate(LEGENDA)}
SIMBOLO = {u: s for u, s, _ in LEGENDA}


def raccogli():
    """Le righe del calendario del radar, in ordine di giorno e urgenza."""
    con = sqlite3.connect(radar.DB_PATH)
    try:
        righe_db = radar.carica(con)
        rif = radar.riferimenti(righe_db)
        righe = radar.calendario(con, rif)
    finally:
        con.close()
    # ordino per il giorno in cui la cosa va fatta DAVVERO, non per la data
    # teorica: un promemoria scaduto il 9 novembre vale oggi, e ordinandolo per
    # la data vecchia finiva in cima al giorno davanti ai capi urgenti.
    oggi = date.today()
    righe.sort(key=lambda r: (max(r["data"], oggi), ORDINE.get(r["urgenza"], 9),
                              -(r["giorni"] or 0)))
    return righe


def per_giorno(righe):
    """Raggruppa per giorno, con gli scaduti su oggi (come fa il radar)."""
    giorni = {}
    for r in righe:
        giorni.setdefault(max(r["data"], date.today()), []).append(r)
    return giorni


def dati(righe):
    """I promemoria come li legge il browser: niente HTML, solo valori."""
    fuori = []
    for r in righe:
        fuori.append({
            "k": radar.chiave_task(r),
            "g": max(r["data"], date.today()).isoformat(),
            "u": r["urgenza"],
            "capo": (r["capo"] or "")[:80],
            "cosa": r["cosa"],
            "prezzo": round(r["prezzo"]) if r["prezzo"] else None,
            "consiglio": round(r["consiglio"]) if r["consiglio"] else None,
            "giorni": r["giorni"],
            "fav": r["fav"],
            "id": r["id"],
            "foto": r.get("foto") or None,
        })
    return fuori


CSS_IN_PIU = """
.navmese{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
.navmese .titolo{font-size:16px;font-weight:700;text-transform:capitalize}
.navmese button{border:1px solid var(--border);background:var(--surface-2);color:var(--ink);
font:inherit;font-size:17px;line-height:1;width:38px;height:38px;border-radius:12px;cursor:pointer}
.navmese button[disabled]{opacity:.35}
.dows,.celle{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.dow{text-align:center;font-size:10px;color:var(--muted);text-transform:uppercase;
letter-spacing:.06em;padding-bottom:4px}
.g{position:relative;min-height:56px;border:0;padding:3px 0 0;border-radius:10px;
background:var(--surface-2);font:inherit;color:var(--ink);display:flex;flex-direction:column;
align-items:center;gap:2px;cursor:default;overflow:hidden}
.g.fuori{background:transparent}
.g.libero{background:transparent;border:1px solid var(--grid)}
.g .num{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;line-height:1}
.g.oggi{outline:2px solid var(--accent);outline-offset:-2px}
.g.oggi .num{color:var(--accent);font-weight:800}
.g.pieno{cursor:pointer}
.g .foto{display:flex;gap:1px;align-items:center;justify-content:center;flex-wrap:nowrap;
padding:0 2px 5px;max-width:100%}
.mini{width:17px;height:22px;border-radius:4px;object-fit:cover;display:block;
background:var(--grid)}
.mini.senza{display:grid;place-items:center;font-size:9px;font-weight:800;color:var(--muted)}
.piu{font-size:9px;color:var(--muted);font-weight:700;padding-left:1px}
/* la striscia in basso dice l'urgenza senza bisogno di aprire il giorno */
.g.pieno::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px;
background:var(--muted)}
.g.u-subito::after,.g.u-ripubblica::after,.g.u-alza::after{background:var(--accent)}
.g.u-verifica::after{background:var(--warn)}
.g.u-aspetta::after{background:var(--grid)}
.g.spento .foto{opacity:.28}
.g.spento::after{background:var(--grid)}
.g.selezionato{background:var(--accent-soft)}
.g.selezionato .num{color:var(--accent)}
.task{display:flex;gap:10px;padding:12px 0;align-items:flex-start;
border-bottom:1px solid var(--grid)}
.task:last-of-type{border-bottom:0}
.grande{width:58px;height:76px;border-radius:10px;object-fit:cover;flex:0 0 auto;
background:var(--grid);display:block}
.grande.senza{display:grid;place-items:center;font-size:22px;font-weight:800;color:var(--muted)}
.task .corpo{min-width:0;flex:1}
.task .capo{font-size:14px;font-weight:600}
.task .cosa{font-size:13px;color:var(--ink-2);margin-top:2px}
.task .numeri{font-size:12px;color:var(--muted);margin-top:4px}
.task .numeri b{color:var(--accent);font-size:13px}
.task .spostato{font-size:12px;color:var(--warn);margin-top:3px}
.azioni{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;align-items:center}
.azioni .btn{padding:9px 13px;font-size:13px;border-radius:10px}
.sposta{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--muted)}
.quando{border:1px solid var(--border);background:var(--surface-2);color:var(--ink);
font:inherit;font-size:13px;padding:8px 10px;border-radius:10px}
.legenda{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted)}
.legenda span{display:inline-flex;align-items:center;gap:5px}
.legenda i{width:10px;height:10px;border-radius:3px;background:var(--muted);display:inline-block}
.legenda i.subito,.legenda i.ripubblica,.legenda i.alza{background:var(--accent)}
.legenda i.verifica{background:var(--warn)}
.legenda i.aspetta{background:var(--grid)}
@media(max-width:360px){.g{min-height:50px}.mini{width:14px;height:19px}}
"""

JS = r"""
(function(){
  var DATI = JSON.parse(document.getElementById('dati').textContent);
  var GIORNI = ["lunedi","martedi","mercoledi","giovedi","venerdi","sabato","domenica"];
  var MESI = ["gennaio","febbraio","marzo","aprile","maggio","giugno","luglio","agosto",
              "settembre","ottobre","novembre","dicembre"];

  /* Due cose vivono nel telefono, non nel database: le spunte "fatto" (chiave
     condivisa col radar) e gli spostamenti di giorno. La pagina e' statica su
     GitHub Pages: non c'e' nessun server a cui raccontarlo. Se cambi telefono,
     ricomincia da zero. */
  var K_FATTI='radar-fatti', K_RINVII='radar-rinvii';
  function leggi(k){try{return JSON.parse(localStorage.getItem(k))||{};}catch(e){return {};}}
  function scrivi(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
  var fatti=leggi(K_FATTI), rinvii=leggi(K_RINVII);

  var ora=new Date();
  var OGGI=iso(ora);
  function iso(d){
    return d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate());
  }
  function p2(n){return (n<10?'0':'')+n;}
  function daIso(s){var a=s.split('-');return new Date(+a[0],+a[1]-1,+a[2]);}
  function oggiChiave(){return ora.getFullYear()+'-'+(ora.getMonth()+1)+'-'+ora.getDate();}

  /* il giorno in cui un promemoria sta davvero: quello spostato a mano se c'e',
     altrimenti quello calcolato dal radar. Mai nel passato: un arretrato e'
     roba di oggi. */
  function giornoDi(t){
    var g = rinvii[t.k] || t.g;
    return g < OGGI ? OGGI : g;
  }
  function fatto(t){ return fatti[t.k]===oggiChiave(); }

  function inLettere(s){
    var d=daIso(s), o=daIso(OGGI);
    var diff=Math.round((d-o)/86400000);
    var nome=GIORNI[(d.getDay()+6)%7]+' '+d.getDate()+' '+MESI[d.getMonth()];
    /* l'anno solo quando non e' questo: il blazer e' segnato a marzo 2027 e
       "lunedi 1 marzo" da solo non dice se e' fra tre mesi o fra quindici */
    if(d.getFullYear()!==o.getFullYear()) nome+=' '+d.getFullYear();
    if(diff===0) return 'oggi, '+nome;
    if(diff===1) return 'domani, '+nome;
    if(diff>1 && diff<7) return nome+' (fra '+diff+' giorni)';
    return nome;
  }

  function miniatura(t,cls){
    if(t.foto){
      var i=document.createElement('img');
      i.className=cls; i.loading='lazy'; i.alt=t.capo; i.src=t.foto;
      return i;
    }
    var s=document.createElement('span');
    s.className=cls+' senza'; s.setAttribute('aria-hidden','true');
    s.textContent=(t.capo.match(/[A-Za-z0-9]/)||['?'])[0].toUpperCase();
    return s;
  }

  /* ---- griglia ---- */
  var mesiElenco=[], selezionato=null, iMese=0;
  var celle=document.getElementById('celle');
  var titolo=document.getElementById('titolo-mese');
  var prec=document.getElementById('mese-prec');
  var succ=document.getElementById('mese-succ');
  var pannello=document.getElementById('pannello');

  function raggruppa(){
    var m={};
    DATI.forEach(function(t){
      var g=giornoDi(t);
      (m[g]=m[g]||[]).push(t);
    });
    return m;
  }

  function calcolaMesi(mappa){
    var o=daIso(OGGI);
    var set=[o.getFullYear()+'-'+p2(o.getMonth()+1)];
    Object.keys(mappa).sort().forEach(function(g){
      var k=g.slice(0,7);
      if(set.indexOf(k)<0) set.push(k);
    });
    return set.slice(0,6);
  }

  function disegna(){
    var mappa=raggruppa();
    mesiElenco=calcolaMesi(mappa);
    if(iMese>=mesiElenco.length) iMese=mesiElenco.length-1;
    var parti=mesiElenco[iMese].split('-');
    var anno=+parti[0], mese=+parti[1];
    titolo.textContent=MESI[mese-1]+' '+anno;
    prec.disabled = iMese===0;
    succ.disabled = iMese===mesiElenco.length-1;

    celle.innerHTML='';
    var primo=new Date(anno,mese-1,1);
    var vuoti=(primo.getDay()+6)%7;
    for(var v=0;v<vuoti;v++) celle.appendChild(cella(null));
    var ultimo=new Date(anno,mese,0).getDate();
    for(var n=1;n<=ultimo;n++){
      var g=anno+'-'+p2(mese)+'-'+p2(n);
      celle.appendChild(cella(g,n,mappa[g]||[]));
    }
  }

  function cella(g,n,lista){
    if(g===null){
      var fuori=document.createElement('span');
      fuori.className='g fuori';
      return fuori;
    }
    var vivi=lista.filter(function(t){return !fatto(t);});
    var el=document.createElement(lista.length?'button':'span');
    el.className='g'+(g===OGGI?' oggi':'')+(lista.length?' pieno':' libero');
    if(lista.length){
      el.type='button';
      el.dataset.giorno=g;
      el.className+=' u-'+(vivi.length?vivi[0].u:lista[0].u);
      if(!vivi.length) el.className+=' spento';
      if(g===selezionato) el.className+=' selezionato';
      el.setAttribute('aria-label',inLettere(g)+': '+lista.length+' da fare');
    }
    var num=document.createElement('span');
    num.className='num'; num.textContent=n;
    el.appendChild(num);
    if(lista.length){
      var box=document.createElement('span');
      box.className='foto';
      /* In una casella da 39 pixel (telefono stretto, 7 colonne) ci stanno due
         miniature e basta. Se i capi sono di piu' ne mostro una e il resto in
         cifre: tre miniature andavano a capo e la riga del mese diventava
         alta il doppio. */
      var mostra=(vivi.length?vivi:lista);
      var quante=(mostra.length>2 ? 1 : mostra.length);
      mostra.slice(0,quante).forEach(function(t){
        box.appendChild(miniatura(t,'mini'));
      });
      if(mostra.length>quante){
        var piu=document.createElement('span');
        piu.className='piu'; piu.textContent='+'+(mostra.length-quante);
        box.appendChild(piu);
      }
      el.appendChild(box);
    }
    return el;
  }

  /* ---- il giorno aperto ---- */
  function apri(g){
    selezionato=g;
    var mappa=raggruppa();
    var lista=(mappa[g]||[]).slice();
    pannello.innerHTML='';
    var testa=document.createElement('div');
    testa.className='giorno'; testa.textContent=inLettere(g);
    pannello.appendChild(testa);

    var vivi=lista.filter(function(t){return !fatto(t);});
    if(!lista.length || !vivi.length){
      var nota=document.createElement('div');
      nota.className='vuoto';
      nota.textContent = lista.length ? 'Tutto fatto per questo giorno.'
                                      : 'Niente da fare in questo giorno.';
      pannello.appendChild(nota);
      disegna();
      return;
    }
    vivi.forEach(function(t){ pannello.appendChild(scheda(t,g)); });
    disegna();
  }

  function scheda(t,g){
    var riga=document.createElement('div');
    riga.className='task '+t.u;
    riga.appendChild(miniatura(t,'grande'));

    var corpo=document.createElement('div');
    corpo.className='corpo';
    corpo.appendChild(testo('capo',t.capo));
    corpo.appendChild(testo('cosa',t.cosa));

    var qualcosa = t.prezzo || t.giorni!==null && t.giorni!==undefined
                   || t.fav!==null && t.fav!==undefined;
    if(qualcosa){
      var num=document.createElement('div');
      num.className='numeri';
      if(t.prezzo){
        num.appendChild(document.createTextNode('ora '+t.prezzo+' €'));
        if(t.consiglio){
          num.appendChild(document.createTextNode(' → '));
          var b=document.createElement('b'); b.textContent=t.consiglio+' €';
          num.appendChild(b);
        }
      }
      var coda=[];
      if(t.giorni!==null && t.giorni!==undefined) coda.push(t.giorni+' giorni online');
      if(t.fav!==null && t.fav!==undefined) coda.push(t.fav+' preferiti');
      if(coda.length) num.appendChild(document.createTextNode(
        (t.prezzo?' · ':'')+coda.join(' · ')));
      corpo.appendChild(num);
    }
    if(rinvii[t.k] && rinvii[t.k]!==t.g){
      corpo.appendChild(testo('spostato','spostato a mano: il radar lo metteva '
        +inLettere(t.g<OGGI?OGGI:t.g)));
    }

    var azioni=document.createElement('div');
    azioni.className='azioni';
    if(t.id){
      var a=document.createElement('a');
      a.className='btn'; a.target='_blank'; a.rel='noopener';
      a.href='https://www.vinted.it/items/'+t.id;
      a.textContent="Apri l'annuncio →";
      azioni.appendChild(a);
    } else {
      var p=document.createElement('a');
      p.className='btn'; p.href='pubblica.html';
      p.textContent='Scheda di pubblicazione →';
      azioni.appendChild(p);
    }
    if(t.u!=='aspetta'){
      var f=document.createElement('button');
      f.className='btn ghost'; f.type='button'; f.textContent='Fatto';
      f.onclick=function(){
        fatti[t.k]=oggiChiave(); scrivi(K_FATTI,fatti); apri(g);
      };
      azioni.appendChild(f);
    }
    var data=document.createElement('input');
    data.className='quando'; data.type='date'; data.value=g;
    data.min=OGGI;
    data.setAttribute('aria-label','spostalo a un altro giorno');
    data.onchange=function(){
      if(!data.value) return;
      if(data.value===t.g) delete rinvii[t.k]; else rinvii[t.k]=data.value;
      scrivi(K_RINVII,rinvii);
      var vai=data.value;
      iMese=Math.max(0,mesiElenco.indexOf(vai.slice(0,7)));
      disegna();
      iMese=Math.max(0,mesiElenco.indexOf(vai.slice(0,7)));
      apri(vai);
    };
    /* il campo data da solo non dice a cosa serve: un'etichetta visibile,
       non solo per il lettore di schermo */
    var et=document.createElement('label');
    et.className='sposta';
    et.appendChild(document.createTextNode('sposta a'));
    et.appendChild(data);
    azioni.appendChild(et);
    corpo.appendChild(azioni);

    riga.appendChild(corpo);
    return riga;
  }

  function testo(cls,s){
    var d=document.createElement('div');
    d.className=cls; d.textContent=s;
    return d;
  }

  celle.addEventListener('click',function(e){
    var c=e.target.closest('.g.pieno');
    if(c) apri(c.dataset.giorno);
  });
  prec.onclick=function(){ if(iMese>0){ iMese--; disegna(); } };
  succ.onclick=function(){ if(iMese<mesiElenco.length-1){ iMese++; disegna(); } };

  var azzera=document.getElementById('azzera');
  azzera.onclick=function(){
    fatti={}; rinvii={};
    scrivi(K_FATTI,fatti); scrivi(K_RINVII,rinvii);
    /* torno a oggi: il giorno che stavo guardando potrebbe essere rimasto
       vuoto proprio perche' ho annullato lo spostamento che lo riempiva */
    iMese=0; disegna(); apri(OGGI);
  };

  disegna();
  var mappa=raggruppa();
  var primo=mappa[OGGI] ? OGGI : Object.keys(mappa).sort()[0];
  if(primo) apri(primo);
})();
"""


def scrivi_html(righe):
    DOCS.mkdir(exist_ok=True)
    quando = datetime.now().strftime("%d/%m/%Y alle %H:%M")
    giorni = per_giorno(righe)
    legenda = "".join(f"<span><i class='{u}'></i>{t}</span>" for u, _, t in LEGENDA)
    # "</" dentro un JSON chiuderebbe il tag script prima del tempo
    payload = json.dumps(dati(righe), ensure_ascii=False).replace("</", "<\\/")

    html_out = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#141112">
<title>Calendario dei capi</title>
<style>{radar.CSS}{CSS_IN_PIU}</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div class="logo">V</div>
  <div><h1>Calendario</h1>
    <p class="sub">Aggiornato il {quando} &middot; <a href="radar.html">radar</a>
    &middot; <a href="pubblica.html">pubblicazione</a>
    &middot; <a href="index.html">dashboard</a></p></div>
</header>

<div class="notice">
Tocca un giorno con la foto: sotto esce cosa fare con quel capo. La striscia di
colore sotto il riquadro dice quanto &egrave; urgente senza bisogno di aprirlo.<br><br>
Se un giorno non ti va bene, cambia la data nella scheda e la foto si sposta
nella casella nuova. Gli arretrati restano su oggi finch&eacute; non li sposti tu.
Le spunte sono le stesse del radar: quello che segni qui risulta segnato anche l&agrave;.
</div>

<div class="card">
<div class="navmese">
  <button type="button" id="mese-prec" aria-label="mese precedente">&lsaquo;</button>
  <div class="titolo" id="titolo-mese"></div>
  <button type="button" id="mese-succ" aria-label="mese successivo">&rsaquo;</button>
</div>
<div class="dows"><span class="dow">L</span><span class="dow">M</span><span class="dow">M</span>
<span class="dow">G</span><span class="dow">V</span><span class="dow">S</span>
<span class="dow">D</span></div>
<div class="celle" id="celle"></div>
</div>

<div class="card" id="pannello"></div>

<div class="card">
<div class="legenda">{legenda}</div>
<div class="riga-fatti">
  <span class="muted">Spunte e spostamenti stanno solo in questo telefono.</span>
  <button class="rimetti" type="button" id="azzera">rimetti tutto come dice il bot</button>
</div>
</div>

<p class="muted">{sum(len(v) for v in giorni.values())} promemoria in
{len(giorni)} giorni. I tempi vengono dalle coorti del tracker: sette giorni di pazienza,
quattordici per ripubblicare, ventuno e la cosa diventa urgente. Il prezzo consigliato
&egrave; la mediana dei venduti di quel gruppo; per le marche che il bot non segue non c'&egrave;
nessun prezzo, e non lo invento. Gli appuntamenti scritti a mano stanno in
<code>calendario.json</code>, e l&igrave; puoi mettere anche una tua <code>foto</code>.</p>

</div>
<script type="application/json" id="dati">{payload}</script>
<script>{JS}</script>
</body>
</html>"""
    out = DOCS / "calendario.html"
    out.write_text(html_out, encoding="utf-8")
    return out


def stampa(righe):
    oggi = date.today()
    giorni = per_giorno(righe)
    print()
    print("=" * 78)
    print("CALENDARIO DEI CAPI")
    print("=" * 78)
    if not giorni:
        print("Niente in agenda.")
        return
    for g in sorted(giorni):
        print()
        print(f"-- {radar.quando_in_lettere(g, oggi)}")
        for r in giorni[g]:
            prezzo = ""
            if r["prezzo"]:
                prezzo = (f"  [{r['prezzo']:.0f} -> {r['consiglio']:.0f} E]"
                          if r["consiglio"] else f"  [{r['prezzo']:.0f} E]")
            foto = "foto" if r.get("foto") else "    "
            print(f"   {SIMBOLO.get(r['urgenza'], '.')} {foto} {(r['capo'] or '')[:40]:<40}"
                  f" {r['cosa'][:38]}{prezzo}")
    print()
    print(f"{sum(len(v) for v in giorni.values())} promemoria in {len(giorni)} giorni. "
          f"Con foto: {sum(1 for r in righe if r.get('foto'))} su {len(righe)}.")


def main():
    ap = argparse.ArgumentParser(description="Calendario dei capi del VintedTracker")
    ap.add_argument("--html", action="store_true", help="scrive docs/calendario.html")
    a = ap.parse_args()

    righe = raccogli()
    stampa(righe)
    if a.html:
        print(f"Scritto {scrivi_html(righe)}")


if __name__ == "__main__":
    main()
