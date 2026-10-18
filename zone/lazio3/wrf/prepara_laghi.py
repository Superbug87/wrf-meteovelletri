#!/usr/bin/env python3
"""Contorni reali dei laghi per le mappe (dati/laghi_lazio.json), da rifare solo se servono altri laghi.

  prepara_laghi.py --out dati/laghi_lazio.json              (scarica da OpenStreetMap tramite Overpass)
  prepara_laghi.py --osm laghi_osm.json --out dati/laghi_lazio.json   (usa un file già scaricato)

Prende gli specchi d'acqua di OpenStreetMap (natural=water, esclusi fiumi, canali, stagni, vasche) nel riquadro
delle mappe, ricompone i contorni (anche i laghi fatti da più linee, come Bolsena, con le isole), tiene quelli
di almeno 1 km², li semplifica (50 m) e scrive un piccolo file JSON che previsioni_lazio.py disegna.
Dati © OpenStreetMap contributors, licenza ODbL (https://www.openstreetmap.org/copyright).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import previsioni_lazio as pc  # noqa: E402
from prepara_confini import semplifica  # noqa: E402

SERVER = ["https://overpass.openstreetmap.fr/api/interpreter", "https://overpass.osm.ch/api/interpreter"]
ESCLUDI_ACQUA = "river|canal|ditch|pond|basin|stream|stream_pool|moat|wastewater|fishpond|reflecting_pool|lock"
ESCLUDI_NOME = re.compile(r"^(Porto|Porticciolo|Canale|Fiume|Darsena|Fontana|Vasca)\b", re.I)
AREA_MINIMA_KM2 = 1.0
TOLLERANZA_KM = 0.05
MARGINE = 0.1


def richiesta():
    la0, la1 = pc.LAT_MIN - MARGINE, pc.LAT_MAX + MARGINE
    lo0, lo1 = pc.LON_MIN - MARGINE, pc.LON_MAX + MARGINE
    box = f"({la0:.2f},{lo0:.2f},{la1:.2f},{lo1:.2f})"
    return (f'[out:json][timeout:170];(way["natural"="water"]["water"!~"{ESCLUDI_ACQUA}"]{box};'
            f'rel["natural"="water"]["water"!~"{ESCLUDI_ACQUA}"]{box};);out geom;')


def scarica():
    import requests

    for url in SERVER:
        try:
            r = requests.post(url, data={"data": richiesta()}, headers={"User-Agent": "wrf-lazio-laghi/1.0"}, timeout=200)
            if r.status_code == 200 and r.json().get("elements"):
                return r.json()
            print(f"{url}: HTTP {r.status_code}", file=sys.stderr)
        except (requests.RequestException, ValueError) as e:
            print(f"{url}: {str(e)[:100]}", file=sys.stderr)
        time.sleep(5)
    sys.exit("Overpass non risponde")


def punti(geom):
    return [(g["lon"], g["lat"]) for g in geom]


def unisci(linee):
    """Ricompone anelli chiusi da linee che si toccano agli estremi."""
    linee = [list(l) for l in linee if len(l) >= 2]
    anelli = []
    while linee:
        anello = linee.pop(0)
        while anello[0] != anello[-1]:
            for i, l in enumerate(linee):
                if l[0] == anello[-1]:
                    anello += l[1:]
                elif l[-1] == anello[-1]:
                    anello += l[::-1][1:]
                elif l[-1] == anello[0]:
                    anello = l[:-1] + anello
                elif l[0] == anello[0]:
                    anello = l[::-1][:-1] + anello
                else:
                    continue
                linee.pop(i)
                break
            else:
                break                                      # contorno aperto (tagliato dal riquadro): si scarta
        if anello[0] == anello[-1] and len(anello) >= 4:
            anelli.append(anello)
    return anelli


def area_km2(anello):
    a = np.asarray(anello)
    x = (a[:, 0] - pc.LON0) * 111.32 * np.cos(np.radians(pc.LAT0))
    y = (a[:, 1] - pc.LAT0) * 111.32
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def elabora(osm):
    laghi = []
    for e in osm["elements"]:
        t = e.get("tags", {})
        if ESCLUDI_NOME.match(t.get("name", "")):
            continue
        if e["type"] == "way":
            esterni, interni = unisci([punti(e["geometry"])]), []
        else:
            esterni = unisci([punti(m["geometry"]) for m in e.get("members", []) if m.get("role") == "outer" and "geometry" in m])
            interni = unisci([punti(m["geometry"]) for m in e.get("members", []) if m.get("role") == "inner" and "geometry" in m])
        if not esterni:
            continue
        area = sum(abs(area_km2(a)) for a in esterni) - sum(abs(area_km2(a)) for a in interni)
        if area < AREA_MINIMA_KM2:
            continue

        def semplice(a):
            s = semplifica(a, TOLLERANZA_KM)
            return [[round(float(x), 5), round(float(y), 5)] for x, y in s] if len(s) >= 4 else None
        ext = [r for r in (semplice(a) for a in esterni) if r]
        inn = [r for r in (semplice(a) for a in interni) if r]
        if ext:
            laghi.append({"nome": t.get("name", ""), "area_km2": round(area, 1), "esterni": ext, "interni": inn})
    laghi.sort(key=lambda l: -l["area_km2"])
    return laghi


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--osm", help="file JSON di Overpass già scaricato (altrimenti si scarica)")
    ap.add_argument("--out", default="dati/laghi_lazio.json")
    a = ap.parse_args()
    osm = json.loads(Path(a.osm).read_text(encoding="utf-8")) if a.osm else scarica()
    laghi = elabora(osm)
    out = {"fonte": "© OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)", "laghi": laghi}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"Scritto {a.out}: {len(laghi)} laghi di almeno {AREA_MINIMA_KM2} km², {Path(a.out).stat().st_size / 1024:.0f} kB")
    for l in laghi[:40]:
        print(f"  {l['area_km2']:7.1f} km²  {l['nome'] or '(senza nome)'}")


if __name__ == "__main__":
    main()
