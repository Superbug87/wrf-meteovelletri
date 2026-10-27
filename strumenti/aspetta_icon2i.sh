#!/usr/bin/env bash
# Un solo download ICON-2I per ciclo: il run ufficiale dei Castelli scarica da MeteoHub e mette i dati in cache
# (chiave icon2i-dati-v2-CICLO-ORE); gli altri run ICON-2I (Lazio, prove) se lo trovano ancora al lavoro aspettano la cache
# invece di scaricare anche loro. Il 27/10/2026 il 12Z e' arrivato in ritardo, le due zone lo aspettavano insieme e hanno
# scaricato in parallelo, riempiendo la quota dell'account. Se il Castelli non c'e', e' finito o fallito, si prosegue
# subito e lo scarico lo fa questo run. Uso: aspetta_icon2i.sh CICLO ORE (serve GH_TOKEN con actions: read).
set -uo pipefail
CICLO=$1; ORE=$2
CHIAVE="icon2i-dati-v2-${CICLO}-${ORE}"
REPO=${GITHUB_REPOSITORY:-Superbug87/wrf-meteovelletri}
MAX_MINUTI=${MAX_MINUTI:-100}
for ((m = 0; m <= MAX_MINUTI; m++)); do
  n=$(gh api "repos/$REPO/actions/caches?key=$CHIAVE" --jq '[.actions_caches[] | select(.key == "'"$CHIAVE"'")] | length' 2>/dev/null || echo 0)
  [[ "$n" =~ ^[0-9]+$ ]] || n=0                      # risposta strana o API non raggiungibile: come se non ci fosse
  if [ "$n" -gt 0 ]; then echo "dati ICON-2I del $CICLO gia' in cache"; exit 0; fi
  attivi=$(gh run list -R "$REPO" --workflow castelli-icon2i.yml -L 10 --json displayTitle,status \
           --jq '[.[] | select((.displayTitle | contains("'"$CICLO"'")) and (.status != "completed"))] | length' 2>/dev/null || echo 0)
  [[ "$attivi" =~ ^[0-9]+$ ]] || attivi=0
  if [ "$attivi" -eq 0 ]; then
    echo "nessun run dei Castelli al lavoro sul $CICLO: scarico io"; exit 0
  fi
  [ $((m % 10)) -eq 0 ] && echo "il Castelli sta scaricando il $CICLO: aspetto la cache ($m min)"
  sleep 60
done
echo "dopo $MAX_MINUTI minuti la cache non c'e' ancora: scarico io"
