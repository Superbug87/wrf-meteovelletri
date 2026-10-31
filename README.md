# WRF per Meteo Velletri

Previsioni a 48 ore con **WRF** (NCAR) su due zone, ognuna con due modelli di partenza. Tutto gira da solo sui server
gratuiti di GitHub; le mappe finiscono in quattro rami di risultati che le pagine del sito leggono direttamente.

| zona | domini | modello di partenza | workflow | ramo dei risultati |
|---|---|---|---|---|
| Castelli Romani | 9 → 3 → 1 km | ECMWF IFS | `castelli-ecmwf.yml` | `risultati-castelli-ecmwf` |
| Castelli Romani | 9 → 3 → 1 km | ICON-2I | `castelli-icon2i.yml` | `risultati-castelli-icon2i` |
| Lazio | 27 → 9 → 3 km | ECMWF IFS | `lazio-ecmwf.yml` | `risultati-lazio-ecmwf` |
| Lazio | 27 → 9 → 3 km | ICON-2I | `lazio-icon2i.yml` | `risultati-lazio-icon2i` |

Il Lazio gira a 27 → 9 → 3 km dal 20/10/2026 (prima 18 → 6 → 2 km, troppo lento). La vecchia configurazione resta in `zone/lazio3/`
solo per le prove: nessun run normale la usa.

Riunisce in un solo repository i progetti `wrf-castelli`, `wrf-lazio` e `wrf-icon2i`.

## Avvertenze
- **Progetto personale e sperimentale, non un servizio ufficiale.** Le previsioni sono prodotte in automatico da un modello
  numerico, senza il controllo di un meteorologo, e possono essere sbagliate, anche di molto. Per pioggia e temporali
  posizione e orario possono sbagliare di chilometri e di ore.
- **Non sostituiscono i bollettini e le allerte ufficiali** (Protezione Civile, Aeronautica Militare, centri funzionali
  regionali). Non usarle per decisioni che riguardano la sicurezza di persone o cose: protezione civile, volo,
  navigazione, lavori o eventi a rischio.
- **Nessuna garanzia.** Sono fornite «così come sono», senza garanzie di esattezza, continuità o disponibilità: il servizio
  può ritardare, fermarsi o dare errori. L'autore non risponde di danni derivanti dall'uso.
- **Nessun legame con i fornitori dei dati.** Il progetto non è collegato a ECMWF, Agenzia ItaliaMeteo, Arpae, NOAA, NCAR/UCAR, Weather Underground né
  agli altri fornitori elencati sotto e non è da loro approvato o rivisto.

## Come funziona
- **Quattro workflow**, uno per zona e modello, due cicli al giorno (00 e 12 UTC). Ognuno ha la sua coda: girano in parallelo e non si aspettano a vicenda.
  Non partono da soli: li lancia il **regista** all'orario giusto (vedi «Partenza a orario»).
- Tutti chiamano lo stesso **flusso comune** (`.github/workflows/previsione.yml`): compilazione di WRF/WPS (una volta, in cache), dati statici
  della zona (geogrid, in cache), scelta del ciclo, dati di partenza, ungrib, metgrid, real, stazioni, WRF a segmenti da 16 ore (fino a 4 job in fila,
  perché GitHub ferma un job dopo 6 ore), controllo di plausibilità, mappe, pubblicazione. I run girano su macchine ARM di GitHub (circa un terzo più veloci).
- **ICON-2I** (`wrf/scarica_icon2i.py`) si scarica una sola volta per ciclo da MeteoHub, che limita le richieste e lo spazio dell'account (circa 1 GB):
  i Castelli partono prima e mettono i dati in cache, il Lazio li ritrova. Se i dati non sono ancora usciti lo scarico aspetta fino a circa 20 minuti.
  Se MeteoHub non risponde in tempo, la rete cade o la richiesta resta in elaborazione, la richiesta viene annullata e rifatta (fino a 3 volte; una sola
  per la richiesta troppo lenta). A ogni scarico si cancellano le richieste dell'account più vecchie di 2 ore, e se MeteoHub risponde «quota superata»
  si liberano quelle di più di 20 minuti e si riprova.
- Da ICON-2I entrano anche acqua di nube, ghiaccio, pioggia, neve e graupel, sia nello stato iniziale sia ai bordi (`have_bcs_moist`): nubi e piogge che
  arrivano da fuori dominio non nascono più da zero. Sono richieste a parte (una alla volta). `IDRO=0` torna al comportamento precedente.
  ECMWF open data non ha questi campi, quindi per ECMWF resta com'era.
