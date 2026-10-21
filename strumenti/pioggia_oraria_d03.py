#!/usr/bin/env python3
"""Pioggia oraria del dominio fine di WRF su tutta la griglia (per le prove del radar, radar-prove.yml).

  pioggia_oraria_d03.py --uscite CARTELLA --ciclo 2026100806 --out pioggia_lazio-ecmwf-radq_2026100806.npz

Salva lat, lon (2D), ore (dall'inizio del ciclo) e pioggia (mm nell'ora che finisce all'ora indicata, float16).
A differenza dell'estratto per la verifica (strumenti/estrai_verifica.py, un punto ogni due celle) qui c'e' ogni cella:
serve per misurare la posizione della pioggia alla risoluzione del modello.
"""
import argparse
import glob
import os
from datetime import datetime

import numpy as np
from netCDF4 import Dataset, chartostring


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uscite", required=True)
    ap.add_argument("--ciclo", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    c0 = datetime.strptime(a.ciclo, "%Y%m%d%H")
    files = sorted(glob.glob(os.path.join(a.uscite, "**", "wrfout_d03_*"), recursive=True))
    if not files:
        raise SystemExit("nessun wrfout_d03")
    cum, lat, lon = {}, None, None
    for f in files:
        with Dataset(f) as ds:
            tempi = [str(x) for x in chartostring(ds.variables["Times"][:])]
            for k, t in enumerate(tempi):
                h = round((datetime.strptime(t, "%Y-%m-%d_%H:%M:%S") - c0).total_seconds() / 3600, 3)
                if abs(h - round(h)) > 1e-3:
                    continue
                cum[int(round(h))] = (ds.variables["RAINNC"][k] + ds.variables["RAINC"][k]).astype("float32")
                if lat is None:
                    lat, lon = ds.variables["XLAT"][k].astype("float32"), ds.variables["XLONG"][k].astype("float32")
    ore = sorted(cum)
    ore = [h for h in ore if h - 1 in cum]
    P = np.stack([np.maximum(cum[h] - cum[h - 1], 0) for h in ore])
    np.savez_compressed(a.out, ciclo=a.ciclo, lat=lat, lon=lon, ore=np.array(ore, np.int16), pioggia=P.astype(np.float16))
    print(a.out, P.shape, "ore", ore[0], "..", ore[-1], "massimo orario", float(P.max()))


if __name__ == "__main__":
    main()
