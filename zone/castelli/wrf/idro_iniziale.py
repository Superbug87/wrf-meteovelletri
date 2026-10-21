#!/usr/bin/env python3
"""idro_iniziale.py - nubi, ghiaccio e pioggia nelle condizioni iniziali dei run ECMWF, ricostruiti dai totali di colonna.

Perche': i dati aperti di ECMWF non hanno acqua di nube, ghiaccio e pioggia sui livelli in quota, quindi real.exe scrive
wrfinput_d0* con quei campi a zero e WRF parte "a secco": nel caso del 20/10/2026 12Z la pioggia sul Lazio arrivava 1-2
ore dopo quella di ECMWF stesso. ECMWF da' pero' i totali sull'intera colonna d'aria (passo 0):
  tclw = acqua di nube liquida, tciw = ghiaccio, tcrw = pioggia  (kg/m2)
Come (solo dati ECMWF, nessun altro modello):
  - i tre totali si interpolano sui punti di ogni dominio (bilineare dalla griglia di 0,25 gradi);
  - l'acqua di nube va nei livelli con umidita' relativa (rispetto all'acqua) sopra l'85% e temperatura sopra -25 C,
    il ghiaccio dove l'umidita' rispetto al ghiaccio supera l'80% e la temperatura e' sotto 0 C, in proporzione a
    quanto si e' vicini alla saturazione; se nessun livello e' abbastanza umido si usano i livelli piu' umidi;
  - la pioggia va, uniforme, dalla base fino alla cima della nube liquida dove la temperatura e' sopra 0 C
    (senza nube: nei primi 2,5 km sopra il suolo);
  - la quantita' in ogni colonna e' quella di ECMWF: somma di q x massa d'aria dei livelli = totale di colonna
    (salvo i tetti di sicurezza sotto); il vapore non si tocca;
  - Thompson vuole anche il numero di gocce e di cristalli (QNRAIN, QNICE): si calcolano dalla massa come fa
    real.exe (pioggia: Marshall-Palmer; ghiaccio: dimensione media che cresce con la temperatura).
Se i campi di acqua in wrfinput non sono a zero (dati con acqua propria) non tocca nulla.
Uso:  python3 idro_iniziale.py --run CARTELLA_DI_REAL --ciclo AAAAMMGGHH
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent))

G = 9.81
RD, CP = 287.04, 1004.5
CAMPI = {"tclw": "QCLOUD", "tciw": "QICE", "tcrw": "QRAIN"}
TETTO = {"QCLOUD": 3e-3, "QICE": 2e-3, "QRAIN": 5e-3}     # kg/kg: oltre questi valori una ricostruzione non e' credibile
MINIMO = 0.005                                             # kg/m2: colonne con meno acqua di cosi' restano asciutte


def colonne(ciclo):
    """I tre totali di colonna di ECMWF al passo 0: {param: matrice}, lat (gradi) e lon (gradi) della griglia."""
    import eccodes as ec
    import scarica_ecmwf as S
    base = S.nome(ciclo, 0)
    righe = [json.loads(r) for r in S.get(base + ".index").decode().splitlines() if r.strip()]
    out, griglia = {}, None
    for r in righe:
        if r["levtype"] != "sfc" or r["param"] not in CAMPI:
            continue
        msg = S.get(base + ".grib2", (r["_offset"], r["_offset"] + r["_length"] - 1))
        h = ec.codes_new_from_message(msg)
        try:
            ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
            la1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees")
            lo1 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
            dla = ec.codes_get(h, "jDirectionIncrementInDegrees")
            dlo = ec.codes_get(h, "iDirectionIncrementInDegrees")
            su = ec.codes_get(h, "jScansPositively")
            v = ec.codes_get_values(h).reshape(nj, ni)
            mancante = ec.codes_get(h, "missingValue")
        finally:
            ec.codes_release(h)
        v = np.where((v == mancante) | ~np.isfinite(v) | (v < 0), 0.0, v)
        out[r["param"]] = v
        griglia = (la1, dla if su else -dla, lo1, dlo, ni, nj)
    if set(out) != set(CAMPI):
        raise RuntimeError(f"nell'indice di ECMWF mancano {sorted(set(CAMPI) - set(out))}")
    return out, griglia


def interpola(v, griglia, lat, lon):
    """Bilineare dalla griglia regolare globale ai punti (lat, lon) di WRF."""
    la1, dla, lo1, dlo, ni, nj = griglia
    fy = (lat - la1) / dla
    fx = ((lon - lo1) % 360.0) / dlo
    j0 = np.clip(np.floor(fy).astype(int), 0, nj - 2)
    i0 = np.floor(fx).astype(int) % ni
    i1 = (i0 + 1) % ni
    wy, wx = np.clip(fy - j0, 0, 1), fx - np.floor(fx)
    return ((1 - wy) * (1 - wx) * v[j0, i0] + (1 - wy) * wx * v[j0, i1]
            + wy * (1 - wx) * v[j0 + 1, i0] + wy * wx * v[j0 + 1, i1])


def saturazione(temp, p, ghiaccio=False):
    """Rapporto di mescolanza di saturazione (kg/kg) rispetto all'acqua o al ghiaccio."""
    if ghiaccio:
        es = 611.2 * np.exp(22.46 * (temp - 273.15) / (temp - 0.53))
    else:
        es = 611.2 * np.exp(17.67 * (temp - 273.15) / (temp - 29.65))
    return 0.622 * es / np.maximum(p - es, 1.0)


