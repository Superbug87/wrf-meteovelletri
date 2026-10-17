#!/usr/bin/env bash
# Compila WRF e WPS a tappe e lascia in $INSTALL solo ciò che serve per girare (cartella messa in cache).
#
#   bash wrf/costruisci.sh [prepara|wrf|wps|installa|tutto]      (senza argomenti: tutto, in ordine)
#
# La prima compilazione è lunga e GitHub ferma i job a 6 ore, quindi:
#  - ogni tappa si può rifare e riparte da dove era arrivata (make ricompila solo ciò che manca);
#  - il flusso di lavoro salva in cache anche il sorgente parziale ($SRC e JasPer), pure se la
#    compilazione non finisce: la volta dopo riprende da lì;
#  - mentre compila, il log mostra ogni PERIODO_SEC secondi a che punto è (oggetti compilati, ultimo file,
#    carico, memoria);
#  - la compilazione di WRF ha un tempo massimo (BUDGET_SEC) e un guardiano: se per STALLO_SEC non cambia
#    nulla (né file compilati né righe di log) stampa chi è rimasto appeso, salva la diagnosi e si ferma.
# Codici di uscita: 0 fatto; 1 errore; 3 tempo massimo finito (rilanciare: riprende); 4 compilazione ferma.
set -euo pipefail
WRF_TAG=${WRF_TAG:-v4.7.0}
WPS_TAG=${WPS_TAG:-v4.7.0}
INSTALL=${INSTALL:-$HOME/wrf-install}
SRC=${SRC:-$HOME/wrf-src}
NC=$HOME/netcdf-combinato
JAS=$HOME/jasper
NPROC=$(nproc)
BUDGET_SEC=${BUDGET_SEC:-17400}   # 290 minuti: lasciano un'ora al job (340 minuti) per salvare la cache
PERIODO_SEC=${PERIODO_SEC:-300}   # ogni quanto scrive l'avanzamento
STALLO_SEC=${STALLO_SEC:-1800}    # quanto tempo senza alcun progresso prima di fermarsi

durata() { printf '%02d:%02d:%02d' $(($1 / 3600)) $(($1 % 3600 / 60)) $(($1 % 60)); }

