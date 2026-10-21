#!/usr/bin/env python3
"""Mappe di concordanza della pioggia tra le versioni WRF dello stesso ciclo.

  probabilita.py --zona lazio --ciclo 2026102012 --estratti CARTELLA --out CARTELLA_PNG

Ogni ciclo ha piu' versioni della stessa previsione: ECMWF e ICON-2I (ufficiali) e le loro varianti di fisica (vento sul terreno,
MYNN, Noah-MP, modello urbano, ...), piu' ICON-EU e GFS in prova. Per ogni punto e finestra di 3 ore si conta la frazione di
versioni che prevede almeno la soglia di pioggia oraria (1 e 5 mm/h) entro 1 cella di griglia (circa 6 km sul Lazio, 2 km sui
Castelli) e +-1 ora: e' la tolleranza che serve contro il ritardo e lo spostamento delle bande.

ATTENZIONE: non e' una probabilita' calibrata. Le versioni partono dagli stessi dati e quindi tendono ad assomigliarsi piu' di
quanto farebbero previsioni davvero indipendenti: "9 versioni su 10" non vuol dire "90% di probabilita'". Si chiama concordanza.
ECMWF e ICON-2I restano distinti: ci sono mappe separate per ciascuna famiglia e una con tutte.

I file letti sono gli estratti per la verifica (estratto_<zona>-<pilota>[-<variante>]_<ciclo>.npz, vedi estrai_verifica.py).
"""
import argparse
import glob
import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap

QUI = os.path.dirname(os.path.abspath(__file__))
SOGLIE = (1.0, 5.0)                              # mm in un'ora
FINESTRA = 3                                     # ore per riquadro
# varianti ammesse tra le versioni (le prove una tantum, come idroec e cu27, non fanno parte della concordanza)
VARIANTI = {"topo2", "mynn", "noahmp", "mynn-noahmp", "radt6", "radt15", "urbano"}
NOMI = {"ecmwf": "ECMWF", "icon2i": "ICON-2I", "iconeu": "ICON-EU", "gfs": "GFS"}
COLORI = ["#ffffff00", "#dbe9f6", "#a6cbe8", "#6aa5d6", "#2f7dbd", "#f2c14e", "#e8833a", "#c93a2b"]   # 0-100%


def carica_membri(cartella, zona, ciclo):
    """{(pilota, variante): estratto} per le versioni dello stesso ciclo che hanno la griglia di pioggia."""
    membri = {}
    for f in sorted(glob.glob(os.path.join(cartella, f"estratto_{zona}-*_{ciclo}.npz"))):
        m = re.match(rf"estratto_{zona}-(ecmwf|icon2i|iconeu|gfs)(?:-(.+))?_{ciclo}\.npz$", os.path.basename(f))
        if not m:
            continue
        pilota, var = m.group(1), m.group(2) or ""
        if var and var not in VARIANTI:
            continue
        d = np.load(f, allow_pickle=True)
        if "griglia_pioggia" not in d.files:
            continue
        membri[(pilota, var)] = {"pioggia": np.asarray(d["griglia_pioggia"], np.float32), "ore": np.asarray(d["ore"]),
                                 "lat": np.asarray(d["griglia_lat"], float), "lon": np.asarray(d["griglia_lon"], float)}
    return membri


def ultimo_ciclo(cartella, zona, minimo=4):
    """Il ciclo piu' recente con almeno `minimo` versioni (altrimenti le mappe sarebbero fatte su una o due sole previsioni)."""
    conta = {}
    for f in glob.glob(os.path.join(cartella, f"estratto_{zona}-*.npz")):
        m = re.match(rf"estratto_{zona}-(ecmwf|icon2i|iconeu|gfs)(?:-(.+))?_(\d{{10}})\.npz$", os.path.basename(f))
        if m and (not m.group(2) or m.group(2) in VARIANTI):
            conta[m.group(3)] = conta.get(m.group(3), 0) + 1
    ok = [c for c, n in conta.items() if n >= minimo]
    return max(ok) if ok else None


def supera(pioggia, soglia):
    """Booleano [ora, y, x]: la pioggia oraria supera la soglia in questa cella o nelle 8 vicine, in questa ora o nelle due vicine."""
    b = np.nan_to_num(pioggia, nan=0.0) >= soglia
    p = np.pad(b, ((1, 1), (1, 1), (1, 1)))
    o = np.zeros_like(b)
    for dt in range(3):
        for dy in range(3):
            for dx in range(3):
                o |= p[dt:dt + b.shape[0], dy:dy + b.shape[1], dx:dx + b.shape[2]]
    return o


def concordanza(membri, chi, soglia):
    """Frazione di versioni (di quelle scelte da `chi`) che superano la soglia: [ora, y, x], e quante sono."""
    scelti = [m for k, m in membri.items() if chi(k)]
    if not scelti:
        return None, 0
    n = min(m["pioggia"].shape[0] for m in scelti)
    forme = [m["pioggia"].shape[1:] for m in scelti]
    forma = max(set(forme), key=forme.count)          # griglie diverse (cambio di dominio): si tiene la piu' comune
    scelti = [m for m in scelti if m["pioggia"].shape[1:] == forma]
    return np.mean([supera(m["pioggia"][:n], soglia) for m in scelti], axis=0), len(scelti)