def pesi(ammessi, rh, soglia):
    """Peso di ogni livello: 0 sotto la soglia di umidita', 1 a saturazione. Colonne senza livelli sopra soglia:
    si usano i livelli ammessi piu' umidi (rh^10), cosi' l'acqua di ECMWF non va persa."""
    w = np.clip((rh - soglia) / (1.0 - soglia), 0.0, 1.0) * ammessi
    riserva = np.clip(rh, 0.0, 1.2) ** 10 * ammessi
    return np.where(w.sum(axis=0)[None] > 0, w, riserva)


def distribuisci(totale, w, dm, tetto):
    """q (kg/kg) con somma(q * dm) = totale (kg/m2) in ogni colonna, poi il tetto di sicurezza."""
    den = (w * dm).sum(axis=0)
    ok = (totale >= MINIMO) & (den > 0)
    q = np.where(ok[None], w * (totale / np.where(den > 0, den, 1.0))[None], 0.0)
    return np.minimum(q, tetto)


def numero_pioggia(qr, rho, temp):
    """Come make_RainNumber di Thompson: N0 di Marshall-Palmer (piu' alto sotto 0 C). Ritorna numero per kg."""
    am_r = np.pi * 1000.0 / 6.0
    n0 = np.where(temp <= 271.15, 8e8, np.where(temp < 273.15, 8.0 * 10.0 ** np.clip(279.15 - temp, 6.0, 8.0), 8e6))
    qv = qr * rho
    lam = np.sqrt(np.sqrt(n0 * am_r * 6.0 / np.where(qv > 0, qv, 1.0)))
    return np.where(qv > 0, n0 / lam / rho, 0.0)


def numero_ghiaccio(qi, rho, temp):
    """Come make_IceNumber di Thompson, con un raggio efficace che va da 15 micron (-60 C) a 60 micron (0 C)."""
    re = 15e-6 + 45e-6 * np.clip((temp - 213.15) / 60.0, 0.0, 1.0)
    lam = 3.0 / (2.0 * re)
    return np.where(qi > 0, qi * rho * lam ** 3 / (np.pi * 890.0) / rho, 0.0)


