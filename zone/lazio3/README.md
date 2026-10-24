# Zona Lazio (3 km: vecchia configurazione, solo per le prove)

Nessun run normale la usa: dal 20/10/2026 il Lazio ufficiale è `zone/lazio/` a 27 → 9 → 3 km.


Codice WRF della zona: tre domini annidati centrati sul Lazio (41,82 N, 12,72 E), **18 → 6 → 2 km**
(`wrf/dominio.py`), mappe disegnate da `previsioni_lazio.py` (30 tipi), dati per le mappe in `dati/`
(confini ISTAT/Openpolis, contorni dei laghi ESA WorldCover).

Il flusso comune (`.github/workflows/previsione.yml`) copia questa cartella nella radice del repository e lancia
`wrf/esegui.sh`, `wrf/controllo.py`, `wrf/postprocess.py`, `wrf/pioggia_radar.py` e `wrf/pubblica.sh`.
Il modello di partenza (ECMWF o ICON-2I) arriva dalla variabile `PILOTA`. Istruzioni, fonti e licenze: README principale.
