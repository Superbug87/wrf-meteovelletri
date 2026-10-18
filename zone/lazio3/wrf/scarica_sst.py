#!/usr/bin/env python3
"""Temperatura superficiale del mare ad alta risoluzione per WRF (facoltativa).

  scarica_sst.py --ciclo 2026101600 --ore 48 --out WPS

Scarica da NOAA CoastWatch ERDDAP l'analisi giornaliera MUR SST (NASA JPL, 0,01 gradi, circa 1 km, qui
diradata a 0,02 gradi, senza registrazione) per il giorno del ciclo o, se non c'e' ancora (arriva con 1-2 giorni di ritardo), per l'ultimo
giorno disponibile, e la scrive nel formato intermedio di WPS come campo SST (file "SST:AAAA-MM-GG_HH", uno
ogni 3 ore, sempre lo stesso valore: sul mare cambia poco in due giorni). metgrid lo prende al posto della
temperatura superficiale del modello globale (a 25 km) sui punti d'acqua; real.exe usa SST solo se e' tra
170 e 400 K, quindi i punti senza dato (terra, vicino alla costa) restano con il valore del modello globale.

Se il servizio non risponde esce con errore e non scrive nulla: WRF gira lo stesso, senza.
"""
import argparse
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

URL = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/jplMURSST41.nc"
# piu' del dominio a 18 km (936 km attorno a 41,82 N 12,72 E: 37,6-46,0 N, 7,0-18,4 E): il riquadro e' rimasto largo
LAT_MIN, LAT_MAX = 35.5, 48.0
LON_MIN, LON_MAX = 4.0, 21.5
PASSO = 2             # si tiene un punto ogni 2 (0,02 gradi, circa 2 km): basta per i 2 km del dominio fine e la richiesta resta leggera
DILATA = 6            # celle di terra riempite col mare piu' vicino, per non lasciare buchi lungo la costa


def scarica_giorno(giorno, tentativi=5):
    """analysed_sst del giorno (AAAA-MM-GG) come matrice (lat, lon) in gradi C, con NaN dove non c'e' dato."""
    import netCDF4

    q = (f"analysed_sst[({giorno}T09:00:00Z)][({LAT_MIN}):{PASSO}:({LAT_MAX})][({LON_MIN}):{PASSO}:({LON_MAX})]")
    ultimo = ""
    for i in range(tentativi):
        try:
            r = requests.get(f"{URL}?{q}", timeout=180)
            if r.status_code == 200 and r.content[:3] in (b"CDF", b"\x89HD"):
                tmp = Path(f"mur_{giorno}.nc")
                tmp.write_bytes(r.content)
                try:
                    with netCDF4.Dataset(tmp) as nc:
                        z = np.ma.filled(nc["analysed_sst"][0].astype("float64"), np.nan)
                        lat = np.array(nc["latitude"][:], dtype="float64")
                        lon = np.array(nc["longitude"][:], dtype="float64")
                finally:
                    tmp.unlink(missing_ok=True)
                return lat, lon, z
            if r.status_code in (400, 404):                    # giorno non (ancora) pubblicato
                return None
            ultimo = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ultimo = str(e)[:100]
        print(f"MUR {giorno}: {ultimo}; riprovo ({i + 1}/{tentativi})", file=sys.stderr)
        time.sleep(10 * (i + 1))
    sys.exit(f"MUR SST non raggiungibile ({ultimo})")


def riempi_costa(z):
    """Dove manca il dato (terra) mette la media dei vicini col dato, a ondate, per al massimo DILATA celle."""
    out = z.copy()
    for _ in range(DILATA):
        manca = ~np.isfinite(out)
        if not manca.any() or manca.all():
            break
        p = np.pad(out, 1, constant_values=np.nan)
        vicini = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        n = np.isfinite(vicini).sum(axis=0)
        somma = np.nansum(vicini, axis=0)
        nuovo = manca & (n > 0)
        out[nuovo] = somma[nuovo] / n[nuovo]
    return out


def scrivi_intermedio(percorso, hdate, lat, lon, kelvin):
    """Formato intermedio di WPS (versione 5), proiezione cilindrica equidistante, campo SST.
    Come lo scrive write_next_met_field di WPS; i record di Fortran hanno il segno di lunghezza di 4 byte,
    qui con l'ordine dei byte "big endian" che WPS usa quando e' compilato con -fconvert=big-endian."""
    ny, nx = kelvin.shape

    def rec(b):
        return struct.pack(">i", len(b)) + b + struct.pack(">i", len(b))

    def txt(s, n):
        return s.ljust(n).encode("ascii")[:n]

    corpo = [
        rec(struct.pack(">i", 5)),
        rec(txt(hdate, 24) + struct.pack(">f", 0.0) + txt("MUR SST JPL via NOAA ERDDAP", 32) + txt("SST", 9)
            + txt("K", 25) + txt("Sea Surface Temperature", 46) + struct.pack(">f", 200100.0)
            + struct.pack(">iii", nx, ny, 0)),
        rec(txt("SWCORNER", 8) + struct.pack(">fffff", float(lat[0]), float(lon[0]),
                                              float((lat[-1] - lat[0]) / (ny - 1)), float((lon[-1] - lon[0]) / (nx - 1)), 6367.470)),
        rec(struct.pack(">i", 0)),                      # i venti non sono rispetto alla griglia (qui non ce ne sono)
        rec(kelvin.astype(">f4").tobytes()),            # nx piu' veloce, righe da sud a nord
    ]
    Path(percorso).write_bytes(b"".join(corpo))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ciclo", required=True, help="AAAAMMGGHH")
    ap.add_argument("--ore", type=int, default=48)
    ap.add_argument("--out", default=".")
    a = ap.parse_args()

    inizio = datetime.strptime(a.ciclo, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    adesso = datetime.now(timezone.utc)
    giorno = min(inizio, adesso).date()
    risultato = None
    for indietro in range(0, 6):                       # il giorno del ciclo, poi i precedenti
        g = (giorno - timedelta(days=indietro)).isoformat()
        risultato = scarica_giorno(g)
        if risultato is not None:
            break
    if risultato is None:
        sys.exit("Nessun giorno MUR SST disponibile negli ultimi 6 giorni.")
    lat, lon, z = risultato
    mare = np.isfinite(z)
    if mare.sum() < 1000 or not (5 < np.nanmean(z) < 35):
        sys.exit(f"MUR SST: dati poco credibili ({int(mare.sum())} punti di mare, media {np.nanmean(z):.1f} C)")
    z = riempi_costa(z)
    k = np.where(np.isfinite(z), z + 273.15, -1.0e30)    # -1e30 e' il valore mancante di metgrid
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for h in range(0, a.ore + 1, 3):
        t = inizio + timedelta(hours=h)
        nome = out / f"SST:{t:%Y-%m-%d_%H}"
        scrivi_intermedio(nome, f"{t:%Y-%m-%d_%H}:00:00", lat, lon, k)
        n += 1
    print(f"MUR SST del {g}: {mare.sum()} punti di mare, mare {np.nanmin(z):.1f}-{np.nanmax(z):.1f} C; "
          f"scritti {n} file SST:* ({z.shape[1]}x{z.shape[0]} punti)")


if __name__ == "__main__":
    main()
