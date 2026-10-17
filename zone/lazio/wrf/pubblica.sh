#!/usr/bin/env bash
# Pubblica le immagini nel ramo dei risultati di questa zona e di questo modello (RAMO, per esempio risultati-castelli-ecmwf):
# ogni workflow ha il suo ramo, cosi' due workflow che pubblicano insieme non si cancellano a vicenda.
#   ultimo/            i fogli di sintesi dell'ultima previsione
#   archivio/<ciclo>/  i fogli degli ultimi 28 cicli
#   orarie/            una immagine per ora e variabile degli ultimi 14 cicli (una settimana) + indice.json
#                      (ultimo ciclo) + cicli.json (elenco dei cicli presenti): li legge la pagina del sito
# Il ramo viene riscritto con un solo commit a ogni pubblicazione: la storia non si accumula (le immagini pesano).
# Uso: pubblica.sh CARTELLA_IMMAGINI CICLO
set -euo pipefail
IMG=$(cd "${1:?cartella immagini}" && pwd); CICLO=${2:?ciclo}
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
RAMO=${RAMO:-risultati}
T=$(mktemp -d)
cd "$T"
git init -q
git remote add origin "$REPO_URL"
if git fetch -q --depth 1 origin "$RAMO" 2>/dev/null; then git checkout -q -b "$RAMO" FETCH_HEAD; else git checkout -q --orphan "$RAMO"; fi
# (AREA: sottocartella facoltativa nel ramo; qui non serve, ogni zona e modello ha il suo ramo)
AREA_DIR=${AREA:-.}; mkdir -p "$AREA_DIR"; cd "$AREA_DIR"
rm -rf ultimo; mkdir -p ultimo "archivio/$CICLO"
cp "$IMG"/*.png ultimo/
cp "$IMG"/*.png "archivio/$CICLO/"
cp "$IMG"/*.csv "archivio/$CICLO/" 2>/dev/null || true       # statistiche del confronto con il radar, solo nell'archivio
if [ -f "$IMG/orarie/indice.json" ]; then
  mkdir -p orarie
  # i cicli pubblicati prima dell'elenco dei cicli non hanno il proprio indice: quello generale e' il loro (se e' dello stesso ciclo)
  if [ -f orarie/indice.json ]; then
    C=$(python3 -c "import json; print(json.load(open('orarie/indice.json')).get('ciclo', ''))" 2>/dev/null || true)
    if [ -n "$C" ] && [ -d "orarie/$C" ] && [ ! -f "orarie/$C/indice.json" ]; then cp orarie/indice.json "orarie/$C/indice.json"; fi
  fi
  for d in "$IMG"/orarie/*/; do rm -rf "orarie/$(basename "$d")"; cp -r "$d" "orarie/$(basename "$d")"; done
  cp "$IMG/orarie/indice.json" orarie/indice.json
  # cartelle di cicli senza indice (non si possono scegliere dalla pagina): via
  for d in orarie/*/; do [ -f "${d}indice.json" ] || rm -rf "$d"; done
  # si tiene una settimana: 14 cicli (due al giorno). I piu' vecchi spariscono.
  { ls -1d orarie/*/ 2>/dev/null || true; } | sort | head -n -14 | xargs -r rm -rf
  # elenco dei cicli presenti, dal piu' recente: lo legge la pagina del sito per scegliere il run
  python3 - <<'PY'
import json, os
cicli = sorted((d for d in os.listdir("orarie") if os.path.isfile(f"orarie/{d}/indice.json")), reverse=True)
json.dump({"cicli": cicli}, open("orarie/cicli.json", "w"))
PY
fi
{
  PIL=${PILOTA:-icon2i}; echo "# Ultima previsione WRF — ciclo ${PIL^^} ${CICLO:0:8} ${CICLO:8:2}Z"
  echo
  echo "> Esperimento personale, **non un servizio ufficiale**: può sbagliare e non sostituisce bollettini e allerte della Protezione Civile e dell'Aeronautica Militare. Nessuna garanzia."
  case "${PIL}" in gfs) FONTE="NOAA/NCEP GFS";; ecmwf) FONTE="ECMWF IFS open data (CC BY 4.0, dati modificati)";; *) FONTE="ICON-2I, Agenzia ItaliaMeteo in cooperation with Arpae Emilia-Romagna Idro-Meteo-Clima Service (CC BY 4.0, dati modificati)";; esac
  echo ">"
  echo "> Dati di partenza: ${FONTE}. Modello: WRF (NCAR). Temperatura del mare: MUR SST (NASA JPL), quando disponibile. Stazioni: Weather Underground, usate solo dentro il modello e non ripubblicate. Fonti e licenze: [README del progetto](https://github.com/${GITHUB_REPOSITORY:-utente/repo}#fonti-dei-dati-e-licenze)."
  if ls ultimo/radar_*.png >/dev/null 2>&1; then
    echo ">"
    echo "> Le immagini «radar_*» contengono dati Radar-DPC, Dipartimento della Protezione Civile (radar.protezionecivile.it), licenza [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/): le immagini ricavate da quei dati hanno la stessa licenza."
  fi
  if [ -f orarie/indice.json ]; then echo; echo "Una immagine per ogni ora e variabile: cartella [orarie/](orarie/), elenco in [indice.json](orarie/indice.json)."; fi
  echo
  for f in ultimo/*.png; do echo "![$(basename "$f" .png)]($f)"; echo; done
} > README.md
# l'archivio tiene solo gli ultimi 28 cicli (14 giorni con due cicli al giorno)
ls -1d archivio/*/ 2>/dev/null | head -n -28 | xargs -r rm -rf
git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
cd "$T"
git add -A
if git diff --cached --quiet; then echo "niente da pubblicare"; exit 0; fi
if git rev-parse -q --verify HEAD >/dev/null; then git checkout -q --orphan nuovo; fi   # un solo commit: la storia delle immagini non si accumula
git commit -q -m "Previsione WRF $CICLO"
git push -q --force origin "HEAD:refs/heads/$RAMO"
echo "pubblicato nel ramo $RAMO"
