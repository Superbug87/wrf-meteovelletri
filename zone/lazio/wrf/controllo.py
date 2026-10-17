#!/usr/bin/env python3
"""Controllo di plausibilità delle uscite di WRF prima di fare le mappe e pubblicarle.

  controllo.py --uscite CARTELLA [--rapporto controllo.json]

Legge tutti i wrfout_d03_* (anche in sottocartelle) e cerca i segni di un run andato storto. Se ne trova
anche uno, esce con codice 1: il workflow si ferma, le mappe non si pubblicano e sul sito resta la previsione
precedente. Le soglie sono larghe di proposito: devono fermare i run sbagliati, non i fenomeni forti veri.

Controlli:
  - terreno a quota 0 sulla terraferma nella cornice del dominio (il guaio del 17/10/2026 con lightning_option);
  - valori mancanti o infiniti;
  - temperatura a 2 m fuori da -45 / +50 °C;
  - vento medio a 10 m sopra 150 km/h;
  - corrente verticale sopra 40 m/s;
  - pioggia oraria sopra 120 mm o pioggia totale sopra 500 mm;
  - salto della pressione al suolo media del dominio sopra 4 hPa in un'ora.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
from netCDF4 import Dataset

CORNICE = 5                 # celle: zona di raccordo con il dominio padre
SOGLIE = {
    "t2_min_c": -45.0, "t2_max_c": 50.0,
    "vento10_kmh": 150.0,
    "w_ms": 40.0,
    "pioggia_oraria_mm": 120.0,
    "pioggia_totale_mm": 500.0,
    "salto_psfc_hpa": 4.0,
    "cornice_quota_zero": 0.05,          # frazione delle celle di terra della cornice con quota 0
}


def tempi(ds):
    return ["".join(x.astype(str)) for x in ds["Times"][:]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uscite", required=True)
    ap.add_argument("--rapporto", default=None)
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.uscite, "**", "wrfout_d03_*"), recursive=True))
    if not files:
        print("nessun wrfout_d03 da controllare")
        sys.exit(1)

    problemi, visti = [], {}
    pioggia_prec = None
    psfc_prec = None
    massimi = {"vento10_kmh": 0.0, "w_ms": 0.0, "pioggia_oraria_mm": 0.0, "pioggia_totale_mm": 0.0, "salto_psfc_hpa": 0.0,
               "t2_min_c": 99.0, "t2_max_c": -99.0}
    cornice_fatta = False
    for f in files:
        with Dataset(f) as ds:
            if not cornice_fatta:
                hgt = np.asarray(ds["HGT"][0], float)
                if "LANDMASK" in ds.variables:
                    terra = np.asarray(ds["LANDMASK"][0]) > 0.5
                elif "XLAND" in ds.variables:
                    terra = np.asarray(ds["XLAND"][0]) < 1.5
                else:                                   # file senza maschera terra/mare (solo nei collaudi): niente controllo
                    terra = np.zeros(hgt.shape, bool)
                    print("  (manca LANDMASK: controllo della cornice saltato)")
                bordo = np.zeros(hgt.shape, bool)
                bordo[:CORNICE] = bordo[-CORNICE:] = True
                bordo[:, :CORNICE] = bordo[:, -CORNICE:] = True
                celle = (bordo & terra).sum()
                zero = (bordo & terra & (hgt <= 0.5)).sum()
                frazione = zero / celle if celle else 0.0
                massimi["cornice_quota_zero"] = float(frazione)
                if frazione > SOGLIE["cornice_quota_zero"]:
                    problemi.append(f"terreno a quota 0 in {zero} celle di terra su {celle} della cornice")
                cornice_fatta = True
            for i, t in enumerate(tempi(ds)):
                if t in visti:
                    continue
                visti[t] = True
                t2 = np.asarray(ds["T2"][i], float) - 273.15
                u, v = np.asarray(ds["U10"][i], float), np.asarray(ds["V10"][i], float)
                psfc = np.asarray(ds["PSFC"][i], float) / 100.0
                pioggia = np.asarray(ds["RAINNC"][i], float)
                if "RAINC" in ds.variables:
                    pioggia = pioggia + np.asarray(ds["RAINC"][i], float)
                w = np.asarray(ds["W"][i], float) if "W" in ds.variables else np.zeros(1)   # W manca solo nei collaudi
                campi = {"T2": t2, "U10": u, "V10": v, "PSFC": psfc, "pioggia": pioggia, "W": w}
                for nome, c in campi.items():
                    if not np.isfinite(c).all():
                        problemi.append(f"{t}: valori non validi in {nome}")
                vento = np.hypot(u, v) * 3.6
                massimi["vento10_kmh"] = max(massimi["vento10_kmh"], float(np.nanmax(vento)))
                massimi["w_ms"] = max(massimi["w_ms"], float(np.nanmax(np.abs(w))))
                massimi["t2_min_c"] = min(massimi["t2_min_c"], float(np.nanmin(t2)))
                massimi["t2_max_c"] = max(massimi["t2_max_c"], float(np.nanmax(t2)))
                massimi["pioggia_totale_mm"] = max(massimi["pioggia_totale_mm"], float(np.nanmax(pioggia)))
                if pioggia_prec is not None:
                    massimi["pioggia_oraria_mm"] = max(massimi["pioggia_oraria_mm"], float(np.nanmax(pioggia - pioggia_prec)))
                if psfc_prec is not None:
                    massimi["salto_psfc_hpa"] = max(massimi["salto_psfc_hpa"], abs(float(np.nanmean(psfc)) - psfc_prec))
                pioggia_prec, psfc_prec = pioggia, float(np.nanmean(psfc))

    if massimi["t2_min_c"] < SOGLIE["t2_min_c"] or massimi["t2_max_c"] > SOGLIE["t2_max_c"]:
        problemi.append(f"temperatura a 2 m fuori scala: {massimi['t2_min_c']:.1f} / {massimi['t2_max_c']:.1f} °C")
    for chiave, testo in (("vento10_kmh", "vento medio a 10 m {:.0f} km/h"), ("w_ms", "corrente verticale {:.1f} m/s"),
                          ("pioggia_oraria_mm", "pioggia in un'ora {:.0f} mm"), ("pioggia_totale_mm", "pioggia totale {:.0f} mm"),
                          ("salto_psfc_hpa", "salto della pressione media {:.1f} hPa in un'ora")):
        if massimi[chiave] > SOGLIE[chiave]:
            problemi.append(testo.format(massimi[chiave]) + f" (soglia {SOGLIE[chiave]:g})")

    print(f"Controllati {len(visti)} istanti in {len(files)} file.")
    for k, v in massimi.items():
        print(f"  {k}: {v:.2f}  (soglia {SOGLIE[k]:g})")
    if a.rapporto:
        json.dump({"istanti": len(visti), "massimi": massimi, "soglie": SOGLIE, "problemi": problemi},
                  open(a.rapporto, "w"), indent=1)
    if problemi:
        print("!! RUN NON PLAUSIBILE: le mappe non si pubblicano.")
        for p in problemi[:30]:
            print("   -", p)
        sys.exit(1)
    print("Uscite plausibili.")


if __name__ == "__main__":
    main()
