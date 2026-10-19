#!/usr/bin/env bash
# Pubblica le mappe orarie di una PROVA (fisica diversa, ICON-EU, GFS...) nel suo ramo, per la pagina "Confronto modelli".
# Un ramo per prova (RAMO, per esempio prove-castelli-icon2i-mynn): le prove non si disturbano tra loro e i rami ufficiali
# restano com'erano. Si tiene SOLO l'ultimo ciclo e solo le variabili principali, per non far crescere il repository.
# Uso: RAMO=... pubblica_prova.sh CARTELLA_IMMAGINI CICLO
set -euo pipefail
IMG=$(cd "${1:?cartella immagini}" && pwd); CICLO=${2:?ciclo}
RAMO=${RAMO:?ramo}
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
[ -f "$IMG/orarie/indice.json" ] || { echo "nessuna mappa oraria da pubblicare"; exit 0; }
VARIABILI="temperatura_2m umidita vento_medio raffiche pioggia_1h pioggia_6h pioggia_24h pressione nubi_totale nubi_basse cape_mu"
T=$(mktemp -d); cd "$T"
mkdir -p orarie
for v in $VARIABILI; do
  [ -d "$IMG/orarie/$CICLO/$v" ] && { mkdir -p "orarie/$CICLO"; cp -r "$IMG/orarie/$CICLO/$v" "orarie/$CICLO/$v"; }
done
VARIABILI="$VARIABILI" CICLO="$CICLO" IMG="$IMG" python3 - <<'PY'
import json, os
tieni = os.environ["VARIABILI"].split(); ciclo = os.environ["CICLO"]; img = os.environ["IMG"]
for src, dst in ((f"{img}/orarie/indice.json", "orarie/indice.json"), (f"{img}/orarie/{ciclo}/indice.json", f"orarie/{ciclo}/indice.json")):
    if not os.path.isfile(src):
        continue
    d = json.load(open(src))
    d["variabili"] = [v for v in d["variabili"] if v["id"] in tieni and os.path.isdir(f"orarie/{ciclo}/{v['id']}")]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    json.dump(d, open(dst, "w"), ensure_ascii=False)
json.dump({"cicli": [ciclo]}, open("orarie/cicli.json", "w"))
PY
echo "Mappe di prova (non e' un servizio ufficiale): ramo $RAMO, ciclo $CICLO. Le legge la pagina Confronto modelli." > README.md
git init -q; git remote add origin "$REPO_URL"
git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
git checkout -q --orphan "$RAMO"
git add -A
git commit -q -m "Mappe di prova $RAMO $CICLO"
for i in 1 2 3 4 5; do
  git push -q --force origin "HEAD:refs/heads/$RAMO" && { echo "pubblicato nel ramo $RAMO"; exit 0; }
  sleep $((i * 5))
done
echo "!! impossibile pubblicare nel ramo $RAMO"; exit 1
