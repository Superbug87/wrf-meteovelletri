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

## Risultati 3 - nowcast solo radar su 4 eventi (96 emissioni: 25/09 notte, 5-9/10, 12/10, 20-21/10)
FSS 20 km, 0,5 mm/h (tutte le emissioni insieme):

| metodo | +1h | +2h | +3h | +4h | +5h | +6h |
|---|---|---|---|---|---|---|
| radar fermo | 0.83 | 0.57 | 0.42 | 0.35 | 0.29 | 0.27 |
| unif (in uso) | 0.92 | 0.75 | 0.60 | 0.46 | 0.36 | 0.31 |
| lk | 0.93 | 0.75 | 0.60 | 0.45 | 0.38 | 0.33 |
| anvil | 0.94 | 0.79 | 0.63 | 0.46 | 0.38 | 0.33 |
| steps | 0.93 | 0.77 | 0.61 | 0.46 | 0.31 | 0.20 |

FSS 10 km, 2 mm/h: unif 0.85/0.65/0.48/0.33/0.23/0.19; anvil 0.87/0.68/0.49/0.32/0.24/0.20.
Anvil - unif (bootstrap a blocchi di 6 h, intervallo 90%): +0.014 [+0.01,+0.03] a +1 h, +0.033 [+0.01,+0.07] a +2 h,
+0.028 [-0.01,+0.09] a +3 h, poi non distinguibile da zero. lk - unif: nessuna differenza significativa.
Ora di arrivo nei punti (163 mila casi punto-emissione): mancati 50% unif, 36% lk e anvil, 90% radar fermo;
entro +-1 h: 39% unif, 49% anvil. Il guadagno di anvil/lk sull'ora di arrivo viene dalla velocita' minima del metodo
in uso (sotto 24 km/h la pioggia resta ferma): unif0 (stesso metodo senza soglia) manca il 40%.
Conclusione parziale: anvil migliora poco ma in modo misurabile la prima-seconda ora; nessun metodo radar tiene oltre
+3 h (FSS < 0,5 e bias 0,6-0,7: la pioggia che nasce dopo l'emissione non puo' esserci).

## Risultati 4 - WRF con e senza radar su 17 cicli ECMWF (5-12/10 e 20-21/10), emissione = avvio + 1 h
Run: rad0 (senza radar) e radq (umidita' dal radar nella prima ora), stesso ciclo, dati ECMWF dell'analisi (caso
favorevole: in tempo reale l'analisi arriva 7,5 h dopo, vedi Risultati 6). Confronto con i nowcast radar emessi alla
stessa ora. 17 emissioni, 3 eventi.

FSS 20 km, 0,5 mm/h:

| | +1h | +2h | +3h | +4h | +5h | +6h |
|---|---|---|---|---|---|---|
| radar unif (in uso) | 0.90 | 0.80 | 0.74 | 0.66 | 0.48 | 0.31 |
| radar anvil | 0.92 | 0.82 | 0.71 | 0.59 | 0.47 | 0.32 |
| WRF rad0 | 0.28 | 0.47 | 0.58 | 0.48 | 0.42 | 0.35 |
| WRF radq | 0.56 | 0.65 | 0.61 | 0.56 | 0.48 | 0.38 |
| fusione anvil + WRF rad0, pesi fissi 0.9/0.7/0.5/0.3/0.2/0.1 | 0.92 | 0.82 | 0.74 | 0.62 | 0.53 | 0.37 |
| fusione anvil + WRF rad0, pesi tarati (valid. incrociata) | 0.92 | 0.81 | 0.72 | 0.61 | 0.53 | 0.40 |
| fusione anvil + WRF radq, pesi fissi | 0.92 | 0.82 | 0.71 | 0.61 | 0.51 | 0.37 |

FSS 10 km, 2 mm/h: unif 0.82/0.79/0.68/0.53/0.26/0.13; rad0 0.21/0.42/0.49/0.32/0.27/0.21; radq 0.32/0.48/0.48/0.36/0.32/0.23.

radq - rad0 (bootstrap, 90%): +0.28 [+0.21,+0.36] a +1 h, +0.18 [+0.11,+0.27] a +2 h, poi +0.03..+0.08 non sempre
significativo. Per ciclo: radq meglio di rad0 nelle prime 3 ore in 15 cicli su 17, uguale negli altri.
Bias: rad0 0.26 a +1 h e 0.51 a +2 h (lo spin-up: WRF partito da ECMWF non ha nubi ne' pioggia e ci mette 2-3 ore a
farle); radq 0.51 e 0.81.

Cosa vuol dire:
- Il radar dentro WRF funziona come previsto: quasi dimezza il ritardo iniziale del modello. Ma anche con il radar
  WRF resta molto sotto il nowcast radar fino a +4 h (0,56 contro 0,90 a +1 h). Il nowcast vince fino a +4 h, a +5 h
  sono pari, a +6 h WRF e' un po' meglio.
- Nella fusione radar + WRF il vantaggio di radq sparisce: nelle prime ore pesa il radar, dopo +3 h radq e rad0 si
  equivalgono (radq meno di rad0 a +5 h: -0,05 [-0,09, 0,00]).
- La fusione aggiunge +0,03..+0,05 di FSS a +3..+6 h rispetto al solo radar (non significativo con 17 casi, ma
  sempre dello stesso segno). Pesi tarati sul radar: circa 1/0,9/0,6-0,9/0,6-0,9/0,4-0,6/0,4 per +1..+6 h:
  il WRF entra davvero solo da +4-5 h. I pesi in uso in pioggia_radar.py (0,83/0,5/0,17 per 3 ore) danno troppo
  peso a WRF gia' a +2-3 h.
- Correzione di posizione di WRF (spostare WRF in modo che la sua ultima ora coincida col radar): NON funziona
  (peggiora, -0,1): all'emissione WRF ha troppo poca pioggia per stimare lo spostamento.
- Ora di arrivo nei punti: la fusione riduce i mancati (dal 44% di unif e 29% di anvil al 17-20%) ma aumenta i falsi
  allarmi (punti rimasti asciutti dove si prevedeva pioggia: 11% unif, 35% anvil, 30-40% fusioni).
