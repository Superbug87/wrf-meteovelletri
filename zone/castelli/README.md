# Zona Castelli Romani

Codice WRF della zona: tre domini annidati centrati sui Castelli Romani (41,71 N, 12,73 E), **9 → 3 → 1 km**
(`wrf/dominio.py`), mappe disegnate da `previsioni_castelli.py` (30 tipi), dati per le mappe in `dati/`
(confini ISTAT/Openpolis, contorni dei laghi ESA WorldCover).

Il flusso comune (`.github/workflows/previsione.yml`) copia questa cartella nella radice del repository e lancia
`wrf/esegui.sh`, `wrf/controllo.py`, `wrf/postprocess.py`, `wrf/pioggia_radar.py` e `wrf/pubblica.sh`.
Il modello di partenza (ECMWF o ICON-2I) arriva dalla variabile `PILOTA`. Istruzioni, fonti e licenze: README principale.