# Ferma un comando partito da con_avanzamento, con tutti i suoi figli (make, compilatori)
ferma() {
  local pid=$1
  kill -TERM -- "-$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -KILL -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

# diagnosi LOG PID: chi è rimasto appeso. Va nel log e in $SRC/diagnosi.txt (il flusso di lavoro la salva tra gli allegati)
diagnosi() {
  mkdir -p "$SRC"
  {
    echo "== NESSUN PROGRESSO da $(durata "$STALLO_SEC") ($(date -u +%H:%M:%S) UTC). Diagnosi"
    echo "-- ultime righe del log"
    tail -n 25 "$1" || true
    echo "-- processi del comando fermo (pid, secondi, %cpu, KB, stato, attesa, comando)"
    ps --sid "$2" -o pid,etimes,pcpu,rss,stat,wchan:20,args 2>/dev/null | cut -c1-200 || true
    echo "-- processi più attivi in tutto il sistema"
    ps -eo pid,etimes,pcpu,rss,args --sort=-pcpu | head -n 8 | cut -c1-200 || true
    echo "-- carico e memoria"
    cat /proc/loadavg
    free -m || true
    echo "-- disco"
    df -h "$HOME" | tail -n 1 || true
  } 2>&1 | tee -a "$SRC/diagnosi.txt"
}

# con_avanzamento CARTELLA LOG SECONDI_MASSIMI comando [argomenti...]
# Lancia il comando in CARTELLA con l'output in LOG e scrive l'avanzamento ogni PERIODO_SEC secondi.
# Esce col codice del comando; 3 se finisce il tempo massimo; 4 se per STALLO_SEC non cambia nulla.
con_avanzamento() {
  local dir=$1 log=$2 massimo=$3 t0=$SECONDS
  local ultimo_tick=$SECONDS ultimo_moto=$SECONDS prec="" pid rc=0 n righe ultimo carico libera
  shift 3
  : >> "$log"
  # setsid: il comando e i suoi figli stanno in un gruppo a parte, che si può fermare senza fermare noi
  (cd "$dir" && exec setsid "$@") >> "$log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    if [ $((SECONDS - ultimo_tick)) -ge "$PERIODO_SEC" ]; then
      ultimo_tick=$SECONDS
      n=$(find "$dir" -name '*.o' 2>/dev/null | wc -l || true)
      righe=$(wc -l < "$log")
      ultimo=$(tail -n 40 "$log" | grep -oE '[A-Za-z0-9_./+-]+\.(f90|F90|F|c)' | tail -n 1 || true)
      carico=$(cut -d' ' -f1 /proc/loadavg)
      libera=$(awk '/MemAvailable/ {printf "%d", $2 / 1024}' /proc/meminfo)
      echo "[$(durata $((SECONDS - t0)))] $n oggetti compilati, $righe righe di log, ultimo file: ${ultimo:-?}, carico $carico, memoria libera $libera MB"
      if [ "$n $righe" != "$prec" ]; then prec="$n $righe"; ultimo_moto=$SECONDS; fi
      if [ $((SECONDS - ultimo_moto)) -ge "$STALLO_SEC" ]; then
        diagnosi "$log" "$pid"
        ferma "$pid"
        scarta_oggetti_recenti "$dir"
        return 4
      fi
    fi
    if [ $((SECONDS - t0)) -ge "$massimo" ]; then
      echo "[$(durata $((SECONDS - t0)))] tempo massimo ($(durata "$massimo")) finito: fermo la compilazione, il lavoro fatto resta in cache e si riprende rilanciando"
      ferma "$pid"
      scarta_oggetti_recenti "$dir"
      return 3
    fi
  done
  wait "$pid" || rc=$?
  return "$rc"
}

# passo_guardato NOME LOG SECONDI_MASSIMI comando [argomenti...]
# Per i passi brevi (JasPer, configure) nella cartella corrente: avanzamento ogni 30 secondi, fermo dopo 5 minuti senza
# progressi; se il passo non riesce stampa le ultime righe del log ed esce con 1.
passo_guardato() {
  local nome=$1 log=$PWD/$2 massimo=$3 rc=0
  shift 3
  PERIODO_SEC=30 STALLO_SEC=300 con_avanzamento "$PWD" "$log" "$massimo" "$@" || rc=$?
  if [ "$rc" != 0 ]; then
    echo "$nome: non riuscito (uscita $rc), ultime righe del log:"
    tail -n 40 "$log" || true
    exit 1
  fi
}

# Un compilatore fermato di colpo può lasciare un .o a metà, che make crederebbe buono: butto gli ultimi due minuti
scarta_oggetti_recenti() {
  find "$1" -name '*.o' -mmin -2 -delete 2>/dev/null || true
}

# Clona in una cartella provvisoria e la rinomina solo a clone finito: un'interruzione non lascia mezzi cloni in cache
clona() {   # clona NOME URL RAMO_O_TAG
  [ -d "$1" ] && return 0
  rm -rf "$1.parziale"
  timeout 600 git clone -q --depth 1 --branch "$3" "$2" "$1.parziale" || { rm -rf "$1.parziale"; return 1; }
  mv "$1.parziale" "$1"
}

ambiente() {
  mkdir -p "$NC/include" "$NC/lib"
  # Su Debian/Ubuntu netCDF-C e netCDF-Fortran stanno in posti diversi: le riuniamo in una cartella sola
  ln -sf /usr/include/netcdf* "$NC/include/"
  ln -sf /usr/lib/x86_64-linux-gnu/libnetcdf* "$NC/lib/"
  export NETCDF="$NC"
  export NETCDF_classic=1
  export WRFIO_NCD_LARGE_FILE_SUPPORT=1
  local lib
  lib=$(find "$JAS" -name 'libjasper.a' 2>/dev/null | head -n1)
  [ -n "$lib" ] || { echo "JasPer non trovato in $JAS: rifare la tappa 'prepara'"; exit 1; }
  JASPERLIB=$(dirname "$lib")
  export JASPERLIB JASPERINC="$JAS/include"
  echo "JasPer: $JASPERLIB"
}

fase_prepara() {
  mkdir -p "$SRC" "$INSTALL"
  cd "$SRC"
  # JasPer (decodifica GRIB2) non è più un pacchetto Ubuntu: lo compiliamo in versione statica,
  # così ungrib.exe non ne ha bisogno quando gira negli altri job
  if [ ! -f "$JAS/.fatto" ]; then
    echo "== JasPer 2.0.33"
    clona jasper https://github.com/jasper-software/jasper.git version-2.0.33
    passo_guardato "JasPer: cmake" jasper-cmake.log 600 \
      cmake -S jasper -B jasper-build -DCMAKE_INSTALL_PREFIX="$JAS" -DJAS_ENABLE_SHARED=OFF -DJAS_ENABLE_PROGRAMS=OFF \
      -DJAS_ENABLE_DOC=OFF -DJAS_ENABLE_LIBJPEG=OFF -DJAS_ENABLE_OPENGL=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON
    passo_guardato "JasPer: compilazione" jasper-build.log 900 cmake --build jasper-build -j "$NPROC"
    passo_guardato "JasPer: installazione" jasper-install.log 300 cmake --install jasper-build
    touch "$JAS/.fatto"
  fi
  ambiente

  clona WRF https://github.com/wrf-model/WRF.git "$WRF_TAG"
  clona WPS https://github.com/wrf-model/WPS.git "$WPS_TAG"

  # Il configure si fa una volta sola: rifarlo a compilazione iniziata non serve e rischia di rimescolare le carte
  if [ ! -f WRF/.configurato ]; then
    echo "== WRF: configure"
    cd "$SRC/WRF"
    local opt
    # senza risposte il configure riscrive il menu all'infinito: si legge solo l'inizio e si chiude
    opt=$(timeout 60 ./configure < /dev/null 2>/dev/null | grep -E 'GNU \(gfortran/gcc\)' | grep -oP '\d+(?=\.\s+\(dmpar\))' | head -n1 || true)
    opt=${opt:-34}
    echo "opzione scelta: $opt (gfortran, dmpar), nesting basic"
    passo_guardato "WRF: configure" configure.log 900 bash -c 'printf "%s\n1\n" "$1" | ./configure' _ "$opt"
    touch .configurato
  fi
  echo "preparazione finita"
}

# Il mpif90 e il mpicc di Ubuntu aggiungono da soli -flto=auto: collegare WRF così richiede 18 minuti e 7 GB per
# eseguibile e finisce con "undefined reference" (visto il 16 ottobre 2026). Si spegne con -fno-lto, che nella riga di
# comando viene dopo i flag del wrapper e vince. Va bene anche su oggetti già compilati con la LTO: sono "fat" e
# contengono il codice normale. Si può rifare più volte senza danni.
disattiva_lto() {   # disattiva_lto configure.wrf
  grep -q 'fno-lto' "$1" || sed -i -E 's/^(DM_FC[[:space:]]*=[[:space:]]*mpif90)/\1 -fno-lto/; s/^(DM_CC[[:space:]]*=[[:space:]]*mpicc)/\1 -fno-lto/' "$1"
  if [ "$(grep -c -E '^DM_(FC|CC)[[:space:]]*=.*-fno-lto' "$1")" != 2 ]; then
    echo "$1: non riesco ad aggiungere -fno-lto a DM_FC e DM_CC"
    grep -n -E '^(DM_FC|DM_CC)' "$1" || true
    exit 1
  fi
}

# Se il collegamento di wrf.exe o real.exe non riesce: cosa c'è davvero nell'archivio e quali flag LTO ha il wrapper
diagnostica_collegamento() {
  {
    echo "== Diagnostica del collegamento"
    echo "-- flag LTO del wrapper mpif90 e di configure.wrf"
    mpif90 -show 2>&1 | tr ' ' '\n' | grep -i lto || true
    grep -E '^(DM_FC|DM_CC)' configure.wrf || true
    echo "-- membri di main/libwrflib.a: totale, e quelli che mancano al collegamento"
    ar t main/libwrflib.a 2>&1 | wc -l
    ar t main/libwrflib.a 2>&1 | grep -E 'module_optional_input|module_comm_nesting_dm|solve_interface|mediation_integrate' || echo "(nessuno dei quattro)"
    echo "-- simbolo cercato dal collegamento, nell'archivio"
    nm -A main/libwrflib.a 2>&1 | grep 'flag_st000010' | head -n 3 || echo "(non trovato)"
    echo "-- primi errori di collegamento"
    grep -n -m 5 'undefined reference' compile.log || true
  } 2>&1 | tee -a "$SRC/diagnosi.txt"
}

fase_wrf() {
  ambiente
  cd "$SRC/WRF"
  if [ -s main/wrf.exe ] && [ -s main/real.exe ]; then echo "WRF già compilato"; return 0; fi
  disattiva_lto configure.wrf
  grep -E '^(DM_FC|DM_CC)' configure.wrf
  local comando=(./compile -j "$NPROC" em_real) t0=$SECONDS rc tentativo
  echo "== WRF: compile em_real con $NPROC processi (la prima volta 40-90 minuti; tempo massimo $(durata "$BUDGET_SEC"), avanzamento ogni $(durata "$PERIODO_SEC"))"
  # Due tentativi: con più processi in parallelo può capitare una gara tra file; la seconda passata di solito la supera
  for tentativo in 1 2; do
    rc=0
    con_avanzamento "$SRC/WRF" "$SRC/WRF/compile.log" $((BUDGET_SEC - (SECONDS - t0))) "${comando[@]}" || rc=$?
    case $rc in
      3) echo "WRF non è finito nel tempo massimo: lavoro salvato, rilanciare per riprendere"; exit 3 ;;
      4) echo "WRF fermo: vedi la diagnosi qui sopra"; exit 4 ;;
    esac
    if [ -s main/wrf.exe ] && [ -s main/real.exe ]; then break; fi
    echo "tentativo $tentativo: mancano wrf.exe o real.exe (uscita $rc), ultime righe:"
    tail -n 40 compile.log
  done
  if [ ! -s main/wrf.exe ] || [ ! -s main/real.exe ]; then
    echo "Compilazione di WRF fallita, ultime righe:"; tail -n 60 compile.log
    diagnostica_collegamento
    exit 1
  fi
  echo "WRF compilato in $(durata $((SECONDS - t0)))"
}

