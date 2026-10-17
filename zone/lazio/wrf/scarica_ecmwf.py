#!/usr/bin/env python3
"""Scarica i dati aperti di ECMWF (IFS, 0,25°, licenza CC-BY 4.0) che servono a WRF come condizioni
iniziali e al contorno, e li prepara per WPS.

  scarica_ecmwf.py --ore 48 --solo-ciclo               stampa l'ultimo ciclo completo (AAAAMMGGHH)
  scarica_ecmwf.py --ciclo 2026101600 --ore 48 --out ecmwf

Per ogni ora di previsione (0, 3, 6 ... 48) scarica solo i campi che servono (circa 40 MB invece dei
140 MB del file completo), li ritaglia su Mediterraneo ed Europa e li riscrive con la compressione semplice di GRIB2:
quella dei dati ECMWF (CCSDS) non la legge ungrib. Scrive ecmwf/ifs.tHHz.fXXX.grib2.

I cicli completi sono solo quelli delle 00 e delle 12 UTC (arrivano circa 7 ore e mezza dopo).
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASI = [  # stessi file su tre server: se uno è lento o rifiuta le richieste si passa al successivo
    "https://storage.googleapis.com/ecmwf-open-data",
    "https://data.ecmwf.int/forecasts",
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
]

# campi usati da WPS (vedi Vtable.ECMWF_OD)
PL = {"gh", "t", "u", "v", "r"}
SFC = {"2t", "2d", "10u", "10v", "msl", "sp", "skt", "lsm", "sd", "z"}
SOL = {"sot", "vsw"}

# strati di suolo di ECMWF (numerati 1-4) con il loro spessore in cm: WPS li riconosce per profondità
STRATI_CM = {1: (0, 7), 2: (7, 28), 3: (28, 100), 4: (100, 289)}

# ritaglio: abbondante rispetto al dominio più grande (1296 km attorno a 41,82 N 12,72 E: 35,75-47,64 N, 4,14-21,30 E);
# resta a est del meridiano 0 così la griglia non lo attraversa
LAT_MIN, LAT_MAX = 30.0, 54.0
LON_MIN, LON_MAX = 0.0, 32.0


def nome(ciclo, h):
    return f"{ciclo[:8]}/{ciclo[8:]}z/ifs/0p25/oper/{ciclo}0000-{h}h-oper-fc"


def esiste(ciclo, h):
    for base in BASI[:2]:  # i primi due bastano per sapere se il ciclo è pubblicato
        try:
            if requests.head(f"{base}/{nome(ciclo, h)}.index", timeout=30).status_code == 200:
                return True
        except requests.RequestException:
            pass
    return False


def ultimo_ciclo(ore):
    """Il ciclo più recente (00 o 12 UTC) di cui esiste già l'ultima ora che serve."""
    adesso = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    c = adesso.replace(hour=12 if adesso.hour >= 12 else 0)
    for _ in range(6):
        testo = c.strftime("%Y%m%d%H")
        if esiste(testo, ore):
            return testo
        c -= timedelta(hours=12)
    sys.exit("Nessun ciclo ECMWF completo trovato nelle ultime 72 ore.")


def get(percorso, intervallo=None, tentativi=6):
    """GET con ripetizioni; prova i tre server a rotazione."""
    ultimo = None
    for i in range(tentativi):
        base = BASI[i % len(BASI)]
        try:
            h = {"Range": f"bytes={intervallo[0]}-{intervallo[1]}"} if intervallo else {}
            r = requests.get(f"{base}/{percorso}", headers=h, timeout=120)
            if r.status_code in (200, 206):
                if intervallo and len(r.content) != intervallo[1] - intervallo[0] + 1:
                    raise IOError(f"{len(r.content)} byte invece di {intervallo[1] - intervallo[0] + 1}")
                return r.content
            ultimo = f"{base}: HTTP {r.status_code}"
        except (requests.RequestException, IOError) as e:
            ultimo = f"{base}: {e}"
        print(f"{percorso.rsplit('/', 1)[-1]}: {ultimo}; riprovo ({i + 1}/{tentativi})", file=sys.stderr)
        time.sleep(5 * (i + 1))
    sys.exit(f"Impossibile scaricare {percorso} ({ultimo})")


def voci(ciclo, h, h0_per_z=True):
    """Le voci dell'indice che servono, in ordine di posizione nel file."""
    indice = get(nome(ciclo, h) + ".index").decode().splitlines()
    righe = [json.loads(r) for r in indice if r.strip()]
    scelte = []
    for r in righe:
        p, t = r["param"], r["levtype"]
        if (t == "pl" and p in PL) or (t == "sfc" and p in SFC) or (t == "sol" and p in SOL):
            scelte.append(r)
    scelte.sort(key=lambda r: r["_offset"])
    return scelte


def intervalli(scelte):
    """Unisce le voci vicine in un'unica richiesta."""
    out = []
    for r in scelte:
        a, b = r["_offset"], r["_offset"] + r["_length"] - 1
        if out and a == out[-1][1] + 1:
            out[-1][1] = b
        else:
            out.append([a, b])
    return out


def scarica_passo(ciclo, h):
    """Il GRIB2 grezzo (compressione CCSDS, globale) con i soli campi che servono."""
    scelte = voci(ciclo, h)
    attesi = len(PL) * 14 + len(SFC) + len(SOL) * 4 - (0 if h == 0 else 1)   # z solo all'ora 0
    if len(scelte) < attesi - 4:
        sys.exit(f"ora +{h}: nell'indice ci sono {len(scelte)} campi su {attesi} attesi; il ciclo non è completo?")
    pezzi = [get(nome(ciclo, h) + ".grib2", (a, b)) for a, b in intervalli(scelte)]
    return b"".join(pezzi)


