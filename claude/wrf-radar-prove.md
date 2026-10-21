# WRF e radar: prove per la previsione a 1-6 ore (punto e ora dei temporali)

Diario di lavoro dell'agente di ricerca. Iniziato il 21/10/2026. Report finale entro il 28/10/2026.

## Obiettivo
Avvicinarsi il piu' possibile al punto e all'ora esatti di temporali e bande di pioggia nelle prossime 1-6 ore,
usando il radar (Radar-DPC SRI, 1 km, 5 min). Confronti sempre con: radar fermo, radar spostato (metodo in uso in
pioggia_radar.py), WRF senza radar dello stesso ciclo. Metriche: FSS 10 e 20 km a 0,5 e 2 mm/h, bias, errore di tempo,
ora di arrivo nei punti.

## Ripresa dal lavoro precedente
- Nessun documento claude/wrf-radar-prove.md e nessun run di prova radar trovati; nel sandbox c'era una scansione del
  radar dal 25/09 al 21/10 (pioggia nel Lazio il 25/09 notte, 5-12/10 quasi continua, 12/10, 20-21/10) e lo scarico in
  corso dei mosaici di quei giorni (ritaglio 500 km): ripreso e allargato a 900 km (a +4..+6 h la pioggia arriva da
  piu' di 250 km: con il ritaglio da 500 km il radar spostato "perdeva" la pioggia in arrivo).
- Nota tecnica: lo strumento Projects (project_read/project_write) non era disponibile a questo agente; il diario e'
  tenuto in file e consegnato all'orchestratore.

## Eventi disponibili
(vedi tabelle sotto)

## Strumenti creati
- `zone/lazio/wrf/radar_nudging.py` + opzione FISICA `radq` (config.py, esegui.sh): sonde finte sature dove il radar
  vede pioggia, nudging solo dell'umidita' nella prima ora (domini 9 e 3 km). Metodo tipo Davolio e Buzzi (2004).
- Opzione `rad0`: nessun cambiamento, nome del run di confronto.
- `.github/workflows/radar-prove.yml`: run brevi (9 h) senza stazioni e senza mappe, anche su cicli passati
  (ECMWF 00/06/12/18 dall'archivio AWS); pubblica la pioggia oraria del 3 km nel ramo `prove-radar`.
- `strumenti/pioggia_oraria_d03.py`, `strumenti/pubblica_prove_radar.sh`.
- Banco di prova nel sandbox: `bench.py` (nowcast radar a +1..+6 h, pysteps), `eval_wrf.py` (WRF sulla griglia radar).

## Risultati 1 - nowcast solo radar, evento 20-21/10 (ritaglio 900 km, 23 emissioni orarie)
Riquadro di verifica 160 x 160 km al centro del Lazio (Roma-Castelli), griglia 2 km, piogge orarie.
"unif" = metodo in uso (movimento uniforme Lucas-Kanade); "lk" = campo di moto non uniforme (pysteps, Lucas-Kanade
denso); "anvil" = pysteps ANVIL (crescita/decadimento per scale); "sprog"/"steps" = pysteps S-PROG e STEPS (12 membri).

FSS 20 km, soglia 0,5 mm/h:

| metodo | +1h | +2h | +3h | +4h | +5h | +6h |
|---|---|---|---|---|---|---|
| radar fermo | 0.84 | 0.65 | 0.54 | 0.43 | 0.36 | 0.32 |
| unif (in uso) | 0.95 | 0.82 | 0.68 | 0.55 | 0.46 | 0.38 |
| lk (moto non uniforme) | 0.95 | 0.83 | 0.68 | 0.56 | 0.50 | 0.46 |
| anvil | 0.95 | 0.85 | 0.71 | 0.56 | 0.49 | 0.44 |
| steps (ensemble) | 0.95 | 0.83 | 0.67 | 0.51 | 0.32 | 0.20 |

FSS 10 km, soglia 2 mm/h: unif 0.89/0.71/0.55/0.43/0.35/0.25; anvil 0.90/0.76/0.59/0.43/0.35/0.31.
Bias: tutti i metodi che spostano il radar perdono pioggia con la scadenza (0.9 a +1 h, 0.5 a +3 h, 0.3 a +6 h):
la pioggia nuova che nasce non c'e', quella che muore resta; il radar fermo ha bias 1 ma la posizione sbagliata.
Ora di arrivo nei punti asciutti all'emissione: con unif/lk/anvil circa 1/3 dei punti esatta all'ora, 55% entro +-1 h,
30% mancati (la pioggia arriva ma il nowcast non la porta); radar fermo 91% mancati.

## Risultati 2 - WRF con il radar (prima prova: ciclo ECMWF 21/10 00Z, emissione 01Z, UN SOLO caso)
WRF Lazio 27-9-3 km, 9 ore, senza stazioni. Emissione = fine della prima ora (finestra del radar per radq).

FSS 20 km, 0,5 mm/h (ore che finiscono a 02..07Z):

| | +1h | +2h | +3h | +4h | +5h | +6h |
|---|---|---|---|---|---|---|
| radar lk | 0.93 | 0.89 | 0.69 | 0.44 | 0.51 | 0.34 |
| radar anvil | 0.93 | 0.88 | 0.72 | 0.38 | 0.34 | 0.22 |
| WRF rad0 (senza radar) | 0.38 | 0.69 | 0.80 | 0.61 | 0.32 | 0.49 |
| WRF radq | 0.62 | 0.89 | 0.77 | 0.65 | 0.35 | 0.40 |
| WRF radq-radforte | 0.68 | 0.84 | 0.78 | 0.66 | 0.46 | 0.37 |
| WRF radq-radnubi | 0.78 | 0.90 | 0.81 | 0.65 | 0.43 | 0.40 |

Bias WRF +1 h: rad0 0.18 (il modello non ha ancora fatto piovere: spin-up), radq 0.62, radq-radnubi 1.02.
Prima impressione: il radar dentro WRF toglie gran parte dello spin-up della prima-seconda ora; dopo +3 h l'effetto
si perde e WRF esagera la pioggia (bias 3-4 a +4..+6 h in tutti i run, anche senza radar). Da confermare su molti cicli.
Costo: un run di 9 ore = circa 17 minuti di runner ARM (15 di WRF); le sonde dal radar aggiungono 30 secondi.