- **Nudging sulle stazioni** (`wrf/stazioni_wrf.py`, `strumenti/stazioni_ufficiali.py`): nelle prime ore, fino all'ultima osservazione
  disponibile, WRF viene tenuto vicino alla temperatura e all'umidità misurate; dopo prosegue libero. Fonti (`NUDGING_DEFAULT` nel flusso
  comune, in uso `wu`, o il campo `nudging` delle prove): `wu` (stazioni Weather Underground), `ufficiali` (Regione Lazio, temperatura ogni 15 minuti
  e umidità dove c'è; Aeronautica Militare, SYNOP orari, METAR di riserva) o `tutte` (le ufficiali più le WU controllate contro di esse
  e riassunte in una «superosservazione» per riquadro di 3 km sui Castelli e 6 km sul Lazio, saltando i riquadri con una stazione ufficiale).
  Con le ufficiali la temperatura si riporta alla quota del terreno di WRF (6,5 K/km). Se una fonte non risponde si va avanti con le altre.
- **Stazioni Weather Underground**: si usano **tutte** quelle del dominio fine, senza tetto. La ricerca procede a riquadri che si suddividono finché le
  10 stazioni più vicine coprono il riquadro (circa 1000 stazioni sui Castelli, circa 2000 sul Lazio), la lettura è in parallelo (`WU_THREAD`,
  `WU_CHIAMATE_AL_SECONDO`) e si ferma dopo 4 risposte 429 di fila. Un controllo di qualità confronta ogni stazione con le 12 vicine riportate alla stessa quota
  e scarta valori, salti nel tempo e sensori bloccati. Un ciclo del Lazio fa circa 5000 chiamate, uno dei Castelli circa 1500: molto più delle 1500 al giorno
  dichiarate da Weather Underground, quindi la chiave è il punto fragile del sistema (vedi “Se qualcosa non va”).
- **Chiave Weather Underground**: si legge dal segreto `WU_API_KEY`. Se WU risponde 401 (chiave rifiutata) `strumenti/wu_chiave.py` prova le chiavi che trova nella
  pagina pubblica di una stazione scelta a caso tra quelle del Lazio nella verifica (poi IGENZANO3 e IVELLE79) e usa la prima che WU accetta per la ricerca e la lettura;
  se nessuna funziona resta il segreto. Il segreto serve anche da password che cifra le osservazioni passate da un segmento all'altro, quindi non va svuotato.
  Senza stazioni il run non si ferma: gira libero e le mappe escono senza correzione.
- **Correzione delle mappe con le stazioni**: è una riserva, solo se il nudging non c'è stato (chiave rifiutata, stazioni non disponibili): dopo il calcolo le mappe
  vengono corrette con le stazioni WU lette in quel momento. Se anche questo non riesce, le mappe escono col solo modello.
- **Controllo di plausibilità** (`wrf/controllo.py`) prima delle mappe: terreno della cornice, valori mancanti, temperatura, vento, corrente verticale,
  pioggia e salti di pressione. Se il run non è plausibile **non si pubblica nulla** e sul sito resta la previsione precedente.
- **Radar** (`wrf/pioggia_radar.py`): le ultime ore di pioggia di WRF a confronto con il radar della Protezione Civile, e la pioggia delle 6 ore successive
  come mix di radar spostato col nowcast ANVIL (pysteps; se manca, moto uniforme) e WRF, con peso sempre maggiore a WRF. Se il radar non risponde le mappe di WRF escono lo stesso.
