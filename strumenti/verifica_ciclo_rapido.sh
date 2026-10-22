#!/usr/bin/env bash
# Scarica le piogge delle prove radar (ramo prove-radar) e gli estratti ufficiali, lancia valuta_ciclo_rapido.py e ripubblica
# lo stato in prove-radar/valutazione/. Si tengono 14 giorni di piogge. Serve GITHUB_TOKEN e GITHUB_REPOSITORY.
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
W=$(mktemp -d); P=$W/pioggia; U=$W/uff; mkdir -p "$P" "$U" "$W/val"
cd "$W"; git init -q; git remote add origin "$REPO_URL"
for r in risultati-lazio-ecmwf risultati-lazio-icon2i; do
  if git fetch -q --depth 1 origin "$r:refs/remotes/o/$r" 2>/dev/null; then
    for p in $(git ls-tree --name-only "refs/remotes/o/$r" estratti/ | grep '\.npz$'); do
      git show "refs/remotes/o/$r:$p" > "$U/$(basename "$p")"
    done
  fi
done
git fetch -q --depth 1 origin prove-radar:refs/remotes/o/prove-radar || { echo "ramo prove-radar non trovato"; exit 0; }
for p in $(git ls-tree --name-only refs/remotes/o/prove-radar pioggia/ | grep 'pioggia_lazio-ecmwf-.*\.npz$'); do
  git show "refs/remotes/o/prove-radar:$p" > "$P/$(basename "$p")"
done
git show refs/remotes/o/prove-radar:valutazione/ciclo_rapido.json > "$W/val/ciclo_rapido.json" 2>/dev/null || true
ls "$P" | wc -l
python3 "$QUI/valuta_ciclo_rapido.py" --pioggia "$P" --ufficiali "$U" --stato "$W/val/ciclo_rapido.json" --testo "$W/val/ciclo_rapido.txt"
LIMITE=$(date -u -d '14 days ago' +%Y%m%d)00
for prova in $(seq 1 12); do
  T=$(mktemp -d); cd "$T"
  git init -q; git remote add origin "$REPO_URL"
  git fetch -q --depth 1 origin prove-radar; PRIMA=$(git rev-parse FETCH_HEAD); git checkout -q -b prove-radar FETCH_HEAD
  mkdir -p valutazione; cp "$W"/val/ciclo_rapido.* valutazione/
  for f in pioggia/pioggia_*.npz; do [ -e "$f" ] || continue; d=${f##*_}; [ "${d%.npz}" \< "$LIMITE" ] && git rm -q "$f"; done
  git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
  git add -A
  git diff --cached --quiet && { echo "niente da pubblicare"; exit 0; }
  git checkout -q --orphan nuovo
  git commit -q -m "Verifica del ciclo rapido"
  if git push -q --force-with-lease=refs/heads/prove-radar:"$PRIMA" origin HEAD:refs/heads/prove-radar; then echo "pubblicato (tentativo $prova)"; exit 0; fi
  echo "altra prova ha pubblicato: riprovo"; cd /; rm -rf "$T"; sleep $((prova * 3))
done
exit 1
