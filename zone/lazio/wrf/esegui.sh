#!/usr/bin/env bash
# Uso:
#   esegui.sh prepara CICLO ORE_TOTALI        scarica i dati globali, ungrib, metgrid, real.exe
#                                             (PILOTA=icon2i, il default; ecmwf e gfs restano possibili)
#   esegui.sh modello CICLO DA_ORA A_ORA      wrf.exe da DA_ORA ad A_ORA (ore dall'inizio del ciclo);
#                                             DA_ORA > 0 riprende dal restart dell'ultimo segmento
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
INSTALL=${INSTALL:-$HOME/wrf-install}
GEO=${GEO:-$HOME/wrf-geo}
LAVORO=${LAVORO:-$HOME/lavoro}
export OMP_NUM_THREADS=1
PILOTA=${PILOTA:-icon2i}; export PILOTA            # config.py ne ricava l'intervallo tra i file dei bordi
case "$PILOTA" in icon2i|ecmwf|iconeu|gfs) ;; *) echo "PILOTA deve essere icon2i, ecmwf, iconeu o gfs"; exit 2;; esac
PREFISSO=$(echo "$PILOTA" | tr a-z A-Z)          # prefisso dei file intermedi di WPS: ECMWF o GFS
cmd=${1:?comando}; CICLO=${2:?ciclo}

cartella_run() {
  mkdir -p "$LAVORO/run"
  cd "$LAVORO/run"
  [ -e wrf.exe ] || { cp -aL "$INSTALL/WRF/run/." .; cp -L "$INSTALL/WRF/wrf.exe" "$INSTALL/WRF/real.exe" .; }
}

esito_wrf() {  # $1 = file rsl da controllare
  if ! tail -n 5 "$1" | grep -q "SUCCESS COMPLETE"; then
    echo "!! Esecuzione non riuscita. Ultime righe di $1:"; tail -n 40 "$1"
    for f in rsl.error.*; do
      grep -l -iE "fatal|segmentation|cfl|error" "$f" >/dev/null 2>&1 && { echo "--- $f"; tail -n 20 "$f"; break; }
    done
    exit 1
  fi
}

