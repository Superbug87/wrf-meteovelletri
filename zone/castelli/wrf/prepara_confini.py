#!/usr/bin/env python3
"""Confini provinciali e comunali per le mappe (dati/confini.json), da rifare solo se cambiano i confini.

  prepara_confini.py --geojson cartella_con_i_file_openpolis --out dati/confini.json

Legge limits_IT_provinces.geojson e limits_R_12_municipalities.geojson (Lazio) di
https://github.com/openpolis/geojson-italy (confini amministrativi ISTAT elaborati da Openpolis, CC BY 4.0), tiene i comuni
e le province che toccano il riquadro delle mappe, semplifica le linee (Douglas-Peucker, 25 m: un quinto di pixel sulle
mappe) e scrive un piccolo JSON che previsioni_castelli.py disegna con linee fini.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import previsioni_castelli as pc  # noqa: E402

TOLLERANZA_KM = 0.025
MARGINE = 0.03            # gradi attorno al riquadro delle mappe


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
    """Tutti gli anelli (esterni e interni: enclave come lo Stato della Città del Vaticano dentro Roma) di un (Multi)Polygon."""
    poligoni = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [np.asarray(r, dtype=float)[:, :2] for p in poligoni for r in p]


def tocca(a):
    return not (a[:, 0].max() < pc.LON_MIN - MARGINE or a[:, 0].min() > pc.LON_MAX + MARGINE
                or a[:, 1].max() < pc.LAT_MIN - MARGINE or a[:, 1].min() > pc.LAT_MAX + MARGINE)


def ritaglia(a):
    """Tiene solo i tratti dell'anello dentro il riquadro (con margine): niente chilometri di costa fuori mappa."""
    dentro = ((a[:, 0] >= pc.LON_MIN - MARGINE) & (a[:, 0] <= pc.LON_MAX + MARGINE) &
              (a[:, 1] >= pc.LAT_MIN - MARGINE) & (a[:, 1] <= pc.LAT_MAX + MARGINE))
    # un punto è utile se è dentro o confina con uno dentro (così la linea arriva fino al bordo)
    utile = dentro | np.r_[dentro[1:], False] | np.r_[False, dentro[:-1]]
    tratti, cur = [], []
    for p, u in zip(a, utile):
        if u:
            cur.append(p)
        elif cur:
            tratti.append(np.array(cur))
            cur = []
    if cur:
        tratti.append(np.array(cur))
    if len(tratti) > 1 and utile[0] and utile[-1]:            # l'anello è chiuso: primo e ultimo tratto si uniscono
        tratti[0] = np.vstack([tratti[-1], tratti[0]])
        tratti.pop()
    return [t for t in tratti if len(t) >= 2]


def prepara(cartella):
    cartella = Path(cartella)
    province = json.loads((cartella / "limits_IT_provinces.geojson").read_text(encoding="utf-8"))["features"]
    comuni = json.loads((cartella / "limits_R_12_municipalities.geojson").read_text(encoding="utf-8"))["features"]

    def arrotonda(a):
        return [[round(float(x), 4), round(float(y), 4)] for x, y in semplifica(a, TOLLERANZA_KM)]

    out = {
        "fonte": "Confini amministrativi ISTAT, elaborati da Openpolis (github.com/openpolis/geojson-italy), CC BY 4.0",
        "province": [],
        "comuni": [],
    }
    for f in province:
        tratti = [t for a in anelli(f["geometry"]) if tocca(a) for t in ritaglia(a)]
        if tratti:
            out["province"].append({"nome": f["properties"]["prov_name"], "linee": [arrotonda(t) for t in tratti]})
    for f in comuni:
        tratti = [t for a in anelli(f["geometry"]) if tocca(a) for t in ritaglia(a)]
        if tratti:
            out["comuni"].append({"nome": f["properties"]["name"], "provincia": f["properties"]["prov_name"],
                                  "linee": [arrotonda(t) for t in tratti]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geojson", required=True, help="cartella con i file geojson di openpolis/geojson-italy")
    ap.add_argument("--out", default="dati/confini.json")
    a = ap.parse_args()
    out = prepara(a.geojson)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    punti = sum(len(r) for k in ("province", "comuni") for p in out[k] for r in p["linee"])
    print(f"Scritto {a.out}: {len(out['province'])} province ({', '.join(p['nome'] for p in out['province'])}), "
          f"{len(out['comuni'])} comuni, {punti} punti, {Path(a.out).stat().st_size / 1024:.0f} kB")


if __name__ == "__main__":
    main()