- **Temperatura del mare**: MUR SST di NASA JPL (circa 1 km) al posto di quella del modello globale, quando raggiungibile.
- **Pubblicazione**: ogni workflow scrive solo nel suo ramo (un solo commit, la storia delle immagini non si accumula):
  `ultimo/` (fogli di sintesi), `archivio/<ciclo>/`, `orarie/` (un'immagine per ora e variabile, con `indice.json` e `cicli.json`) ed `estratti/` (per la verifica).

## Orari dei cicli
Ora italiana = UTC + 2 fino al 25/10/2026 (ora legale), poi UTC + 1. Il regista lancia con al più 5 minuti di ritardo.

| workflow | partenza dopo il ciclo | cicli 00 / 12 UTC | in ora italiana (CEST) | pubblicazione tipica (CEST) |
|---|---|---|---|---|
| Castelli ICON-2I | +3:15 | 03:15 / 15:15 | 05:15 / 17:15 | circa 06:20-06:30 / 18:20-19:00 |
| Lazio ICON-2I | +3:50 | 03:50 / 15:50 | 05:50 / 17:50 | circa 06:30-06:45 / 18:30-19:15 |
| Castelli ECMWF | +7:50 | 07:50 / 19:50 | 09:50 / 21:50 | circa 11:10-11:30 / 23:20-23:30 |
| Lazio ECMWF | +7:50 | 07:50 / 19:50 | 09:50 / 21:50 | circa 11:10-11:30 / 23:20-23:30 |

I dati escono su MeteoHub circa 2 ore dopo il ciclo (ICON-2I) e su ECMWF circa 7 ore e 35 minuti dopo. ICON-2I parte prima perché lo scarico aspetta se mancano.
Un run dura circa 1h45-2h25 per ICON-2I e circa 1h30 per ECMWF, più l'attesa in coda se i runner sono affollati.
La verifica giornaliera parte dopo le 05:20 UTC.

## Struttura
- `zone/castelli/`, `zone/lazio/` (e `zone/lazio3/`, solo prove): il codice di ciascuna zona (domini, WPS/WRF, stazioni, mappe). Il flusso comune copia la cartella
  della zona nella radice prima di lavorare, quindi `wrf/` e `strumenti/` stanno fianco a fianco durante un run.
  Le parti uguali nelle zone (compilazione, controllo, scarico dei dati, ricerca stazioni) sono per ora copiate in ciascuna.
- `strumenti/`: tutto ciò che sta fuori dalla zona.
  - `regista.py`: decide cosa lanciare e quando.
  - `verifica.py`, `estrai_verifica.py`, `verifica.sh`: verifica giornaliera e pagella; `aggiungi_stazioni_wu.py` e `stazioni_verifica.json`: elenco delle stazioni di verifica.
  - `stazioni_ufficiali.py` (stazioni ufficiali per il nudging, collaudo in `prova_ufficiali.py`), `wu_chiave.py` (chiave WU che funziona).
  - `pulisci_meteohub.py`: elenca o cancella le richieste dell'account MeteoHub.
  - `genera_pagine.py` e `modello_pagina.html`: pagine del sito con la scelta del modello.
  - Valutazioni: `valuta_modelli_stazioni.py`, `valuta_modelli_radar.py` (confronto per scadenza di tutte le versioni), `valuta_nowcast.py`, `valuta_ciclo_rapido.py`,
    `probabilita.py` e `concordanza.sh` (mappe di concordanza della pioggia), `pioggia_oraria_d03.py`, `tempi_run.py` (durata dei run).
  - `pubblica_estratto.sh`, `pubblica_prova.sh`, `pubblica_prove_radar.sh`, `verifica_ciclo_rapido.sh`: pubblicazione nei rami delle prove e della verifica.
- `sito/`: le pagine da caricare a mano sul sito (si caricano una volta: le immagini le prendono da GitHub):
  `previsioni-wrf.html` (Castelli), `previsioni-wrf-lazio.html` (Lazio), `confronto-wrf.html` e `confronto-wrf-lazio.html` (confronto tra le versioni), `pagella-wrf.html` (verifica),
  `stato-wrf.html` (ultimo ciclo pubblicato, run in corso e prove attive, letto dal ramo `regista`) e `meteovelletri.html` (dati in tempo reale della stazione).

## Rami del repository
| ramo | contenuto |
|---|---|
| `main` | il codice |
| `risultati-<zona>-<modello>` | le quattro pubblicazioni (`ultimo/`, `archivio/`, `orarie/`, `estratti/`) |
| `verifica` | `giorni/AAAA-MM-GG.json` e `pagella.json` |
| `prove-fisica` | gli estratti delle prove (20 giorni di cicli) |
| `prove-<zona>-<modello>[-<variante>]` | l'ultimo ciclo di ogni prova, per le pagine «Confronto modelli» |
| `prove-radar` | pioggia oraria delle prove del radar e valutazione del ciclo rapido (`valutazione/`) |
| `prove-radar-diario` | diario delle prove del radar (note di lavoro) |
| `concordanza` | le mappe di concordanza della pioggia |
| `regista` | memoria del regista (`lanciate.txt`) e `stato.json` per `stato-wrf.html` |
| `arm`, `confronto`, `idrometeore`, `nuovi-piloti` | rami di lavoro del 19/10/2026 (ARM, pagina «Confronto modelli», idrometeore, nuovi piloti), già riuniti in `main`, non usati dai run |

## Verifica giornaliera e pagella
- Durante ogni run, dopo il controllo, `strumenti/estrai_verifica.py` salva i valori del modello nei punti delle stazioni e la pioggia oraria
  su una griglia ridotta (`estratti/` nel ramo dei risultati, pochi kB a run, 31 giorni).
- Ogni mattina `verifica.yml` (`strumenti/verifica.py`) confronta il giorno prima con le osservazioni: stazioni del Centro Funzionale della
  Regione Lazio (temperatura, pioggia, vento), SYNOP dell'Aeronautica Militare (temperatura, rugiada, vento, pressione), radar della
  Protezione Civile (pioggia, con 10 km di tolleranza) e stazioni Weather Underground «a norma» (lista `wu` di `stazioni_verifica.json`: storico orario di temperatura,
  rugiada e vento, indici `t2_wu`, `td2_wu`, `vento_wu`). Come riferimento: previsioni a breve di ECMWF e ICON-2I originali (Open-Meteo).
- Le stazioni WU si aggiungono con il workflow `stazioni-wu.yml` (ID separati da spazi, oppure `--campione N` per una prova con N stazioni sparse, non controllate).
  Se la chiave WU non vale, le stazioni WU saltano senza fermare la verifica.
- Si contano solo le ore **dopo la fine del nudging**: prima il modello vedeva le stazioni (con le ufficiali, proprio quelle della verifica)
  e quelle ore erano già passate quando la previsione è uscita.
- Dal 26/10/2026 (somme di **versione 2**): il **vento** si confronta solo con i SYNOP (anemometro a 10 m in campo aperto, come il vento del modello);
  le stazioni della Regione, con sensori bassi o riparati (23% di calme sui Castelli, rapporto modello/osservato 1,6-1,7 in tutte le versioni contro ~1,0 dei SYNOP),
  restano solo come informazione (`vento_reg`). Il **punto di rugiada** si confronta anche sulle stazioni della Regione (da temperatura e umidità), alla quota della stazione.
  Per ogni ciclo si tengono le somme di temperatura, rugiada e vento: la pagella confronta ogni prova con il WRF normale **ciclo per ciclo** sugli stessi cicli
  (`confronti` in `pagella.json`, con l'incertezza a 2 deviazioni standard; un po' ottimista perché i cicli si sovrappongono). I giorni di versione 1 non contano per vento e rugiada.
- Risultati nel ramo `verifica`: `giorni/AAAA-MM-GG.json` (somme del giorno) e `pagella.json` (ultimi 7 e 30 giorni), letto da `sito/pagella-wrf.html`.
- Elenco delle stazioni: `strumenti/stazioni_verifica.json` (da rifare se la rete regionale cambia).
- **Confronti per scadenza**: `valuta_modelli_stazioni.py` e `valuta_modelli_radar.py` mettono a confronto tutte le versioni (ufficiali e prove) da +1 a +48 ore
  su temperatura, rugiada, umidità, vento (solo SYNOP), e sulla pioggia contro il radar.
- **Concordanza**: ogni 3 ore `concordanza.yml` confronta la pioggia delle versioni dello stesso ciclo e pubblica le mappe nel ramo `concordanza`.

## Segreti del repository (Settings → Secrets and variables → Actions)
- `MH_EMAIL`, `MH_PASSWORD`: account MeteoHub (per ICON-2I).
- `WU_API_KEY`: chiave Weather Underground per il nudging, per la correzione delle mappe e per la verifica (senza, WRF gira libero). Cifra anche le
  osservazioni passate da un segmento all'altro, quindi deve restare sempre la stessa durante un run.

## Partenza a orario (regista)
I lanci programmati di GitHub nei repository nuovi arrivano con ore di ritardo o saltano. Per questo i run li lancia `regista.yml` (`strumenti/regista.py`).
- **Chi lo sveglia**: un lavoro su [cron-job.org](https://cron-job.org) ogni **5 minuti**, che fa
  `POST https://api.github.com/repos/Superbug87/wrf-meteovelletri/actions/workflows/regista.yml/dispatches` con corpo `{"ref":"main"}` e intestazioni
  `Authorization: Bearer <token>` (token «fine-grained» limitato a questo repository e al permesso Actions) e `Accept: application/vnd.github+json`;
  la risposta giusta è 204, e cron-job.org avvisa se fallisce. Come riserva il regista ha anche il cron di GitHub (minuti 7, 22, 37 e 52 di ogni ora), che spesso arriva in ritardo.
  Nessun altro workflow ha un lancio programmato, a parte `concordanza.yml` (ogni 3 ore, minuto 50).
- **Cosa decide**: a ogni sveglia, per ogni workflow, prende l'ultimo ciclo il cui orario di partenza è passato (da meno di 10 ore) e lo lancia se non è già
  pubblicato (cartella `archivio/<ciclo>` nel ramo dei risultati), se non c'è già un run in coda o in corso e se i tentativi sono meno di 3, con almeno 50 minuti dalla
  partenza dell'ultimo (se un run fallisce si riprova subito dopo quei 50 minuti, ma non all'infinito). Lo stesso vale per la verifica del mattino.
- **Prove e ciclo rapido**: il regista lancia anche le prove (`sperimentale.yml`, vedi sotto), le prove del radar (`radar-prove.yml`) e il ciclo rapido con la sua verifica
  (`ciclo-rapido-verifica.yml`). Una prova parte solo se il run ufficiale dello stesso ciclo, zona e modello è partito da almeno 25 minuti e non è fallito;
  ogni sveglia ne lancia al massimo 3 e in tutto ce ne sono al più 12 in coda o in corso (`MAX_PROVE`), per lasciare i runner ai run normali.
- **Memoria**: l'elenco delle prove già lanciate sta in `lanciate.txt` del ramo `regista`, non nella lista dei run di GitHub (che si può ripulire senza che il regista
  rilanci prove già fatte).