def un_dominio(path, col, griglia):
    with Dataset(path, "r+") as nc:
        for nome in ("QCLOUD", "QRAIN", "QICE", "QNRAIN", "QNICE"):
            if nome not in nc.variables:
                raise RuntimeError(f"{path.name}: manca {nome} (microfisica diversa da Thompson?)")
        if max(float(np.max(nc[n][0])) for n in ("QCLOUD", "QRAIN", "QICE", "QSNOW", "QGRAUP") if n in nc.variables) > 1e-8:
            return f"{path.name}: c'e' gia' acqua nelle condizioni iniziali, non tocco nulla"
        lat, lon = np.array(nc["XLAT"][0]), np.array(nc["XLONG"][0])
        theta = np.array(nc["T"][0]) + 300.0
        p = np.array(nc["P"][0]) + np.array(nc["PB"][0])
        qv = np.array(nc["QVAPOR"][0])
        temp = theta * (p / 1e5) ** (RD / CP)
        mu = np.array(nc["MU"][0]) + np.array(nc["MUB"][0])
        c3f, c4f = np.array(nc["C3F"][0]), np.array(nc["C4F"][0])
        ptop = float(nc["P_TOP"][0])
        pw = c3f[:, None, None] * mu[None] + c4f[:, None, None] + ptop        # pressione secca ai livelli w
        dm = (pw[:-1] - pw[1:]) / G                                             # massa di aria secca per livello (kg/m2)
        if np.any(dm <= 0):
            raise RuntimeError(f"{path.name}: massa d'aria dei livelli non positiva, calcolo non affidabile")
        zw = (np.array(nc["PH"][0]) + np.array(nc["PHB"][0])) / G
        quota = 0.5 * (zw[:-1] + zw[1:]) - np.array(nc["HGT"][0])[None]          # metri sopra il suolo
        rho = p / (RD * temp * (1.0 + 0.608 * qv))

        tot = {CAMPI[k]: interpola(v, griglia, lat, lon) for k, v in col.items()}
        rh_l, rh_i = qv / saturazione(temp, p), qv / saturazione(temp, p, ghiaccio=True)
        qc = distribuisci(tot["QCLOUD"], pesi(temp > 248.15, rh_l, 0.85), dm, TETTO["QCLOUD"])
        qi = distribuisci(tot["QICE"], pesi(temp < 273.15, rh_i, 0.80), dm, TETTO["QICE"])
        # pioggia: dal suolo fino alla cima della nube liquida, solo sopra 0 C; senza nube nei primi 2,5 km
        nz = qc.shape[0]
        k = np.arange(nz)[:, None, None]
        con_nube = qc > 1e-7
        cima = np.where(con_nube.any(axis=0), nz - 1 - np.argmax(con_nube[::-1], axis=0), -1)
        sotto = np.where(cima[None] >= 0, k <= cima[None], quota < 2500.0)
        qr = distribuisci(tot["QRAIN"], ((temp > 273.15) & sotto).astype(float), dm, TETTO["QRAIN"])

        nc["QCLOUD"][0] = qc.astype(np.float32)
        nc["QICE"][0] = qi.astype(np.float32)
        nc["QRAIN"][0] = qr.astype(np.float32)
        nc["QNRAIN"][0] = numero_pioggia(qr, rho, temp).astype(np.float32)
        nc["QNICE"][0] = numero_ghiaccio(qi, rho, temp).astype(np.float32)

        righe = []
        for nome, q in (("QCLOUD", qc), ("QICE", qi), ("QRAIN", qr)):
            messa = (q * dm).sum(axis=0)
            voluta = np.where(tot[nome] >= MINIMO, tot[nome], 0.0)
            quota_messa = messa.sum() / voluta.sum() if voluta.sum() > 0 else 1.0
            righe.append(f"{nome} media {voluta.mean():.3f} kg/m2, messa {100 * quota_messa:.0f}%, "
                         f"max {1000 * q.max():.2f} g/kg, punti {100 * (q > 1e-7).any(axis=0).mean():.0f}%")
        return f"{path.name}: " + " | ".join(righe)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", default=".", help="cartella con wrfinput_d0*")
    ap.add_argument("--ciclo", required=True, help="AAAAMMGGHH del run ECMWF")
    a = ap.parse_args()
    col, griglia = colonne(a.ciclo)
    file = sorted(Path(a.run).glob("wrfinput_d0?"))
    if not file:
        sys.exit("nessun wrfinput_d0* nella cartella")
    for f in file:
        print(un_dominio(f, col, griglia))


if __name__ == "__main__":
    main()
