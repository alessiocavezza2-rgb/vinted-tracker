# VintedTracker

Monitora ogni giorno prezzi e velocitÃ  di vendita su Vinted per combinazioni brand Ã— categoria
(Carhartt, Nike, adidas, Levi's, The North Face) e pubblica una dashboard statica su GitHub Pages.

## Comandi

```
py tracker.py run      # scarica gli annunci, aggiorna vinted.db, rigenera docs/index.html
py tracker.py fetch    # solo raccolta
py tracker.py build    # solo dashboard
.\run.ps1              # run + git commit + git push (usato dall'attivitÃ  pianificata)
```

Automazione: `powershell -ExecutionPolicy Bypass -File install_task.ps1` registra l'attivitÃ 
pianificata "VintedTracker" (ogni giorno alle 03:30).

## Come stima il prezzo di vendita

Vinted non pubblica i prezzi a cui gli articoli si vendono. Il tracker scarica ogni giorno le prime
pagine di ogni ricerca ordinate per "piÃ¹ recenti" e ricorda ogni annuncio. Un annuncio visto nei giorni
precedenti, ma assente oggi pur essendo dentro la finestra coperta, Ã¨ sparito dal catalogo: nella grande
maggioranza dei casi Ã¨ stato venduto. Il prezzo che aveva al momento della sparizione Ã¨ la stima del
prezzo di vendita. Gli annunci giÃ  esistenti al primo giorno di raccolta hanno etÃ  sconosciuta e non
contano per le statistiche di velocitÃ .

Le statistiche "venduto â‰¤7 gg" diventano significative dopo circa 8-10 giorni di raccolta.

## Configurazione

`config.json`: ricerche (`brand_ids`, `catalog_ids`, opzionale `search_text`), pagine per ricerca,
pause tra le richieste, soglia giorni "veloce".

ID brand: Nike 53, adidas 14, Levi's 10, Carhartt 362, The North Face 2319, Wrangler 259.
ID categorie uomo: giacche 2052, bomber 1223, giacche denim 1224, felpe 267, t-shirt 77, jeans 257.
ID categorie donna: giacche 1908, felpe 196, t-shirt 221, jeans 183.

## Note

Usa le pagine HTML pubbliche di vinted.it con poche richieste al giorno (circa 50) e pause casuali;
non richiede login. Il catalogo di vinted.it include gli annunci di tutta l'UE.

