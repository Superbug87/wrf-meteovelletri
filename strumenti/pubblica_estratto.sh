#!/usr/bin/env bash
# Pubblica gli estratti per la verifica (estratto_*.npz) delle PROVE DI FISICA nel ramo "prove-fisica", cartella estratti/.
# Piu' prove finiscono nello stesso momento: il ramo e' un solo commit riscritto con "--force-with-lease"; se un'altra prova
# ha pubblicato nel frattempo si riparte dal ramo aggiornato (fino a 12 tentativi, con pause diverse per ciascuna).
# Si tengono 20 giorni di cicli; quelli piu' vecchi spariscono (la verifica guarda sempre gli ultimi giorni).
# Uso: pubblica_estratto.sh CARTELLA_CON_GLI_ESTRATTI
set -euo pipefail
SRC=$(cd "${1:?cartella degli estratti}" && pwd)
RAMO=prove-fisica
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
ls "$SRC"/estratto_*.npz >/dev/null 2>&1 || { echo "nessun estratto in $SRC"; exit 1; }
LIMITE=$(date -u -d '20 days ago' +%Y%m%d)00          # i nomi finiscono con _AAAAMMGGHH.npz

for prova in $(seq 1 12); do
  T=$(mktemp -d); cd "$T"
  git init -q; git remote add origin "$REPO_URL"
  if git fetch -q --depth 1 origin "$RAMO" 2>/dev/null; then
    PRIMA=$(git rev-parse FETCH_HEAD); git checkout -q -b "$RAMO" FETCH_HEAD
  else
    PRIMA=""; git checkout -q --orphan "$RAMO"
  fi
  mkdir -p estratti
  cp "$SRC"/estratto_*.npz estratti/
  for f in estratti/*.npz; do
    c=${f%.npz}; c=${c: -10}
    if [ "$c" -lt "$LIMITE" ]; then rm -f "$f"; fi
  done
  cat > README.md <<'MD'
# Prove di fisica WRF

Estratti per la verifica (valori del modello nei punti delle stazioni) dei run di prova con fisica diversa da quella in uso
(topo_wind 2, PBL MYNN, suolo Noah-MP, MYNN + Noah-MP). Non sono previsioni pubblicate: li legge la verifica giornaliera
(strumenti/verifica.py) e i risultati stanno nella pagella.
MD
  git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
  git add -A
  git diff --cached --quiet && { echo "niente da pubblicare"; exit 0; }
  if git rev-parse -q --verify HEAD >/dev/null; then git checkout -q --orphan nuovo; fi
  git commit -q -m "Estratti delle prove di fisica"
  if [ -n "$PRIMA" ]; then LEASE="--force-with-lease=refs/heads/$RAMO:$PRIMA"; else LEASE="--force-with-lease=refs/heads/$RAMO:"; fi
  if git push -q $LEASE origin "HEAD:refs/heads/$RAMO"; then echo "pubblicato nel ramo $RAMO (tentativo $prova)"; exit 0; fi
  echo "altra prova ha pubblicato nel frattempo: riprovo ($prova)"
  cd /; rm -rf "$T"
  sleep $(( (RANDOM % 8) + 2 ))
done
echo "!! non sono riuscito a pubblicare dopo 12 tentativi"; exit 1
