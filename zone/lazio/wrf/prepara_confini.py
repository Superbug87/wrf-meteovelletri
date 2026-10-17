#!/usr/bin/env python3
"""Confini regionali e provinciali per le mappe (dati/confini_lazio.json), da rifare solo se cambiano i confini.

  prepara_confini.py --geojson ~/openpolis/geojson-italy/geojson --out dati/confini_lazio.json

Legge limits_IT_regions.geojson e limits_IT_provinces.geojson di https://github.com/openpolis/geojson-italy
(confini amministrativi ISTAT elaborati da Openpolis, licenza CC BY 4.0), tiene il Lazio e le province che toccano
il riquadro delle mappe (con un margine), semplifica le linee (Douglas-Peucker, 100 m: meno di un decimo di pixel
sulle mappe) e scrive un piccolo file JSON che previsioni_lazio.py disegna: confine del Lazio più spesso,
confini provinciali più sottili.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import previsioni_lazio as pc  # noqa: E402

TOLLERANZA_KM = 0.1
MARGINE = 0.05           # gradi attorno al riquadro delle mappe


def semplifica(punti, tol_km):
    """Douglas-Peucker su coordinate (lon, lat) portate in km."""
    p = np.asarray(punti, dtype=float)
    if len(p) <= 4:
        return p
    xy = np.c_[(p[:, 0] - pc.LON0) * 111.32 * np.cos(np.radians(pc.LAT0)), (p[:, 1] - pc.LAT0) * 111.32]
    tieni = np.zeros(len(p), bool)
    tieni[0] = tieni[-1] = True
    pila = [(0, len(p) - 1)]
    while pila:
        a, b = pila.pop()
        if b <= a + 1:
            continue
        d = xy[b] - xy[a]
        n = np.hypot(*d)
        seg = xy[a + 1:b] - xy[a]
        dist = np.hypot(*seg.T) if n == 0 else np.abs(d[0] * seg[:, 1] - d[1] * seg[:, 0]) / n
        i = int(np.argmax(dist))
        if dist[i] > tol_km:
            m = a + 1 + i
            tieni[m] = True
            pila += [(a, m), (m, b)]
    return p[tieni]


def anelli(geom):
    """Gli anelli esterni di un Polygon o MultiPolygon (le isole contano: Ponza, Ventotene...)."""
    if geom["type"] == "Polygon":
        poligoni = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        poligoni = geom["coordinates"]
    else:
        raise ValueError(geom["type"])
    return [np.asarray(p[0], dtype=float)[:, :2] for p in poligoni]


def tocca_riquadro(anello):
    la0, la1 = pc.LAT_MIN - MARGINE, pc.LAT_MAX + MARGINE
    lo0, lo1 = pc.LON_MIN - MARGINE, pc.LON_MAX + MARGINE
    return not (anello[:, 0].max() < lo0 or anello[:, 0].min() > lo1 or anello[:, 1].max() < la0 or anello[:, 1].min() > la1)


def prepara(cartella):
    cartella = Path(cartella)
    regioni = json.loads((cartella / "limits_IT_regions.geojson").read_text(encoding="utf-8"))["features"]
    province = json.loads((cartella / "limits_IT_provinces.geojson").read_text(encoding="utf-8"))["features"]

    def arrotonda(a):
        return [[round(float(x), 4), round(float(y), 4)] for x, y in semplifica(a, TOLLERANZA_KM)]

    def vale(a):                                          # gli isolotti ridotti a 2-3 punti non si vedono: via
        return tocca_riquadro(a) and len(semplifica(a, TOLLERANZA_KM)) >= 4

    lazio = [f for f in regioni if f["properties"]["reg_name"] == "Lazio"]
    if len(lazio) != 1:
        sys.exit("Il Lazio non c'è nel file delle regioni")
    out = {
        "fonte": "Confini amministrativi ISTAT, elaborati da Openpolis (github.com/openpolis/geojson-italy), CC BY 4.0",
        "regione": [arrotonda(a) for a in anelli(lazio[0]["geometry"]) if vale(a)],
        "province": [],
    }
    for f in province:
        rings = [a for a in anelli(f["geometry"]) if vale(a)]
        if rings:
            out["province"].append({"nome": f["properties"]["prov_name"], "regione": f["properties"]["reg_name"],
                                    "anelli": [arrotonda(a) for a in rings]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geojson", required=True, help="cartella geojson di openpolis/geojson-italy")
    ap.add_argument("--out", default="dati/confini_lazio.json")
    a = ap.parse_args()
    out = prepara(a.geojson)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    punti = sum(len(r) for r in out["regione"]) + sum(len(r) for p in out["province"] for r in p["anelli"])
    lazio = sorted(p["nome"] for p in out["province"] if p["regione"] == "Lazio")
    print(f"Scritto {a.out}: Lazio {len(out['regione'])} anelli, {len(out['province'])} province ({', '.join(lazio)} nel Lazio), "
          f"{punti} punti, {Path(a.out).stat().st_size / 1024:.0f} kB")


if __name__ == "__main__":
    main()