def carica_confini_zona(zona):
    """disegna_confini della zona (stessi confini delle mappe normali), oppure None."""
    try:
        p = os.path.join(QUI, "..", "zone", zona, f"previsioni_{zona}.py")
        sys.path.insert(0, os.path.dirname(p))
        spec = importlib.util.spec_from_file_location(f"previsioni_{zona}", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.disegna_confini
    except Exception as e:                                      # i confini sono un in piu'
        print(f"confini non disponibili: {e}")
        return None


def disegna(conc, n, lat, lon, ciclo, titolo, base, soglia, out, confini):
    ore = conc.shape[0]
    finestre = [(h, min(h + FINESTRA, ore)) for h in range(0, ore, FINESTRA)]
    colonne = 4
    righe = int(np.ceil(len(finestre) / colonne))
    fig, assi = plt.subplots(righe, colonne, figsize=(colonne * 3.1, righe * 3.0 + 1.0), squeeze=False)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.10, hspace=0.18, wspace=0.04)
    liv = np.linspace(0, 1, len(COLORI) + 1)
    cmap = ListedColormap(COLORI)
    norma = BoundaryNorm(liv, cmap.N)
    t0 = datetime.strptime(ciclo, "%Y%m%d%H")
    for ax, (a, b) in zip(assi.ravel(), finestre):
        c = conc[a:b].max(axis=0)
        im = ax.pcolormesh(lon, lat, c, cmap=cmap, norm=norma, shading="auto", zorder=2)
        if confini:
            try:
                confini(ax, scala=0.4)
            except Exception:
                pass
        ax.set_xlim(lon.min(), lon.max())
        ax.set_ylim(lat.min(), lat.max())
        ax.set_aspect(1 / np.cos(np.radians(lat.mean())))
        ax.set_xticks([])
        ax.set_yticks([])
        ini = t0 + timedelta(hours=a)
        ax.set_title(f"{ini:%d/%m %H}Z-{(ini + timedelta(hours=b - a)):%H}Z", fontsize=8)
    for ax in assi.ravel()[len(finestre):]:
        ax.axis("off")
    fig.suptitle(f"{titolo}: pioggia oltre {soglia:g} mm/h, concordanza tra {n} versioni", fontsize=11, y=0.985)
    fig.text(0.5, 0.945, "Quota di versioni che la prevedono entro ~1 cella e +-1 ora: non e' una probabilita' calibrata", ha="center", fontsize=8, color="#555")
    cb = fig.colorbar(im, ax=assi, orientation="horizontal", fraction=0.02, pad=0.03, shrink=0.5, ticks=np.linspace(0, 1, 5))
    cb.ax.set_xticklabels(["0", "25%", "50%", "75%", "100%"])
    fig.text(0.5, 0.005, f"WRF by Meteo Velletri · base {base} · ciclo {t0:%d/%m/%Y %H}Z", ha="center", fontsize=8)
    fig.savefig(out, dpi=110)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zona", required=True, choices=["castelli", "lazio"])
    ap.add_argument("--ciclo", required=True, help="AAAAMMGGHH oppure auto (il piu' recente con almeno 4 versioni)")
    ap.add_argument("--estratti", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.ciclo == "auto":
        a.ciclo = ultimo_ciclo(a.estratti, a.zona)
        if not a.ciclo:
            raise SystemExit("nessun ciclo con almeno 4 versioni")
        print("ciclo:", a.ciclo)
    membri = carica_membri(a.estratti, a.zona, a.ciclo)
    if len(membri) < 2:
        raise SystemExit(f"servono almeno 2 versioni dello stesso ciclo, trovate {len(membri)}")
    print("versioni:", ", ".join(f"{NOMI[p]}{'+' + v if v else ''}" for p, v in sorted(membri)))
    os.makedirs(a.out, exist_ok=True)
    forme = [m["pioggia"].shape[1:] for m in membri.values()]
    forma = max(set(forme), key=forme.count)
    k0 = next(k for k, m in membri.items() if m["pioggia"].shape[1:] == forma)
    lat, lon = membri[k0]["lat"], membri[k0]["lon"]
    confini = carica_confini_zona(a.zona)
    gruppi = [("tutte", "Tutte le versioni", "ECMWF + ICON-2I + prove", lambda k: True),
              ("ecmwf", "Versioni ECMWF", "ECMWF", lambda k: k[0] == "ecmwf"),
              ("icon2i", "Versioni ICON-2I", "ICON-2I", lambda k: k[0] == "icon2i")]
    for sigla, titolo, base, chi in gruppi:
        for soglia in SOGLIE:
            if sigla != "tutte" and soglia != SOGLIE[0]:
                continue
            conc, n = concordanza(membri, chi, soglia)
            if conc is None or n < 2:
                continue
            disegna(conc, n, lat, lon, a.ciclo, titolo, base, soglia,
                    os.path.join(a.out, f"concordanza_{a.zona}_{sigla}_{soglia:g}mm_{a.ciclo}.png"), confini)
            print(f"{sigla} {soglia:g} mm/h: {n} versioni")


if __name__ == "__main__":
    main()