def ritaglia_e_riscrivi(grezzo, dest, z=None):
    """Ritaglia l'area e riscrive con compressione semplice. Ritorna il messaggio dell'orografia (se c'è)."""
    import eccodes as ec
    import numpy as np

    z_trovato = None
    scritti = 0
    with open(dest, "wb") as out:
        for h in messaggi(grezzo):
            ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
            la1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees")
            lo1 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
            di = ec.codes_get(h, "iDirectionIncrementInDegrees")
            dj = ec.codes_get(h, "jDirectionIncrementInDegrees")
            vals = ec.codes_get_values(h).reshape(nj, ni)
            lats = la1 - dj * np.arange(nj)                      # da nord a sud
            lons = ((lo1 + di * np.arange(ni) + 180.0) % 360.0) - 180.0
            if lons[0] > lons[-1]:                               # la griglia parte da 0° est: si riordina
                ordine = np.argsort(lons)
                lons, vals = lons[ordine], vals[:, ordine]
            ij = np.where((lats >= LAT_MIN) & (lats <= LAT_MAX))[0]
            ii = np.where((lons >= LON_MIN) & (lons <= LON_MAX))[0]
            sotto = vals[ij.min():ij.max() + 1, ii.min():ii.max() + 1]
            if ec.codes_get(h, "shortName") == "r":              # la ricompressione può dare -1 o 101 per cento
                sotto = np.clip(sotto, 0.0, 100.0)
            nuovo = ec.codes_clone(h)
            if ec.codes_get_long(h, "typeOfFirstFixedSurface") == 151:    # strato di suolo "per numero"
                alto, basso = STRATI_CM[ec.codes_get_long(h, "scaledValueOfSecondFixedSurface")]
                for quale, cm in (("First", alto), ("Second", basso)):   # profondità sotto la superficie, in cm
                    ec.codes_set_long(nuovo, f"typeOf{quale}FixedSurface", 106)
                    ec.codes_set_long(nuovo, f"scaleFactorOf{quale}FixedSurface", 2)
                    ec.codes_set_long(nuovo, f"scaledValueOf{quale}FixedSurface", cm)
            ec.codes_set(nuovo, "packingType", "grid_simple")
            ec.codes_set(nuovo, "bitsPerValue", 16)
            ec.codes_set(nuovo, "Ni", int(len(ii)))
            ec.codes_set(nuovo, "Nj", int(len(ij)))
            ec.codes_set(nuovo, "latitudeOfFirstGridPointInDegrees", float(lats[ij.min()]))
            ec.codes_set(nuovo, "latitudeOfLastGridPointInDegrees", float(lats[ij.max()]))
            ec.codes_set(nuovo, "longitudeOfFirstGridPointInDegrees", float(lons[ii.min()] % 360.0))
            ec.codes_set(nuovo, "longitudeOfLastGridPointInDegrees", float(lons[ii.max()] % 360.0))
            ec.codes_set_values(nuovo, sotto.ravel().astype(float))
            ec.codes_write(nuovo, out)
            if ec.codes_get(h, "shortName") == "z" and ec.codes_get(h, "typeOfLevel") == "surface":
                z_trovato = nuovo
            else:
                ec.codes_release(nuovo)
            ec.codes_release(h)
            scritti += 1
    return z_trovato, scritti


def messaggi(dati):
    import eccodes as ec

    pos = 0
    while True:
        i = dati.find(b"GRIB", pos)
        if i < 0:
            return
        lung = int.from_bytes(dati[i + 8:i + 16], "big")
        h = ec.codes_new_from_message(dati[i:i + lung])
        yield h
        pos = i + lung


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ciclo", default="", help="AAAAMMGGHH (solo 00 o 12 UTC); vuoto = l'ultimo completo")
    ap.add_argument("--ore", type=int, default=48)
    ap.add_argument("--out", default="ecmwf")
    ap.add_argument("--solo-ciclo", action="store_true")
    a = ap.parse_args()

    ciclo = a.ciclo or ultimo_ciclo(a.ore)
    if a.solo_ciclo:
        print(ciclo)
        return
    if ciclo[8:] not in ("00", "12"):
        sys.exit("I dati aperti di ECMWF a 48 ore e oltre ci sono solo per i cicli delle 00 e delle 12 UTC.")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ore = list(range(0, a.ore + 1, 3))

    def uno(h):
        grezzo = scarica_passo(ciclo, h)
        return h, grezzo

    z_msg = None
    with ThreadPoolExecutor(3) as ex:
        for h, grezzo in ex.map(uno, ore):
            dest = out / f"ifs.t{ciclo[8:]}z.f{h:03d}.grib2"
            zz, n = ritaglia_e_riscrivi(grezzo, dest)
            if zz is not None:
                z_msg = zz
            print(f"ok +{h:02d} h: {n} campi, {dest.stat().st_size / 1e6:.1f} MB", flush=True)
    if z_msg is None:
        sys.exit("Manca l'orografia (campo z alla superficie) all'ora 0.")
    # l'orografia ECMWF c'è solo all'ora 0: la copio nelle altre ore, come se fosse un campo di ogni passo
    import eccodes as ec

    for h in ore[1:]:
        dest = out / f"ifs.t{ciclo[8:]}z.f{h:03d}.grib2"
        copia = ec.codes_clone(z_msg)
        ec.codes_set(copia, "step", h)
        with open(dest, "ab") as f:
            ec.codes_write(copia, f)
        ec.codes_release(copia)


if __name__ == "__main__":
    main()
