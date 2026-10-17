# WRF per Meteo Velletri

Previsioni a 48 ore con **WRF** (NCAR) su due zone, ognuna con due modelli di partenza. Tutto gira da solo sui server
gratuiti di GitHub; le mappe finiscono in quattro rami di risultati che le pagine del sito leggono direttamente.

| zona | domini | modello di partenza | workflow | ramo dei risultati |
|---|---|---|---|---|
| Castelli Romani | 9 → 3 → 1 km | ECMWF IFS | `castelli-ecmwf.yml` | `risultati-castelli-ecmwf` |
| Castelli Romani | 9 → 3 → 1 km | ICON-2I | `castelli-icon2i.yml` | `risultati-castelli-icon2i` |
| Lazio | 18 → 6 → 2 km | ECMWF IFS | `lazio-ecmwf.yml` | `risultati-lazio-ecmwf` |
| Lazio | 18 → 6 → 2 km | ICON-2I | `lazio-icon2i.yml` | `risultati-lazio-icon2i` |

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
- **Quattro workflow**, uno per zona e modello, con orari propri (due cicli al giorno, 00 e 12 UTC, più un lancio di riserva per ciclo).
  Ognuno ha la sua macchina e la sua coda: girano in parallelo e non si aspettano a vicenda.
- Tutti chiamano lo stesso **flusso comune** (`.github/workflows/previsione.yml`): compilazione di WRF/WPS (una volta, in cache), dati statici
  della zona (geogrid, in cache), scelta del ciclo, dati di partenza, ungrib, metgrid, real, WRF a segmenti da 16 ore, controllo, mappe, pubblicazione.
- **ICON-2I** si scarica una sola volta per ciclo (MeteoHub limita le richieste): i Castelli partono prima e lo mettono in cache, il Lazio lo ritrova.
- **Controllo di plausibilità** (`wrf/controllo.py`) prima delle mappe: terreno della cornice, valori mancanti, temperatura, vento, corrente verticale,
  pioggia e salti di pressione. Se il run non è plausibile **non si pubblica nulla** e sul sito resta la previsione precedente.
- **Pubblicazione**: ogni workflow scrive solo nel suo ramo (un solo commit, la storia delle immagini non si accumula):
  `ultimo/` (fogli di sintesi), `archivio/<ciclo>/`, `orarie/` (un'immagine per ora e variabile, con `indice.json` e `cicli.json`).

## Struttura
- `zone/castelli/`, `zone/lazio/`: il codice di ciascuna zona (domini, WPS/WRF, stazioni, mappe). Il flusso comune copia la cartella della zona nella radice prima di lavorare.
  Le parti uguali nelle due zone (compilazione, controllo, scarico dei dati) sono per ora copiate in entrambe.
- `strumenti/`: `genera_pagine.py` (pagine del sito con la scelta del modello), `pulisci_meteohub.py` (cancella le richieste vecchie su MeteoHub).
- `sito/`: le pagine da caricare a mano sul sito (si caricano una volta: le immagini le prendono da GitHub).

## Segreti del repository (Settings → Secrets and variables → Actions)
- `MH_EMAIL`, `MH_PASSWORD`: account MeteoHub (per ICON-2I).
- `WU_API_KEY`: chiave Weather Underground, per il nudging sulle stazioni (senza, WRF gira libero).

## Avvio a mano
Actions → il workflow della zona e del modello → **Run workflow**. Campi: `ciclo` (vuoto = l'ultimo), `ore` (48), `ore_segmento` (16),
`stazioni`, `mare` (temperatura del mare MUR), `radar` (confronto con il radar della Protezione Civile).

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
| Stazioni meteo | rete di stazioni amatoriali di [Weather Underground](https://www.wunderground.com/) (The Weather Company), tramite la chiave personale del proprietario | restano soggette ai termini d'uso di Weather Underground, da rispettare (la chiave è personale). I dati **non vengono ripubblicati**: si scaricano durante il calcolo, entrano in WRF e non finiscono nei risultati; i file temporanei che passano da un segmento all'altro sono cifrati; i log non riportano codici o posizioni delle stazioni; le mappe mostrano solo l'uscita del modello |
| Solo per `previsioni_castelli.py` / `previsioni_lazio.py` usati da soli | [Open-Meteo](https://open-meteo.com/) ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); l'API gratuita è per uso non commerciale) e quote dal modello digitale del terreno Copernicus DEM («contiene dati Copernicus modificati») | non usati dalla catena WRF |

Librerie principali: ecCodes (Apache 2.0), JasPer, NumPy, SciPy, Matplotlib, netCDF4, requests, pyproj, OpenCV, tifffile: ciascuna con la sua licenza.
I nomi dei paesi e le posizioni sulle mappe sono scritti a mano nello script, non vengono da OpenStreetMap o altri archivi.
Le condizioni dei fornitori possono cambiare: prima di usare il progetto in modo diverso da quello personale
(per esempio per un servizio pubblico o commerciale) vanno ricontrollate.
