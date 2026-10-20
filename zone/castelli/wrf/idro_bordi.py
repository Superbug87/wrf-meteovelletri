#!/usr/bin/env python3
"""idro_bordi.py - mette nei bordi di WRF l'acqua di nube, pioggia, ghiaccio, neve e graupel del modello di partenza.

Perche': real.exe scrive nel file dei bordi (wrfbdy_d01) il solo vapore; gli altri campi di acqua restano a zero anche
se i dati di partenza (ICON-2I, ICON-EU, GFS) li hanno. Con have_bcs_moist attivo wrf.exe forza quegli zeri nella
fascia di bordo, quindi le perturbazioni entrano senza acqua.
Come: dopo real.exe, rifa' girare real.exe una volta per ogni istante dei bordi con il solo dominio 1 (cosi' i campi
sono gia' sui livelli di WRF), prende le righe e colonne di bordo, le accoppia alla massa dell'aria come fa WRF per il
vapore e le scrive nel file dei bordi, con le tendenze. Come controllo ripete lo stesso calcolo sul vapore e lo
confronta con quello scritto da real.exe: se non coincide NON tocca il file e lascia il segno idro_ko.txt
(config.py allora toglie have_bcs_moist e WRF gira come prima).
Uso:  python3 idro_bordi.py --run CARTELLA_DI_REAL --ciclo AAAAMMGGHH
"""
import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

SPECIE = ("QCLOUD", "QRAIN", "QICE", "QSNOW", "QGRAUP")
LATI = ("BXS", "BXE", "BYS", "BYE")
LARGHEZZA = 5
TOLLERANZA = 1e-4          # errore relativo massimo ammesso sul vapore (il vapore e' scritto in singola precisione)


def bordi(campo):
    """campo (z, y, x) -> i quattro lati come li scrive WRF: (largh, z, y) per BXS/BXE, (largh, z, x) per BYS/BYE."""
    w = LARGHEZZA
    return {"BXS": campo[:, :, :w].transpose(2, 0, 1),
            "BXE": campo[:, :, ::-1][:, :, :w].transpose(2, 0, 1),
            "BYS": campo[:, :w, :].transpose(1, 0, 2),
            "BYE": campo[:, ::-1, :][:, :w, :].transpose(1, 0, 2)}


def riscrivi_namelist(testo, data):
    for nome in ("start", "end"):
        for campo, fmt in (("year", "%Y"), ("month", "%m"), ("day", "%d"), ("hour", "%H"), ("minute", "%M"), ("second", "%S")):
            v = int(data.strftime(fmt))
            testo, n = re.subn(rf"^\s*{nome}_{campo}\s*=.*$", f" {nome}_{campo} = {v}, {v}, {v},", testo, flags=re.M)
            if n != 1:
                raise SystemExit(f"namelist: {nome}_{campo} trovato {n} volte")
    for chiave, valore in (("run_hours", 0), ("max_dom", 1)):
        testo, n = re.subn(rf"(\b{chiave}\s*=\s*)\d+", rf"\g<1>{valore}", testo, count=1)
        if n != 1:
            raise SystemExit(f"namelist: {chiave} non trovato")
    return testo


def prepara_cartella(run, sub):
    sub.mkdir(exist_ok=True)
    for p in run.iterdir():
        if p.name.startswith(("wrfinput", "wrfbdy", "rsl.", "namelist.input", "idro_")) or p.name.endswith(".log"):
            continue
        d = sub / p.name
        if not d.exists():
            d.symlink_to(p.resolve())            # anche i collegamenti relativi (met_em) vanno resi assoluti