fase_wps() {
  ambiente
  cd "$SRC/WPS"
  export WRF_DIR="$SRC/WRF"
  local e rc=0
  if [ ! -s geogrid.exe ] || [ ! -s ungrib.exe ] || [ ! -s metgrid.exe ]; then
    echo "== WPS: configure + compile"
    if [ ! -f .configurato ]; then
      local opt
      opt=$(timeout 60 ./configure < /dev/null 2>/dev/null | grep -E 'gfortran +\(serial\)' | grep -v NO_GRIB2 | grep -oP '^\s*\K\d+' | head -n1 || true)
      opt=${opt:-1}
      echo "opzione scelta: $opt (gfortran, serial, GRIB2)"
      passo_guardato "WPS: configure" configure.log 900 bash -c 'echo "$1" | ./configure' _ "$opt"
      touch .configurato
    fi
    con_avanzamento "$SRC/WPS" "$SRC/WPS/compile.log" 3600 ./compile || rc=$?
    case $rc in
      3) echo "WPS non è finito in un'ora: rilanciare per riprendere"; exit 3 ;;
      4) echo "WPS fermo: vedi la diagnosi qui sopra"; exit 4 ;;
    esac
  fi
  for e in geogrid.exe ungrib.exe metgrid.exe; do
    [ -s "$e" ] || { echo "WPS: manca $e, ultime righe:"; tail -n 60 compile.log; exit 1; }
  done
  echo "WPS compilato"
}

