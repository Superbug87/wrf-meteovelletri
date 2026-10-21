#!/usr/bin/env bash
# Mappe di concordanza della pioggia tra le versioni WRF (strumenti/probabilita.py): prende gli estratti dai rami dei run ufficiali
# (risultati-<zona>-<modello>) e dalle prove (prove-fisica), per ogni zona sceglie l'ultimo ciclo con almeno 4 versioni e pubblica le
# immagini nel ramo "concordanza" (un solo commit, cartelle castelli/ e lazio/).
# Uso: concordanza.sh        (serve GITHUB_TOKEN e GITHUB_REPOSITORY)
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
W=$(mktemp -d); EST=$W/estratti; OUT=$W/out; mkdir -p "$EST" "$OUT"
cd "$W"; git init -q; git remote add origin "$REPO_URL"
for r in risultati-castelli-ecmwf risultati-castelli-icon2i risultati-lazio-ecmwf risultati-lazio-icon2i prove-fisica; do
  if git fetch -q --depth 1 origin "$r:refs/remotes/o/$r" 2>/dev/null; then
    for p in $(git ls-tree --name-only "refs/remotes/o/$r" estratti/ | grep '\.npz$'); do
      git show "refs/remotes/o/$r:$p" > "$EST/$(basename "$p")"
    done
  else
    echo "ramo $r non trovato"
  fi
done
ls "$EST" | wc -l
for z in castelli lazio; do
  python3 "$QUI/probabilita.py" --zona "$z" --ciclo auto --estratti "$EST" --out "$OUT/$z" || echo "nessuna mappa per $z"
done
ls "$OUT"/*/*.png >/dev/null 2>&1 || { echo "nessuna immagine prodotta"; exit 1; }
cat > "$OUT/README.md" <<'MD'
# Concordanza della pioggia tra le versioni WRF

Per ogni zona, l'ultimo ciclo con almeno 4 versioni (ECMWF e ICON-2I ufficiali, loro varianti di fisica, ICON-EU e GFS in prova).
Ogni riquadro e' una finestra di 3 ore; il colore e' la quota di versioni che prevedono oltre 1 (o 5) mm/h entro circa 1 cella
e +-1 ora. Non e' una probabilita' calibrata: le versioni partono dagli stessi dati. ECMWF e ICON-2I restano distinti nelle mappe
per famiglia. Codice: strumenti/probabilita.py.
MD
cd "$OUT"; git init -q -b concordanza
git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
git add -A; git commit -q -m "Concordanza della pioggia"
git remote add origin "$REPO_URL"
git push -q --force origin HEAD:refs/heads/concordanza
echo "pubblicato nel ramo concordanza"
