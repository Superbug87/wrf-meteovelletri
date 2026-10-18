#!/usr/bin/env python3
"""Estratto di un run WRF per la verifica giornaliera (strumenti/verifica.py).

  estrai_verifica.py --uscite CARTELLA --stazioni strumenti/stazioni_verifica.json --ciclo AAAAMMGGHH --out FILE.npz

I file wrfout completi spariscono dopo pochi giorni: qui se ne tiene solo quello che serve a confrontarli con le
osservazioni, in poche decine di kB:
  - nei punti delle stazioni dentro il dominio fine (Regione Lazio e Aeronautica), ogni ora: temperatura a 2 m,
    punto di rugiada, vento a 10 m (velocita' e direzione), pioggia caduta nell'ora, pressione ridotta al livello del mare;
    in piu' la quota del terreno del modello nel punto (per riportare la temperatura alla quota della stazione);
  - la pioggia oraria su una griglia ridotta (un punto ogni PASSO_GRIGLIA celle), per il confronto con il radar.
"""
import argparse
import glob
import json
import os
from datetime import datetime

import numpy as np
from netCDF4 import Dataset

PASSO_GRIGLIA = 2          # 1 km -> 2 km sui Castelli, 2 km -> 4 km sul Lazio
CORNICE = 5                # celle di raccordo con il dominio padre: le stazioni li' non si usano
G, RD = 9.81, 287.05


def punto_di_rugiada(t2, q2, psfc):
    e = np.maximum(q2 * psfc / (0.622 + q2) / 100.0, 1e-3)        # hPa
    a = np.log(e / 6.112)
    return 243.5 * a / (17.67 - a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uscite", required=True)
    ap.add_argument("--stazioni", required=True)
    ap.add_argument("--ciclo", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.uscite, "**", "wrfout_d03_*"), recursive=True))
    if not files:
        raise SystemExit("nessun wrfout_d03")
    ciclo = datetime.strptime(a.ciclo, "%Y%m%d%H")

    # tutti gli istanti, senza doppioni (i segmenti ripartono dove finiscono i precedenti)
    istanti = {}
    for f in files:
        with Dataset(f) as ds:
            for i, t in enumerate(["".join(x.astype(str)) for x in ds["Times"][:]]):
                istanti.setdefault(t, (f, i))
    tempi = sorted(istanti)
    with Dataset(istanti[tempi[0]][0]) as ds:
        lat = np.asarray(ds["XLAT"][0], float)
        lon = np.asarray(ds["XLONG"][0], float)
        hgt = np.asarray(ds["HGT"][0], float)
    ny, nx = lat.shape

    # stazioni nel dominio (fuori dalla cornice): cella piu' vicina
    st = json.load(open(a.stazioni))
    elenco = [(s["id"], s["lat"], s["lon"]) for s in st["regione"] + st["aeronautica"]]
    ids, jj, ii = [], [], []
    coslat = np.cos(np.radians(lat.mean()))
    for sid, la, lo in elenco:
        d2 = (lat - la) ** 2 + ((lon - lo) * coslat) ** 2
        j, i = np.unravel_index(d2.argmin(), d2.shape)
        if CORNICE <= j < ny - CORNICE and CORNICE <= i < nx - CORNICE and d2[j, i] < (0.02 ** 2):
            ids.append(sid); jj.append(j); ii.append(i)
    jj, ii = np.array(jj, int), np.array(ii, int)

    n = len(tempi)
    T = np.full((n, len(ids)), np.nan, np.float32)
    TD, VV, DV, PP, MSLP = (T.copy() for _ in range(5))
    sl = (slice(None, None, PASSO_GRIGLIA), slice(None, None, PASSO_GRIGLIA))
    glat, glon = lat[sl], lon[sl]
    PG = np.full((n,) + glat.shape, np.nan, np.float32)
    prec_pt = prec_g = None
    ore = []
    for k, t in enumerate(tempi):
        f, i = istanti[t]
        with Dataset(f) as ds:
            t2 = np.asarray(ds["T2"][i], float)
            q2 = np.asarray(ds["Q2"][i], float)
            ps = np.asarray(ds["PSFC"][i], float)
            u = np.asarray(ds["U10"][i], float)
            v = np.asarray(ds["V10"][i], float)
            r = np.asarray(ds["RAINNC"][i], float)
            if "RAINC" in ds.variables:
                r = r + np.asarray(ds["RAINC"][i], float)
            if "COSALPHA" in ds.variables:                 # vento riportato a nord vero (la griglia di Lambert e' ruotata)
                ca, sa = np.asarray(ds["COSALPHA"][i], float), np.asarray(ds["SINALPHA"][i], float)
                u, v = u * ca - v * sa, v * ca + u * sa
        td = punto_di_rugiada(t2, q2, ps)
        tv = t2 * (1 + 0.61 * q2) + 0.0065 * hgt / 2
        mslp = ps / 100.0 * np.exp(G * hgt / (RD * tv))
        T[k] = t2[jj, ii] - 273.15
        TD[k] = td[jj, ii]
        VV[k] = np.hypot(u, v)[jj, ii]
        DV[k] = (np.degrees(np.arctan2(-u, -v)) % 360)[jj, ii]   # direzione da cui viene
        MSLP[k] = mslp[jj, ii]
        if prec_pt is not None:
            PP[k] = np.maximum(r[jj, ii] - prec_pt, 0)
            PG[k] = np.maximum(r[sl] - prec_g, 0)
        prec_pt, prec_g = r[jj, ii], r[sl]
        ore.append(int(round((datetime.strptime(t, "%Y-%m-%d_%H:%M:%S") - ciclo).total_seconds() / 3600)))

    np.savez_compressed(
        a.out, ciclo=a.ciclo, ore=np.array(ore, np.int16), stazioni=np.array(ids), quota_modello=hgt[jj, ii].astype(np.float32),
        t2=T, td2=TD, vento=VV, dir_vento=DV, pioggia=PP, mslp=MSLP,
        griglia_lat=glat.astype(np.float32), griglia_lon=glon.astype(np.float32), griglia_pioggia=PG.astype(np.float16))
    print(f"estratto: {len(ids)} stazioni nel dominio, {n} istanti (ore {ore[0]}..{ore[-1]}), griglia {glat.shape}, "
          f"{os.path.getsize(a.out) / 1024:.0f} kB -> {a.out}")


if __name__ == "__main__":
    main()