- **Se qualcosa non parte**: lanciare a mano il regista, `gh workflow run regista.yml -R Superbug87/wrf-meteovelletri --ref main`, oppure il workflow del ciclo con il campo `ciclo`.
  Se i runner di GitHub si affollano, si può rallentare cron-job.org a 10 o 15 minuti.

## Se qualcosa non va
Comandi utili (serve `gh`):
`gh run list -R Superbug87/wrf-meteovelletri --workflow castelli-icon2i.yml --limit 5` (stesso con `lazio-icon2i.yml`, `castelli-ecmwf.yml`, `lazio-ecmwf.yml`, `regista.yml`),
`--status failure --limit 5` per i fallimenti, `--status queued` per la coda.
- **«Wunderground rifiuta la chiave (HTTP 401)»**: il segreto `WU_API_KEY` non vale più. Il ciclo prosegue lo stesso (con la ricerca della chiave di riserva, se funziona);
  per risolvere bene si crea una chiave nuova nel proprio account Weather Underground (Member Settings → API Keys) e si aggiorna il segreto. Nei log cercare «Chiave WU del segreto rifiutata» e «Uso la chiave WU trovata».
- **«Disk quota exceeded» o timeout di MeteoHub**: ora lo scarico libera lo spazio e ritenta da solo. A mano, il workflow `pulizia.yml` elenca le richieste dell'account e, con `cancella`,
  le toglie tutte: **non usarlo mentre uno scarico ICON-2I è in corso**.
