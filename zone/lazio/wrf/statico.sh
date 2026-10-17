#!/usr/bin/env bash
# Dati statici (terreno, uso del suolo, suoli): scarica i dati geografici NCAR, lancia geogrid
# e conserva solo i piccoli file geo_em.d0*.nc (messi in cache: si fa una volta sola).
set -euo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
INSTALL=${INSTALL:-$HOME/wrf-install}
GEO=${GEO:-$HOME/wrf-geo}
GEOG=$HOME/dati-geografici
URL=https://www2.mmm.ucar.edu/wrf/src/wps_files/geog_high_res_mandatory.tar.gz

# liberiamo spazio sul runner: i dati geografici scompattati sono parecchi GB
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL || true
df -h / | tail -n1

mkdir -p "$GEOG" "$GEO"
echo "== download dei dati geografici (in streaming)"
curl -fL --retry 8 --retry-delay 15 --retry-all-errors "$URL" | tar -xz -C "$GEOG"
TOPO=$(find "$GEOG" -maxdepth 3 -type d -name topo_gmted2010_30s | head -n1)
[ -n "$TOPO" ] || { echo "topo_gmted2010_30s non trovato nell'archivio:"; ls -R "$GEOG" | head -n 40; exit 1; }
GEOG_PATH=$(dirname "$TOPO")
echo "dati geografici in: $GEOG_PATH"; ls "$GEOG_PATH"

W=$HOME/lavoro-geogrid
rm -rf "$W"; mkdir -p "$W"
cp -aL "$INSTALL/WPS/." "$W/"
cd "$W"
python3 "$QUI/config.py" wps --ciclo 2026010100 --ore 6 --geog "$GEOG_PATH" > namelist.wps
./geogrid.exe > geogrid.log 2>&1 || { tail -n 60 geogrid.log; exit 1; }
tail -n 3 geogrid.log
ls geo_em.d0*.nc
cp geo_em.d0*.nc "$GEO/"
du -sh "$GEO"