def accoppia(d):
    """Il fattore con cui WRF moltiplica le variabili di massa nei bordi: c1h*(mu+mub) + c2h."""
    mu = d["MU"][0] + d["MUB"][0]
    return d["C1H"][0][:, None, None] * mu[None] + d["C2H"][0][:, None, None]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=".")
    ap.add_argument("--ciclo", required=True)
    a = ap.parse_args()
    run = Path(a.run).resolve()
    ko = run / "idro_ko.txt"
    ko.unlink(missing_ok=True)

    def fallisci(msg):
        print(f"!! idro_bordi: {msg}. I bordi restano senza acqua e WRF gira come prima (have_bcs_moist spento).", flush=True)
        ko.write_text(msg + "\n")
        sys.exit(0)

    testo = (run / "namelist.input").read_text()
    m = re.search(r"interval_seconds\s*=\s*(\d+)", testo)
    intervallo = int(m.group(1))
    bdy = Dataset(run / "wrfbdy_d01")
    n = bdy.dimensions["Time"].size                       # intervalli: gli istanti sono n + 1
    t0 = datetime.strptime(a.ciclo, "%Y%m%d%H")
    campi = {}                                            # k -> {nome: (z, y, x) accoppiato}
    for k in range(n + 1):
        data = t0 + timedelta(seconds=k * intervallo)
        sub = run / f"idro_{k}"
        prepara_cartella(run, sub)
        (sub / "namelist.input").write_text(riscrivi_namelist(testo, data))
        r = subprocess.run(["mpirun", "-np", "1", "./real.exe"], cwd=sub, capture_output=True, text=True)
        rsl = sub / "rsl.error.0000"
        if r.returncode != 0 or not rsl.exists() or "SUCCESS COMPLETE" not in rsl.read_text()[-400:]:
            print(r.stdout[-800:], r.stderr[-800:], (rsl.read_text()[-800:] if rsl.exists() else ""))
            fallisci(f"real.exe non riuscito per {data:%Y-%m-%d_%H}")
        d = Dataset(sub / "wrfinput_d01")
        f = accoppia(d)
        campi[k] = {"QVAPOR": np.asarray(d["QVAPOR"][0], float) * f}
        for q in SPECIE:
            if q in d.variables:
                campi[k][q] = np.maximum(np.asarray(d[q][0], float), 0.0) * f
        d.close()

    # controllo: il vapore ricostruito deve coincidere con quello che real.exe ha scritto nei bordi
    peggiore = 0.0
    for k in range(n):
        b0, b1 = bordi(campi[k]["QVAPOR"]), bordi(campi[k + 1]["QVAPOR"])
        for lato in LATI:
            for nome, atteso, calc in ((f"QVAPOR_{lato}", bdy[f"QVAPOR_{lato}"][k], b0[lato]),
                                       (f"QVAPOR_BT{lato[1:]}", bdy[f"QVAPOR_BT{lato[1:]}"][k], (b1[lato] - b0[lato]) / intervallo)):
                atteso = np.asarray(atteso, float)
                err = float(np.abs(atteso - calc).max() / max(float(np.abs(atteso).max()), 1e-30))
                peggiore = max(peggiore, err)
    print(f"idro_bordi: controllo sul vapore, errore relativo massimo {peggiore:.2e} (limite {TOLLERANZA:.0e})", flush=True)
    if not peggiore < TOLLERANZA:
        fallisci(f"il vapore ricostruito non coincide con quello di real.exe (errore {peggiore:.1e})")
    bdy.close()

    # scrittura
    bdy = Dataset(run / "wrfbdy_d01", "r+")
    for q in SPECIE:
        if q not in campi[0]:
            print(f"idro_bordi: {q} assente nei dati: resta zero")
            continue
        massimo = 0.0
        for k in range(n):
            b0, b1 = bordi(campi[k][q]), bordi(campi[k + 1][q])
            for lato in LATI:
                bdy[f"{q}_{lato}"][k] = b0[lato]
                bdy[f"{q}_BT{lato[1:]}"][k] = (b1[lato] - b0[lato]) / intervallo
                massimo = max(massimo, float(np.abs(b0[lato]).max()))
        print(f"idro_bordi: {q} scritto nei bordi (massimo accoppiato {massimo:.2e})", flush=True)
    bdy.close()
    for k in range(n + 1):
        subprocess.run(["rm", "-rf", str(run / f"idro_{k}")])
    print("idro_bordi: fatto")


if __name__ == "__main__":
    main()