fase_installa() {
  echo "== installazione in $INSTALL"
  rm -rf "$INSTALL"
  mkdir -p "$INSTALL/WRF/run" "$INSTALL/WPS/geogrid" "$INSTALL/WPS/metgrid" "$INSTALL/WPS/ungrib/Variable_Tables"
  cp -L "$SRC/WRF/main/wrf.exe" "$SRC/WRF/main/real.exe" "$INSTALL/WRF/"
  cp -aL "$SRC/WRF/run/." "$INSTALL/WRF/run/"
  rm -f "$INSTALL"/WRF/run/*.exe "$INSTALL"/WRF/run/namelist.input
  cp -L "$SRC"/WPS/{geogrid,ungrib,metgrid}.exe "$SRC/WPS/link_grib.csh" "$INSTALL/WPS/"
  cp -L "$SRC/WPS/geogrid/GEOGRID.TBL.ARW" "$INSTALL/WPS/geogrid/GEOGRID.TBL"
  cp -L "$SRC/WPS/metgrid/METGRID.TBL.ARW" "$INSTALL/WPS/metgrid/METGRID.TBL"
  cp -L "$SRC/WPS/ungrib/Variable_Tables/Vtable.GFS" "$INSTALL/WPS/ungrib/Variable_Tables/"
  du -sh "$INSTALL"
  echo "fatto"
}

main() {
  case "${1:-tutto}" in
    prepara) fase_prepara ;;
    wrf) fase_wrf ;;
    wps) fase_wps ;;
    installa) fase_installa ;;
    tutto) fase_prepara; fase_wrf; fase_wps; fase_installa ;;
    *) echo "uso: bash wrf/costruisci.sh [prepara|wrf|wps|installa|tutto]"; exit 2 ;;
  esac
}

# Lanciato direttamente fa il lavoro; letto con "source" (come nelle prove) mette a disposizione solo le funzioni
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
