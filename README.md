# VintedTracker

Monitora ogni giorno prezzi e velocità di vendita su Vinted per combinazioni brand × categoria
(Carhartt, Nike, adidas, Levi's, The North Face, Wrangler) e pubblica una dashboard statica su GitHub Pages,
con una sezione "Stima da foto" che usa l'API Anthropic con la chiave personale dell'utente (salvata solo nel browser).

## Comandi

```
py tracker.py run      # scarica gli annunci, aggiorna vinted.db, rigenera docs/index.html
py tracker.py fetch    # solo raccolta
py tracker.py build    # solo dashboard
```

Automazione: `.github/workflows/tracker.yml` esegue la raccolta ogni notte su GitHub Actions e committa i dati.
`run.ps1` / `install_task.ps1` restano come alternativa locale (attività pianificata Windows).

## Come stima il prezzo di vendita

Vinted non pubblica i prezzi a cui gli articoli si vendono e il catalogo anonimo mostra al massimo
10 pagine (per le ricerche popolari poche ore di nuovi annunci). Il tracker quindi:

1. ogni giorno scarica le prime pagine di ogni ricerca per la fotografia del mercato (prezzi richiesti, concorrenza);
2. dalla pagina 1 campiona i 20 annunci più nuovi (età nota) e li segue nel tempo;
3. dopo 2 e 7 giorni apre la pagina di ciascun annuncio campionato: "Venduto" = venduto al prezzo corrente,
   `can_buy` = ancora in vendita, 404 = eliminato. I controlli sono lenti (3-5 s) per rispettare i limiti di Vinted.

I primi venduti compaiono dal 3° giorno, le percentuali a 7 giorni dall'8°.

## Configurazione

`config.json`: ricerche (`brand_ids`, `catalog_ids`, opzionale `search_text`), pagine per ricerca,
pause tra le richieste, soglia giorni "veloce".

ID brand: Nike 53, adidas 14, Levi's 10, Carhartt 362, The North Face 2319, Wrangler 259.
ID categorie uomo: giacche 2052, bomber 1223, giacche denim 1224, felpe 267, t-shirt 77, jeans 257.
ID categorie donna: giacche 1908, felpe 196, t-shirt 221, jeans 183.

## Note

Usa le pagine HTML pubbliche di vinted.it con poche richieste al giorno (circa 60) e pause casuali;
non richiede login. Il catalogo di vinted.it include gli annunci di tutta l'UE.
