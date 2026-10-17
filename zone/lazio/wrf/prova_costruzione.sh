#!/usr/bin/env bash
# Prova la parte "intelligente" della compilazione a tappe (avanzamento, tempo massimo, guardiano contro i blocchi)
# con comandi inventati: non compila nulla e dura mezzo minuto.
#   bash wrf/prova_costruzione.sh [CARTELLA]
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
T=${1:-$(mktemp -d)}
mkdir -p "$T/src" "$T/lavoro"
export SRC="$T/src" PERIODO_SEC=1 STALLO_SEC=4
# shellcheck source=costruisci.sh
. "$QUI/costruisci.sh"
D="$T/lavoro"
guasto() { echo "PROVA FALLITA: $*"; exit 1; }

echo "-- finisce da solo: esce con 0 e scrive l'avanzamento"
rc=0
con_avanzamento "$D" "$D/ok.log" 60 bash -c 'for i in 1 2 3 4; do touch "o$i.o"; echo "mpif90 -c modulo_$i.f90"; sleep 1; done' > "$T/ok.txt" || rc=$?
[ "$rc" = 0 ] || guasto "uscita $rc invece di 0"
grep -q 'oggetti compilati' "$T/ok.txt" || guasto "manca la riga di avanzamento"
grep -q 'ultimo file: modulo_' "$T/ok.txt" || guasto "non riconosce l'ultimo file compilato"
grep -q 'modulo_4.f90' "$D/ok.log" || guasto "l'output del comando non è nel log"

echo "-- fallisce: l'errore del comando arriva a chi chiama"
rc=0
con_avanzamento "$D" "$D/ko.log" 60 bash -c 'echo "errore finto"; exit 7' > /dev/null || rc=$?
[ "$rc" = 7 ] || guasto "uscita $rc invece di 7"

echo "-- tempo massimo: ferma tutto (anche i figli) ed esce con 3"
rc=0
con_avanzamento "$D" "$D/tempo.log" 3 bash -c 'sleep 9701 & echo "avviato"; wait' > "$T/tempo.txt" || rc=$?
[ "$rc" = 3 ] || guasto "uscita $rc invece di 3"
grep -q 'tempo massimo' "$T/tempo.txt" || guasto "manca il messaggio del tempo massimo"
sleep 1
! pgrep -f 'sleep 9701' > /dev/null || { pkill -f 'sleep 9701' || true; guasto "un processo figlio è rimasto vivo"; }

echo "-- guardiano: nulla si muove per STALLO_SEC, diagnosi, esce con 4"
rc=0
con_avanzamento "$D" "$D/fermo.log" 120 bash -c 'echo "inizio"; sleep 9702' > "$T/fermo.txt" || rc=$?
[ "$rc" = 4 ] || guasto "uscita $rc invece di 4"
grep -q 'NESSUN PROGRESSO' "$T/fermo.txt" || guasto "manca la diagnosi"
grep -q 'NESSUN PROGRESSO' "$SRC/diagnosi.txt" || guasto "la diagnosi non è stata salvata"
sleep 1
! pgrep -f 'sleep 9702' > /dev/null || { pkill -f 'sleep 9702' || true; guasto "il comando fermo è rimasto vivo"; }

echo "-- un comando che continua a lavorare non viene scambiato per fermo (resta vivo oltre STALLO_SEC)"
rc=0
con_avanzamento "$D" "$D/lungo.log" 60 bash -c 'for i in 1 2 3 4 5 6 7 8 9 10; do echo "riga $i"; sleep 1; done' > /dev/null || rc=$?
[ "$rc" = 0 ] || guasto "un comando attivo è stato fermato (uscita $rc)"

echo "-- passo_guardato: riuscito passa, fallito stampa le ultime righe del log ed esce con 1"
( cd "$D" && passo_guardato "prova ok" pg_ok.log 30 bash -c 'echo bene' ) > /dev/null || guasto "passo_guardato fallisce su un comando riuscito"
rc=0
( cd "$D" && passo_guardato "prova ko" pg_ko.log 30 bash -c 'echo "errore finto"; exit 5' ) > "$T/pg_ko.txt" 2>&1 || rc=$?
[ "$rc" = 1 ] || guasto "passo_guardato: uscita $rc invece di 1"
grep -q 'errore finto' "$T/pg_ko.txt" || guasto "passo_guardato non mostra il log del passo fallito"

echo "-- disattiva_lto: aggiunge -fno-lto a DM_FC e DM_CC (righe come quelle vere di configure.wrf), una volta sola"
cat > "$T/configure.wrf" <<'EOF'
SFC             =       gfortran
SCC             =       gcc
DM_FC           =       mpif90
DM_CC           =       mpicc -cc=$(SCC)
FC              =        $(DM_FC)
CC              =       $(DM_CC) -DFSEEKO64_OK
EOF
disattiva_lto "$T/configure.wrf"
cp "$T/configure.wrf" "$T/configure.wrf.1"
disattiva_lto "$T/configure.wrf"
cmp -s "$T/configure.wrf" "$T/configure.wrf.1" || guasto "disattiva_lto non è idempotente"
grep -q '^DM_FC *= *mpif90 -fno-lto' "$T/configure.wrf" || guasto "manca -fno-lto in DM_FC"
grep -q '^DM_CC *= *mpicc -fno-lto -cc=' "$T/configure.wrf" || guasto "manca -fno-lto in DM_CC (e -cc deve restare)"
[ "$(grep -c 'fno-lto' "$T/configure.wrf")" = 2 ] || guasto "-fno-lto compare dove non deve"
printf 'SFC = gfortran\n' > "$T/senza_dm.wrf"
rc=0
( disattiva_lto "$T/senza_dm.wrf" ) > /dev/null 2>&1 || rc=$?
[ "$rc" = 1 ] || guasto "file senza DM_FC/DM_CC: uscita $rc invece di 1"
if command -v mpif90 > /dev/null; then
  echo "-- il wrapper mpif90 di questo sistema: -fno-lto viene dopo -flto e vince"
  mpif90 -show -c -O2 -fno-lto x.f90 | tr ' ' '\n' | grep -n -E '^-f(no-)?lto' | tail -n 1 | grep -q 'fno-lto' || guasto "con questo mpif90 -fno-lto non è l'ultimo flag LTO"
fi

echo "-- clona: nessuna cartella a metà se il clone fallisce"
cd "$T"
( clona finto "file://$T/non-esiste" main ) > /dev/null 2>&1 && guasto "il clone impossibile non ha dato errore"
[ ! -d "$T/finto" ] || guasto "è rimasta una cartella a metà"

echo "compilazione a tappe: prove ok"