- **Un ciclo non è stato pubblicato**: il controllo di plausibilità lo ha bloccato (sul sito resta il precedente) oppure i 3 tentativi del regista sono finiti. Si rilancia a mano il workflow con il campo `ciclo`.
- **Troppi lavori in coda**: il limite dei runner gratuiti è di 20 lavori contemporanei per account; le prove cedono il passo ai run normali, ma una coda lunga ritarda tutto.

## Avvio a mano
Actions → il workflow della zona e del modello → **Run workflow**. Campi: `ciclo` (vuoto = l'ultimo), `ore` (48), `ore_segmento` (16),
`stazioni`, `mare` (temperatura del mare MUR), `radar` (confronto con il radar della Protezione Civile).

**Prove** (`sperimentale.yml`, Actions → PROVE): un run che non pubblica nulla, con la zona (`castelli`, `lazio`, `lazio3`), il modello (`ecmwf`, `icon2i`, `iconeu`, `gfs`), il ciclo e le stazioni
del nudging scelti (`wu`, `ufficiali`, `tutte`, `nessuno`); `controllo` tiene fuori dal nudging un quarto fisso delle stazioni della Regione,
per misurare l'effetto su stazioni che il modello non ha visto; `fisica` sceglie una variante. Mappe ed estratto restano negli allegati del run (14 giorni).

Altri workflow a mano: `stazioni-wu.yml` (aggiunge stazioni WU alla verifica), `estratti-arretrati.yml` (ricava l'estratto da un run già fatto, finché gli allegati esistono, 3 giorni),
`pulizia.yml` (MeteoHub), `diagnosi-d01.yml`, `diagnosi-idro.yml` e `prova-ecmwf.yml` (diagnosi una tantum, non toccano i run). `prova.yml` parte a ogni modifica del codice: controlla i workflow (actionlint) e per ogni zona tutto quello
che si può provare senza WRF (namelist, stazioni inventate, file del mare, mappe da file WRF inventati, controllo di plausibilità); non pubblica nulla.

## Prove di fisica e di piloti
Per scegliere la fisica migliore si confrontano, sugli stessi cicli, stesse stazioni e stesso nudging del run normale (fisica YSU,
suolo Noah, `topo_wind` 1, Thompson, RRTMG, convezione Grell-Freitas sui domini esterni, cioè il 9 km dei Castelli e il 27 e 9 km del Lazio, esplicita sui nidi), alcune varianti, una per ciascuna sigla della variabile `FISICA` letta da `config.py`:

| sigla | cosa cambia |
|---|---|
| `topo2` | `topo_wind` 2 (variante della correzione del vento sul terreno) |
| `mynn` | PBL MYNN 2.5 (`bl_pbl_physics` 5) con lo strato superficiale MYNN (`sf_sfclay_physics` 5); `topo_wind` a 0, perché è una correzione del solo YSU |
| `noahmp` | suolo Noah-MP (`sf_surface_physics` 4) |
| `mynn-noahmp` | le due cose insieme (con MYNN il `topo_wind` è spento, quindi non esiste un «tutte e tre») |
| `radt6`, `radt15` | solo Castelli: la radiazione (RRTMG) si ricalcola ogni 6 o 15 minuti nel dominio da 1 km invece di 3 |
| `urbano` | modello urbano a strato singolo (UCM) sulle celle urbane (Roma) |
| `livelli` | 50 livelli invece di 40, primo strato di 20 m invece di 50 (`dzbot` 20, `dzstretch_s` 1,2): 8 livelli sotto i 300 m invece di 4, per lo strato freddo notturno e la nebbia |
| `diff2` | solo Castelli: `diff_opt` 2 nel dominio da 1 km (diffusione orizzontale nello spazio fisico, ridotta sui pendii) |
| `raggi` | nudging più locale: `obs_rinxy` del dominio fine dimezzato (Castelli 12 km, Lazio 25 km) e `obs_dpsmx` 3 cb (peso nullo oltre ~260 m di dislivello) |
| `cu27`, `idroec` | prove singole sul Lazio: convezione esplicita sul 27 km, idrometeore dai dati ECMWF |

Le lancia il regista (`PROVE` in `strumenti/regista.py`) per le quattro combinazioni zona/modello, con finestre di date diverse:
`noahmp` dal 19/10 al 7/11 (prorogato il 31/10 di una settimana; poi si decide se diventa la fisica in uso), `radt6` e `radt15` dal 20/10 e `urbano` dal 21/10 fino al 7/11, `livelli` e `raggi` (e `diff2` sui Castelli)
dal ciclo 26/10 12Z all'8/11. `topo2`, `mynn` e `mynn-noahmp` (dal 19/10) sono finite il 26/10: peggioravano il vento o rendevano le notti più calde. Partono dopo i run normali (ICON-2I 3:30, ECMWF 8:20 dopo il ciclo)
perché i dati globali sono già in cache (i dati ECMWF si scaricano una volta per zona e ciclo: chiave `ecmwf-dati-v1-<zona>-<ciclo>-<ore>`).
Non fanno le mappe: lasciano solo l'estratto, negli allegati restano 1 giorno. Quando una variante risulta migliore del run normale su almeno una settimana, si porta nella fisica in uso.

**Piloti in prova** (mai pubblicati, 5 ore dopo il ciclo 00/12): ICON-EU del DWD (`scarica_iconeu.py`, acqua di nube e ghiaccio, 60 livelli del modello) e GFS di NOAA
(ghiaccio, pioggia, neve, graupel sui livelli di pressione; la nube liquida non c'è; `Vtable.GFS_IDRO`). Estratti nel ramo `prove-fisica` come `<zona>-<pilota>`;
la pagella li mostra in una riga ciascuno e nella vista «Tutti insieme». Sui Castelli con ECMWF girano anche le prove del nudging con `ufficiali` e `tutte` (fino al 25/10/2026).

**Ciclo rapido con il radar** (in ombra, fino al 05/11/2026): ogni 3 ore un WRF corto (9 ore) sul Lazio parte dall'ora piena con i dati ECMWF dell'ultimo ciclo disponibile, una volta senza radar (`rad0`)
e una con il radar nell'avvio (`radq-radnubi`, `wrf/radar_nudging.py`); `valuta_ciclo_rapido.py` li confronta con il radar, col nowcast e con le previsioni ufficiali.
Non pubblica nulla: scrive nel ramo `prove-radar`.

## Stato
- 18/10/2026: primi run riusciti di tutti e quattro i workflow, controllo di plausibilità passato, mappe pubblicate. Dal 20/10 il Lazio gira a 27 → 9 → 3 km.
- 22-23/10/2026: l'avvio passa dalle sveglie di Claude a cron-job.org ogni 5 minuti, e ICON-2I parte prima (Castelli +2:15, Lazio +2:35).
- 24/10/2026: il Lazio ICON-2I passa a +2:50 (35 minuti dopo i Castelli) perché i due scarichi MeteoHub non si sovrappongano e non riempiano la quota.
- 23-24/10/2026: la chiave WU smette di valere (401) dalle 19:52 UTC del 23/10; i Castelli procedono senza stazioni, il Lazio si fermava al passo delle mappe. Corretto: il Lazio non si ferma più,
  c'è la ricerca di una chiave di riserva, il segreto è stato rinnovato e i due cicli persi (Lazio e Castelli ECMWF 12Z) sono stati rifatti con le stazioni. Lo scarico MeteoHub ora ritenta e si libera lo spazio da solo
  (una richiesta dimenticata di 359 MB aveva fatto cadere il Castelli ICON-2I 12Z del 24/10).
- Il nudging sulle stazioni resta attivo: confrontato con 27 stazioni della Regione Lazio migliora la temperatura a 2 m (errore tipico 1,27 °C contro 1,51 °C senza).
- Risultati dei confronti (6 cicli, 20-22/10, un solo evento di pioggia: da prendere con prudenza; analisi del 26/10, confronto ciclo per ciclo):
  mattino troppo caldo (+1,8/+2,9 K alle 07 UTC) e notti calde (+0,7/+1,1 K), quasi niente nebbia (umidità ≥ 97% nel 2-6% dei casi contro il 18-24% osservato),
  aria secca sul Lazio (rugiada -1,0 K, peggio il secondo giorno); vento giusto sui SYNOP dei Castelli e +40% su quelli del Lazio.
  Noah-MP è l'unica variante che migliora davvero (errore di temperatura -0,12/-0,18 °C, rugiada +0,6/+0,8 K), ma scalda un po' le notti del Lazio.
- Da verificare: WRF a volte produce piovaschi leggeri al mattino che i modelli globali non hanno (non dipende dal nudging né dalla temperatura del mare); il confronto con le stazioni WU «a norma» (aspetta gli ID);
  il pulsante «Fogli/Radar/Concordanza» nelle pagine WRF; il caricamento automatico sul sito (FTP) non è attivo, le pagine si caricano a mano.

## Fonti dei dati e licenze
| cosa | fonte | condizioni |
|---|---|---|
| Dati di partenza e al contorno (ECMWF) | [ECMWF IFS open data](https://www.ecmwf.int/en/forecasts/datasets/open-data), 0,25°, [doi:10.21957/open-data](https://doi.org/10.21957/open-data) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) e condizioni d'uso ECMWF. **Dati modificati**: ritagliati sull'Italia, ricodificati e usati come condizioni iniziali e al contorno di WRF. ECMWF non garantisce i dati e non risponde di errori o omissioni |
| Dati di partenza e al contorno (ICON-2I) | ICON-2I, 2,2 km: **Agenzia ItaliaMeteo in cooperation with Arpae Emilia-Romagna Idro-Meteo-Clima Service**, tramite [MeteoHub](https://meteohub.agenziaitaliameteo.it/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (pagina «Attributions & Licenses» di MeteoHub). **Dati modificati**: ritagliati, portati su livelli di pressione e ricodificati in GRIB2 per WRF |
| Dati di partenza alternativi | [GFS di NOAA/NCEP](https://www.nco.ncep.noaa.gov/pmb/products/gfs/) | dati pubblici del governo statunitense; NOAA non approva questo lavoro |
| Temperatura del mare (facoltativa) | [MUR SST](https://podaac.jpl.nasa.gov/dataset/MUR-JPL-L4-GLOB-v4.1) di NASA JPL (0,01°, circa 1 km), via [NOAA CoastWatch ERDDAP](https://coastwatch.pfeg.noaa.gov/erddap/), citazione: JPL MUR MEaSUREs Project, 2015, [doi:10.5067/GHGMR-4FJ04](https://doi.org/10.5067/GHGMR-4FJ04) | dati NASA ad accesso libero, si chiede di citare la fonte |
| Radar (facoltativo) | [Radar-DPC](https://radar.protezionecivile.it), Dipartimento della Protezione Civile, mosaico nazionale SRI (intensità di pioggia) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): va citata la fonte e le immagini ricavate (`radar_*`) hanno la stessa licenza. Dati usati solo per le due immagini radar, non ripubblicati in forma grezza; il CSV contiene solo medie sull'area |
| Modello | [WRF e WPS](https://www2.mmm.ucar.edu/wrf/users/public.html) di NCAR/UCAR | software pubblico, non incluso qui: lo compila il flusso di lavoro dai sorgenti ufficiali |
| Terreno e uso del suolo | dati geografici [WPS_GEOG](https://www2.mmm.ucar.edu/wrf/users/download/get_sources_wps_geog.html) di NCAR/UCAR (tra cui GMTED2010 di USGS/NGA e MODIS) | scaricati durante il calcolo, non inclusi qui; vale la licenza di ciascun dato |
| Confini provinciali e comunali | confini amministrativi ISTAT, elaborati da [Openpolis](https://github.com/openpolis/geojson-italy) ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)) | semplificati (25 m) e ritagliati sull'area delle mappe in `dati/confini.json`; la fonte va citata (Confini: ISTAT, elaborati da Openpolis, CC BY 4.0) |
| Contorno dei laghi | [ESA WorldCover 2021](https://esa-worldcover.org) v200 (acqua a 10 m) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium; contorni semplificati in `dati/laghi.json` |
| Stazioni meteo | rete di stazioni amatoriali di [Weather Underground](https://www.wunderground.com/) (The Weather Company), tramite la chiave nel segreto `WU_API_KEY`; se Weather Underground la rifiuta (errore 401), `strumenti/wu_chiave.py` cerca una chiave tra quelle incorporate nella pagina pubblica di una stazione e usa la prima che WU accetta | restano soggette ai termini d'uso di Weather Underground, che vanno rispettati da chi usa il progetto (una chiave del sito non è una chiave personale: può essere cambiata o bloccata in qualsiasi momento). I dati **non vengono ripubblicati**: si scaricano durante il calcolo, entrano in WRF e non finiscono nei risultati; i file temporanei che passano da un segmento all'altro sono cifrati; i log non riportano codici o posizioni delle stazioni; le mappe mostrano solo l'uscita del modello |
| Stazioni ufficiali (nudging e verifica) | Regione Lazio, rete del Centro Funzionale, dal portale pubblico [temporeale.regione.lazio.it](https://temporeale.regione.lazio.it) (account pubblico); SYNOP dell'Aeronautica Militare (dati WMO) tramite [OGIMET](https://www.ogimet.com); METAR tramite [aviationweather.gov](https://aviationweather.gov) (NOAA) | dati consultabili pubblicamente; si scaricano durante il calcolo e per la verifica e **non si ripubblicano in forma grezza** (nei risultati restano solo il modello e gli scarti medi della pagella). Prima di un uso diverso da quello personale le condizioni della Regione vanno chieste o verificate |
| Solo per `previsioni_castelli.py` / `previsioni_lazio.py` usati da soli | [Open-Meteo](https://open-meteo.com/) ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); l'API gratuita è per uso non commerciale) e quote dal modello digitale del terreno Copernicus DEM («contiene dati Copernicus modificati») | non usati dalla catena WRF |

Librerie principali: ecCodes (Apache 2.0), JasPer, NumPy, SciPy, Matplotlib, netCDF4, requests, pyproj, OpenCV, tifffile: ciascuna con la sua licenza.
I nomi dei paesi e le posizioni sulle mappe sono scritti a mano nello script, non vengono da OpenStreetMap o altri archivi.
Le condizioni dei fornitori possono cambiare: prima di usare il progetto in modo diverso da quello personale
(per esempio per un servizio pubblico o commerciale) vanno ricontrollate.
- 27-29/10/2026: da MeteoHub scarica solo il run ufficiale dei Castelli (una volta per ciclo, dati in cache); Lazio e prove partono solo con la cache presente e, senza, si fermano senza chiamare MeteoHub. Il 29/10 si è visto che MeteoHub pubblica ICON-2I circa 3 ore dopo il ciclo: Castelli ICON-2I +3:15, Lazio +3:50.
