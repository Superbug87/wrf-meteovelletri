#!/usr/bin/env bash
# Pubblica la pioggia oraria delle prove del radar (pioggia_*.npz) nel ramo "prove-radar", cartella pioggia/.
# Come pubblica_estratto.sh: il ramo e' un solo commit riscritto con --force-with-lease, si riprova se un'altra prova
# ha pubblicato nel frattempo. Questo ramo non lo legge nessun run ufficiale: serve solo alla valutazione delle prove.
# Uso: pubblica_prove_radar.sh CARTELLA_CON_I_FILE
set -euo pipefail
SRC=$(cd "${1:?cartella}" && pwd)
RAMO=prove-radar
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
ls "$SRC"/pioggia_*.npz >/dev/null 2>&1 || { echo "nessun file pioggia_*.npz in $SRC"; exit 1; }
for prova in $(seq 1 12); do
  T=$(mktemp -d); cd "$T"
  git init -q; git remote add origin "$REPO_URL"
  if git fetch -q --depth 1 origin "$RAMO" 2>/dev/null; then
    PRIMA=$(git rev-parse FETCH_HEAD); git checkout -q -b "$RAMO" FETCH_HEAD
  else
    PRIMA=""; git checkout -q --orphan "$RAMO"
  fi
  mkdir -p pioggia
  cp "$SRC"/pioggia_*.npz pioggia/
  printf '# Prove del radar\n\nPioggia oraria del dominio fine delle prove di .github/workflows/radar-prove.yml (rad0 = senza radar, radq = umidita dal radar). Non sono previsioni pubblicate.\n' > README.md
  git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
  git add -A
  git diff --cached --quiet && { echo "niente da pubblicare"; exit 0; }
  if git rev-parse -q --verify HEAD >/dev/null; then git checkout -q --orphan nuovo; fi
  git commit -q -m "Pioggia oraria delle prove del radar"
  if [ -n "$PRIMA" ]; then LEASE="--force-with-lease=refs/heads/$RAMO:$PRIMA"; else LEASE="--force-with-lease=refs/heads/$RAMO:"; fi
  if git push -q $LEASE origin "HEAD:refs/heads/$RAMO"; then echo "pubblicato nel ramo $RAMO (tentativo $prova)"; exit 0; fi
  echo "altra prova ha pubblicato nel frattempo: riprovo ($prova)"
  cd /; rm -rf "$T"
  sleep $(( (RANDOM % 8) + 2 ))
done
echo "!! non sono riuscito a pubblicare dopo 12 tentativi"; exit 1