case "$cmd" in
prepara)
  ORE=${3:?ore totali}
  mkdir -p "$LAVORO/WPS" "$LAVORO/dati" "$LAVORO/run"
  cd "$LAVORO"
  echo "== $PREFISSO $CICLO, ore 0-$ORE"
  if [ -n "${DATI_PRONTI:-}" ] && [ -d "$DATI_PRONTI" ]; then
    echo "dati globali gia' scaricati: $DATI_PRONTI"; rmdir dati; ln -sfn "$DATI_PRONTI" dati   # rmdir: senza, il collegamento finisce DENTRO la cartella
  else
    python3 "$QUI/scarica_$PILOTA.py" --ciclo "$CICLO" --ore "$ORE" --out dati
  fi
  du -sh dati; df -h / | tail -n1

  echo "== WPS: ungrib e metgrid"
  cp -aL "$INSTALL/WPS/." WPS/
  cp "$GEO"/geo_em.d0*.nc WPS/
  cd WPS
  python3 "$QUI/config.py" wps --ciclo "$CICLO" --ore "$ORE" --pilota "$PILOTA" > namelist.wps
  ./link_grib.csh ../dati/*
  if [ "$PILOTA" = gfs ]; then ln -sf "$QUI/Vtable.GFS_IDRO" Vtable; else ln -sf "$QUI/Vtable.ECMWF_OD" Vtable; fi
  if [ "$PILOTA" = gfs ]; then
    # i campi di acqua di nube di GFS stanno solo su una parte dei livelli di pressione: metgrid vuole gli stessi livelli
    # di tutti gli altri campi, quindi quelli mancanti si riempiono con zero (invece di fermarsi con un errore di scrittura)
    awk '/^name=/ {blocco = ($0 ~ /^name=(QC|QI|QR|QS|QG)$/)} blocco && /fill_lev=/ {print "        fill_lev=all:const(0.)"; next} {print}' \
      metgrid/METGRID.TBL > metgrid/METGRID.TBL.nuova && mv metgrid/METGRID.TBL.nuova metgrid/METGRID.TBL
  fi
  ./ungrib.exe > ungrib.log 2>&1 || { tail -n 40 ungrib.log; exit 1; }
  ls "$PREFISSO":* | head -n 3
  if [ -L ../dati ]; then rm -f ../dati; else rm -rf ../dati; fi; rm -rf GRIBFILE.*
  if [ "${SST_MARE:-1}" = 1 ]; then          # temperatura del mare ad alta risoluzione (facoltativa)
    if python3 "$QUI/scarica_sst.py" --ciclo "$CICLO" --ore "$ORE" --out .; then
      python3 "$QUI/config.py" wps --ciclo "$CICLO" --ore "$ORE" --pilota "$PILOTA" --sst > namelist.wps
      SST_USATA=1
    else
      echo "!! MUR SST non disponibile: si usa la temperatura del mare di $PREFISSO"; rm -f SST:*
    fi
  fi
  ./metgrid.exe > metgrid.log 2>&1 || { tail -n 40 metgrid.log; exit 1; }
  rm -f "$PREFISSO":* SST:*
  if [ "${SST_USATA:-0}" = 1 ]; then echo "MUR SST" > "$LAVORO/run/sst_usata.txt"; fi   # le mappe lo citano
  ls met_em.d0* | head -n 3
  read -r NLEV NSOIL < <(python3 "$QUI/config.py" metgrid-info "$(ls met_em.d01.* | head -n1)")
  echo "livelli metgrid: $NLEV, livelli suolo: $NSOIL"

  echo "== real.exe"
  cartella_run
  ln -sf ../WPS/met_em.d0* .
  python3 "$QUI/config.py" input --ciclo "$CICLO" --da 0 --a "$ORE" --nlev "$NLEV" --nsoil "$NSOIL" > namelist.input
  rm -f rsl.*
  mpirun -np 1 ./real.exe > real.log 2>&1 || { tail -n 40 real.log; tail -n 30 rsl.error.0000 || true; exit 1; }
  esito_wrf rsl.error.0000
  ls -la wrfinput_d0* wrfbdy_d01
  if [ "${IDRO:-1}" != 0 ] && [ "$PILOTA" != ecmwf ]; then     # real.exe scrive nei bordi solo il vapore: l'acqua di ICON/GFS ce la mette questo script
    echo "== acqua di nube, pioggia, neve nei bordi"
    python3 "$QUI/idro_bordi.py" --run . --ciclo "$CICLO" ||
      { echo "!! idro_bordi non riuscito: WRF gira senza acqua nei bordi"; echo "errore dello script" > idro_ko.txt; }
  fi
  if [ "$PILOTA" = ecmwf ] && [[ "-${FISICA:-}-" == *-idroec-* ]]; then   # PROVA: ECMWF non da' nubi e pioggia in quota
    echo "== nubi, ghiaccio e pioggia iniziali ricostruiti dai totali di colonna di ECMWF"
    python3 "$QUI/idro_iniziale.py" --run . --ciclo "$CICLO" ||
      echo "!! idro_iniziale non riuscito: WRF parte senza nubi, come nei run normali"
  fi
  rm -rf "$LAVORO/WPS"

  # NUDGING_FONTI: wu (Wunderground, serve WU_API_KEY), ufficiali (Regione Lazio e Aeronautica) o tutte; STAZIONI=false: niente
  echo "== Stazioni per il nudging (fonti: ${NUDGING_FONTI:-wu})"
  if [ "${STAZIONI:-true}" = false ]; then
    echo "nudging disattivato"
  else
    python3 "$QUI/stazioni_wrf.py" --ciclo "$CICLO" --geo "$GEO" --out . ||
      { echo "!! Stazioni non utilizzabili: WRF girera' senza nudging"; rm -f OBS_DOMAIN* obs_fdda_end.txt obs_fonti.txt; }
  fi
  ;;
modello)
  DA=${3:?da ora}; A=${4:?a ora}
  cartella_run
  if [ "$DA" -gt 0 ]; then                     # ripresa: servono wrfinput, wrfbdy e il restart del segmento prima
    tar -xzf "$LAVORO/stato.tgz" -C "$LAVORO/run"
    if [ -s obs.tgz.enc ]; then                # le osservazioni delle stazioni viaggiano cifrate (vedi sotto)
      if [ -n "${WU_API_KEY:-}" ] && openssl enc -d -aes-256-cbc -pbkdf2 -pass env:WU_API_KEY -in obs.tgz.enc | tar -xzf -; then
        rm -f obs.tgz.enc
      else
        echo "!! Osservazioni delle stazioni non leggibili: questo segmento gira senza nudging"
        rm -f OBS_DOMAIN* obs_fdda_end.txt obs_fonti.txt obs.tgz.enc
      fi
    fi
  fi
  OBS=()
  if [ -s obs_fdda_end.txt ] && [ -s OBS_DOMAIN301 ]; then
    OBS=(--obs-minuti "$(tr -d '[:space:]' < obs_fdda_end.txt)")
    echo "nudging sulle stazioni fino a +$(cat obs_fdda_end.txt) minuti"
  fi
  python3 "$QUI/config.py" input --ciclo "$CICLO" --da "$DA" --a "$A" "${OBS[@]}" > namelist.input
  rm -f rsl.*
  echo "== wrf.exe da +${DA} h a +${A} h su $(nproc --all) processori ($(date -u +%H:%M) UTC)"
  mpirun -np "$(nproc --all)" ./wrf.exe > wrf.log 2>&1 || { tail -n 40 wrf.log; esito_wrf rsl.error.0000; exit 1; }
  esito_wrf rsl.error.0000
  FINE=$(date -u -d "${CICLO:0:8} ${CICLO:8:2}:00 UTC + ${A} hours" +%Y-%m-%d_%H:%M:%S)
  for d in 1 2 3; do   # senza il restart di tutti e tre i domini il segmento dopo non puo' ripartire
    [ -s "wrfrst_d0${d}_${FINE}" ] || { echo "!! manca il restart del dominio ${d} alle ${FINE}"; ls wrfrst_* 2>/dev/null || true; exit 1; }
  done
  echo "fine ($(date -u +%H:%M) UTC)"
  ls -la wrfout_d03_* wrfrst_d03_* 2>/dev/null | tail -n 8 || true
  # i nomi dei file di WRF contengono ":" e GitHub non li accetta come allegati: li impacchettiamo
  # le osservazioni delle stazioni servono anche ai segmenti dopo, e la loro presenza va saputa dalle mappe
  # (il repository e' pubblico e gli allegati si scaricano: le osservazioni, con i codici e le posizioni delle
  #  stazioni, escono solo cifrate con la chiave Wunderground; in chiaro resta il solo istante finale)
  EXTRA=()
  if [ -s obs_fdda_end.txt ] && [ -n "${WU_API_KEY:-}" ] && ls OBS_DOMAIN* >/dev/null 2>&1; then
    tar -czf - OBS_DOMAIN* | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:WU_API_KEY -out obs.tgz.enc
    EXTRA=(obs.tgz.enc obs_fdda_end.txt)
  elif [ -s obs_fdda_end.txt ]; then
    EXTRA=(obs_fdda_end.txt)
  fi
  USC=(); [ -s obs_fdda_end.txt ] && USC+=(obs_fdda_end.txt)
  if [ -s sst_usata.txt ]; then EXTRA+=(sst_usata.txt); USC+=(sst_usata.txt); fi
  if [ -s obs_fdda_end.txt ] && [ -s obs_fonti.txt ]; then EXTRA+=(obs_fonti.txt); USC+=(obs_fonti.txt); fi
  [ -e idro_ko.txt ] && EXTRA+=(idro_ko.txt)
  tar -czf "$LAVORO/stato.tgz" wrfinput_d0* wrfbdy_d01 wrfrst_d0*_"$FINE" "${EXTRA[@]}"
  tar -czf "$LAVORO/uscite.tgz" wrfout_d03_* wrfxtrm_d03_* "${USC[@]}"
  ls -la "$LAVORO"/*.tgz
  ;;
*)
  echo "comando sconosciuto: $cmd"; exit 2 ;;
esac
