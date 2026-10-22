#!/usr/bin/env python3
"""Confronto con il radar di TUTTE le versioni di WRF (ufficiali e prove), per scadenza da +1 a +48 ore.

  valuta_modelli_radar.py --estratti CARTELLA --zona lazio --giorni 5 --out CARTELLA_OUT

Legge gli estratti per la verifica (estratto_<zona>-<pilota>[-<variante>]_<ciclo>.npz, griglia_pioggia oraria) dei cicli degli ultimi
giorni, scarica il radar SRI del DPC (media oraria a 2 km), porta la pioggia del modello sulla griglia del radar e calcola per ogni
versione, a scadenze di 3 ore, FSS a 20 km (0,5 e 2 mm/h) e rapporto tra pioggia prevista e osservata. Disegna un grafico per
famiglia (ECMWF, ICON-2I, altri piloti). Dove il radar non copre il dominio la cella e' esclusa per tutte le versioni allo stesso modo.
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(QUI, "..", "zone", "lazio", "wrf"))
sys.path.insert(0, QUI)
import radar  # noqa: E402
from valuta_ciclo_rapido import blocchi2, fss_acc, lonlat_griglia, regrid  # noqa: E402

LAT0, LON0 = 41.9, 12.7
SOGLIE = (0.5, 2.0)
BIN = 3                                           # ore per punto del grafico


def radar_orario(t_ini, t_fine):
    """{ora_fine_ms: matrice 2 km (mm/h medi nell'ora che finisce a quell'istante)}, orig, ora per ora."""
    n = int((t_fine - t_ini) / 300000) + 1
    ist, fr, orig = radar.serie("SRI", t_fine, n, ritaglio=(LAT0, LON0, 250))
    per = {}
    for h in range(int(t_ini // 3600000) + 1, int(t_fine // 3600000) + 1):
        fine = h * 3600000
        mos = [fr[ist.index(fine - k * 300000)] for k in range(0, 12) if (fine - k * 300000) in ist]
        mos = [m for m in mos if m is not None]
        if len(mos) >= 9:
            per[fine] = blocchi2(np.nanmean(np.stack(mos), axis=0))
    return per, orig


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--estratti", required=True)
    ap.add_argument("--zona", default="lazio", choices=["lazio", "castelli"])
    ap.add_argument("--giorni", type=int, default=5)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    adesso = datetime.now(timezone.utc)
    files = {}
    for f in glob.glob(os.path.join(a.estratti, f"estratto_{a.zona}-*.npz")):
        m = re.match(rf"estratto_{a.zona}-(ecmwf|icon2i|iconeu|gfs)(?:-(.+))?_(\d{{10}})\.npz$", os.path.basename(f))
        if not m:
            continue
        c = datetime.strptime(m.group(3), "%Y%m%d%H").replace(tzinfo=timezone.utc)
        if adesso - c > timedelta(days=a.giorni) or c + timedelta(hours=48) > adesso - timedelta(hours=1):
            continue                                       # solo cicli vecchi di meno di N giorni e finiti
        files[(m.group(1), m.group(2) or "", m.group(3))] = f
    if not files:
        raise SystemExit("nessun estratto")
    cicli = sorted({k[2] for k in files})
    t0 = int(datetime.strptime(cicli[0], "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp() * 1000)
    t1 = int((datetime.strptime(cicli[-1], "%Y%m%d%H").replace(tzinfo=timezone.utc) + timedelta(hours=48)).timestamp() * 1000)
    print("radar", cicli[0], "->", cicli[-1], "+48 h", flush=True)
    per, orig = radar_orario(t0, t1)
    print("ore di radar:", len(per), flush=True)
    H, W = next(iter(per.values())).shape
    lat_r, lon_r = lonlat_griglia(orig, H, W)
    acc = {}                                              # (pilota, var) -> {(bin, soglia): [num, den]}, bias
    bias = {}
    dominio = {}
    for (pil, var, c), f in sorted(files.items()):
        d = np.load(f, allow_pickle=True)
        if "griglia_pioggia" not in d.files:
            continue
        ore = [int(x) for x in d["ore"]]
        P = d["griglia_pioggia"].astype(np.float32)
        T0 = int(datetime.strptime(c, "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp() * 1000)
        orelist = [h for h in range(1, 49) if h in ore and (T0 + h * 3600000) in per]
        if not orelist:
            continue
        # il modello sulla griglia radar (una volta per ora); la copertura e' la stessa per tutte le versioni dello stesso dominio
        cam = regrid(d["griglia_lat"], d["griglia_lon"], [P[ore.index(h)] for h in orelist], lat_r, lon_r)
        chiave = d["griglia_lat"].shape
        if chiave not in dominio:                          # celle del radar dentro il dominio del modello
            dominio[chiave] = copertura(d["griglia_lat"], d["griglia_lon"], lat_r, lon_r)
        dom = dominio[chiave]
        for k, h in enumerate(orelist):
            o = per[T0 + h * 3600000]
            ok = np.isfinite(o) & dom
            b = (h - 1) // BIN
            for s in SOGLIE:
                x = fss_acc(cam[k] >= s, np.nan_to_num(o) >= s, ok)
                r = acc.setdefault((pil, var), {}).setdefault((b, s), [0.0, 0.0])
                r[0] += x[0]; r[1] += x[1]
            bb = bias.setdefault((pil, var), {}).setdefault(b, [0.0, 0.0])
            bb[0] += float((cam[k] * ok).sum()); bb[1] += float(np.nansum(o * ok))
        print("ciclo", c, pil, var or "-", len(orelist), "ore", flush=True)
    json.dump({f"{p}|{v}": {"fss": {f"{b}|{s}": x for (b, s), x in dd.items()}, "bias": bias[(p, v)]} for (p, v), dd in acc.items()},
              open(os.path.join(a.out, f"modelli_radar_{a.zona}.json"), "w"))
    disegna(acc, bias, a.zona, len(cicli), os.path.join(a.out, f"confronto_modelli_radar_{a.zona}.png"))


def copertura(lat_w, lon_w, lat_r, lon_r):
    from pyproj import Transformer
    from scipy.interpolate import griddata
    tr = Transformer.from_crs("EPSG:4326", radar.PROJ, always_xy=True)
    xw, yw = tr.transform(lon_w.ravel(), lat_w.ravel())
    xr, yr = tr.transform(lon_r.ravel(), lat_r.ravel())
    return np.isfinite(griddata((xw, yw), np.ones(xw.size), (xr, yr), method="linear").reshape(lat_r.shape))


def nome(p, v):
    base = {"ecmwf": "ECMWF", "icon2i": "ICON-2I", "iconeu": "ICON-EU", "gfs": "GFS"}[p]
    return base + (f" + {v}" if v else " (ufficiale)")


def disegna(acc, bias, zona, ncicli, out):
    famiglie = [("Famiglia ECMWF", lambda p: p == "ecmwf"), ("Famiglia ICON-2I", lambda p: p == "icon2i"),
                ("ICON-EU e GFS (prove) con gli ufficiali", lambda p: p in ("iconeu", "gfs"))]
    cm = plt.get_cmap("tab10")
    fig, assi = plt.subplots(3, 3, figsize=(18, 13))
    for col, (titolo, chi) in enumerate(famiglie):
        chiavi = sorted([k for k in acc if chi(k[0]) or (col == 2 and k[1] == "" and k[0] in ("ecmwf", "icon2i"))],
                        key=lambda k: (k[1] != "", k[0], k[1]))
        for i, k in enumerate(chiavi):
            ufficiale = k[1] == ""
            colore = {"ecmwf": "#264653", "icon2i": "#6a4c93"}[k[0]] if ufficiale and k[0] in ("ecmwf", "icon2i") else cm(i % 10)
            for riga, s in enumerate(SOGLIE):
                bs = sorted({b for (b, ss) in acc[k] if ss == s})
                y = [1 - acc[k][(b, s)][0] / max(acc[k][(b, s)][1], 1e-9) for b in bs]
                assi[riga][col].plot([b * BIN + (BIN + 1) / 2 for b in bs], y, color=colore, lw=3 if ufficiale else 1.4,
                                     marker="o" if ufficiale else None, ms=4, label=nome(*k))
            bs = sorted(bias[k])
            assi[2][col].plot([b * BIN + (BIN + 1) / 2 for b in bs], [bias[k][b][0] / max(bias[k][b][1], 1e-9) for b in bs],
                              color=colore, lw=3 if ufficiale else 1.4)
        assi[0][col].set_title(titolo, fontsize=11)
        assi[0][col].legend(fontsize=7, loc="upper right")
        for riga in range(3):
            assi[riga][col].grid(alpha=.3)
            assi[riga][col].set_xlabel("ore di previsione")
        assi[2][col].axhline(1, color="k", lw=.8)
    assi[0][0].set_ylabel("FSS 20 km, ≥ 0,5 mm/h (1 = perfetto)")
    assi[1][0].set_ylabel("FSS 20 km, ≥ 2 mm/h")
    assi[2][0].set_ylabel("pioggia prevista / osservata")
    for riga in range(2):
        for col in range(3):
            assi[riga][col].set_ylim(0, 1)
    assi[2][0].set_ylim(0, 3)
    for col in (1, 2):
        assi[2][col].set_ylim(0, 3)
    fig.suptitle(f"Tutte le versioni di WRF contro il radar - zona {zona} - {ncicli} cicli (linea spessa = versione ufficiale)", fontsize=13)
    fig.tight_layout()
    fig.savefig(out, dpi=110)


if __name__ == "__main__":
    main()
