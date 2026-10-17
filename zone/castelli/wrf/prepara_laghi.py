#!/usr/bin/env python3
"""Contorni reali dei laghi per le mappe (dati/laghi.json), da rifare solo se cambia qualcosa.

  prepara_laghi.py --out dati/laghi.json

Legge l'acqua (classe 80) di ESA WorldCover 2021 a 10 m sul riquadro delle mappe, ne ricava i contorni dei laghi
(escluso il mare e l'acqua che lo tocca), li semplifica (Douglas-Peucker, 15 m) e scrive un piccolo JSON che
previsioni_castelli.py disegna al posto dei cerchi. Fonte: ESA WorldCover 2021 v200, CC BY 4.0
(© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) processed by ESA WorldCover consortium).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import previsioni_castelli as pc  # noqa: E402
from prepara_confini import semplifica  # noqa: E402

URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
       "ESA_WorldCover_10m_2021_v200_N{la:02d}E{lo:03d}_Map.tif")
PIX = 12000                          # punti per grado
TOLLERANZA_KM = 0.015
AREA_MIN_KM2 = 0.1                   # sotto questa i laghetti non si vedono sulle mappe
NOMI = [("Albano", 41.748, 12.667), ("Nemi", 41.7227, 12.7138)]


def leggi_acqua(la0, la1, lo0, lo1):
    import rasterio
    la, lo = (int(np.floor(x / 3)) * 3 for x in (la0, lo0))
    assert int(np.floor((la1 - 1e-9) / 3)) * 3 == la and int(np.floor((lo1 - 1e-9) / 3)) * 3 == lo, "il riquadro cade su due tessere"
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif"):
        with rasterio.open("/vsicurl/" + URL.format(la=la, lo=lo)) as s:
            r0, r1 = round((la + 3 - la1) * PIX), round((la + 3 - la0) * PIX)
            c0, c1 = round((lo0 - lo) * PIX), round((lo1 - lo) * PIX)
            wc = s.read(1, window=((r0, r1), (c0, c1)))
    return (wc == 80)[::-1]                       # righe dal sud


def main():
    from scipy import ndimage
    from skimage import measure

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dati/laghi.json")
    a = ap.parse_args()
    # riquadro delle mappe con un po' di margine, arrotondato al decimo di grado (la griglia è a 1/12000)
    la0, la1 = np.floor((pc.LAT_MIN - 0.03) * 10) / 10, np.ceil((pc.LAT_MAX + 0.03) * 10) / 10
    lo0, lo1 = np.floor((pc.LON_MIN - 0.03) * 10) / 10, np.ceil((pc.LON_MAX + 0.03) * 10) / 10
    acqua = leggi_acqua(la0, la1, lo0, lo1)
    et, n = ndimage.label(acqua)
    km2 = (111.32 * np.cos(np.radians(pc.LAT0)) / PIX) * (111.32 / PIX)
    aree = np.bincount(et.ravel(), minlength=n + 1) * km2
    bordo = set(np.unique(np.r_[et[0], et[-1], et[:, 0], et[:, -1]]).tolist()) - {0}
    laghi = []
    for k in range(1, n + 1):
        if k in bordo or aree[k] < AREA_MIN_KM2:      # mare, Tevere e simili toccano il bordo: non sono laghi
            continue
        maschera = np.pad(et == k, 1)
        # contorno esterno (il più lungo); le isole dentro il lago sono trascurabili
        c = max(measure.find_contours(maschera.astype(float), 0.5), key=len) - 1
        lat = la0 + (c[:, 0] + 0.5) / PIX
        lon = lo0 + (c[:, 1] + 0.5) / PIX
        pts = semplifica(np.c_[lon, lat], TOLLERANZA_KM)
        clon, clat = float(lon.mean()), float(lat.mean())
        nome = next((nm for nm, la, lo in NOMI if abs(la - clat) < 0.03 and abs(lo - clon) < 0.03), "")
        laghi.append({"nome": nome, "area_km2": round(float(aree[k]), 2),
                      "anello": [[round(float(x), 5), round(float(y), 5)] for x, y in pts]})
    laghi.sort(key=lambda l: -l["area_km2"])
    out = {"fonte": "ESA WorldCover 2021 v200, CC BY 4.0 (© ESA WorldCover project 2021 / Copernicus Sentinel data 2021)",
           "laghi": laghi}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"Scritto {a.out}: {len(laghi)} laghi, {Path(a.out).stat().st_size / 1024:.0f} kB")
    for l in laghi:
        print(f"  {l['nome'] or '(senza nome)':14s} {l['area_km2']:6.2f} km2, {len(l['anello'])} punti")


if __name__ == "__main__":
    main()
