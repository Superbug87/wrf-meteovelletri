#!/usr/bin/env bash
# Verifica giornaliera: prende il ramo "verifica", aggiunge il giorno (strumenti/verifica.py), rifa' la pagella e ripubblica
# il ramo con un solo commit (sono file JSON piccoli: la storia non serve, i giorni restano tutti nella cartella giorni/).
# Uso: verifica.sh [AAAA-MM-GG]     (default: ieri, in UTC)
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
GIORNO=${1:-$(date -u -d yesterday +%Y-%m-%d)}
REPO_URL=${REPO_URL:-"https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"}
T=$(mktemp -d)
cd "$T"
git init -q
git remote add origin "$REPO_URL"
if git fetch -q --depth 1 origin verifica 2>/dev/null; then git checkout -q -b verifica FETCH_HEAD; else git checkout -q --orphan verifica; fi
python3 "$QUI/verifica.py" --giorno "$GIORNO" --archivio "$T"
cat > README.md <<EOT
# Verifica delle previsioni WRF

Confronto giornaliero delle previsioni con le osservazioni: stazioni del Centro Funzionale della Regione Lazio,
stazioni SYNOP dell'Aeronautica Militare (via OGIMET) e radar della Protezione Civile (Radar-DPC, CC BY-SA 4.0).
Riferimento: previsioni a breve di ECMWF e ICON-2I originali (Open-Meteo, CC BY 4.0).

- \`giorni/AAAA-MM-GG.json\`: le somme di ogni giorno (errori, tabelle della pioggia, confronto col radar), per zona, modello e scadenza.
- \`pagella.json\`: gli indici degli ultimi 7 e 30 giorni; la legge la pagina \`pagella-wrf.html\` del sito.

Ultimo giorno verificato: ${GIORNO}.
EOT
git config user.name "github-actions"; git config user.email "actions@users.noreply.github.com"
git add -A
if git rev-parse -q --verify HEAD >/dev/null; then git checkout -q --orphan nuovo; fi
git commit -q -m "Verifica del $GIORNO"
git push -q --force origin HEAD:refs/heads/verifica
echo "pubblicato nel ramo verifica"
