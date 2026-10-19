#!/usr/bin/env python3
"""
scarica_iconeu.py - dati di partenza e di bordo per WRF da ICON-EU (DWD open data, circa 6,5 km, licenza CC BY 4.0).

  scarica_iconeu.py --solo-ciclo --ore 48                  stampa l'ultimo ciclo completo (AAAAMMGGHH)
  scarica_iconeu.py --ciclo 2026101900 --ore 48 --passo 6 --out iconeu

Come scarica_icon2i.py, ma da opendata.dwd.de: un file bz2 per variabile e per livello (60 livelli del modello:
T, p, q, u, v, acqua di nube e ghiaccio), piu' i campi di superficie e di suolo. Ritaglia l'area che serve a WRF,
porta i livelli del modello su livelli di pressione e riscrive in GRIB2 con gli stessi codici dei file ECMWF
(si usa la stessa Vtable e lo stesso resto della catena). Uscita: iconeu.tHHz.fXXX.grib2.

Note:
  - ICON-EU ha cicli 00, 06, 12, 18 UTC (disponibili circa 3 ore e mezza dopo): sono i piu' freschi tra i piloti.
  - Dei idrometeori ICON-EU da' solo acqua di nube e ghiaccio (non pioggia, neve, graupel): quelli entrano a zero.
  - Suolo: come ICON-2I (modello TERRA): le stesse conversioni a strati di Noah.
"""
import argparse
import bz2
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scarica_icon2i as S          # noqa: E402  (funzioni comuni: interpolazione, scrittura, suolo)

BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib"
PREF = "icon-eu_europe_regular-lat-lon"
NZ = 60
STRATI_W_SO_BASSO = [1, 3, 9, 27, 81, 243, 729]            # cm: i file W_SO sono etichettati con il fondo dello strato
PROFONDITA_T_CM = [0, 6, 18, 54, 162, 486, 1458]


def ses():
    s = requests.Session()
    s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32))
    return s


SES = ses()


def url_file(ciclo, var, tipo, ora, livello=None):
    c = f"{ciclo:%Y%m%d%H}"
    parti = [PREF, tipo, c]
    if ora is not None:
        parti.append(f"{ora:03d}")
    if livello is not None:
        parti.append(str(livello))
    parti.append(var.upper())
    return f"{BASE}/{ciclo:%H}/{var}/" + "_".join(parti) + ".grib2.bz2"


def scarica(u, tentativi=6):
    ultimo = None
    for i in range(tentativi):
        try:
            r = SES.get(u, timeout=120)
            if r.status_code == 200:
                return bz2.decompress(r.content)
            ultimo = f"HTTP {r.status_code}"
            if r.status_code == 404 and i >= 1:
                break
        except (requests.RequestException, OSError, EOFError) as e:
            ultimo = str(e)
        time.sleep(3 * (i + 1))
    sys.exit(f"Impossibile scaricare {u} ({ultimo})")


def esiste(u):
    try:
        return SES.head(u, timeout=30).status_code == 200
    except requests.RequestException:
        return False


def ultimo_ciclo(ore):
    adesso = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    c = adesso.replace(hour=adesso.hour - adesso.hour % 6)
    for _ in range(8):
        # l'ultimo file che serve (un campo di superficie e un livello del modello all'ora piu' lontana)
        if esiste(url_file(c, "ps", "single-level", ore)) and esiste(url_file(c, "qi", "model-level", ore, NZ)):
            return f"{c:%Y%m%d%H}"
        c -= timedelta(hours=6)
    sys.exit("Nessun ciclo ICON-EU completo trovato nelle ultime 48 ore.")


class Griglia:
    """Ritaglio e orientamento (da nord a sud, longitudine crescente) dalla prima griglia letta."""
    lats = lons = None
    ij = ii = None


G = Griglia()


def campo(dati, template=False):
    """GRIB2 -> array 2D ritagliato (nord->sud). Con template=True ritorna anche il messaggio da clonare."""
    import eccodes as ec

    h = ec.codes_new_from_message(dati)
    ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
    v = ec.codes_get_values(h).reshape(nj, ni)
    if G.lats is None:
        la1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees")
        lo1 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
        di, dj = ec.codes_get(h, "iDirectionIncrementInDegrees"), ec.codes_get(h, "jDirectionIncrementInDegrees")
        la = la1 + dj * np.arange(nj) if ec.codes_get(h, "jScansPositively") else la1 - dj * np.arange(nj)
        lo = ((lo1 + di * np.arange(ni) + 180.0) % 360.0) - 180.0
        G.ij = np.where((la >= S.LAT_MIN) & (la <= S.LAT_MAX))[0]
        G.ii = np.where((lo >= S.LON_MIN) & (lo <= S.LON_MAX))[0]
        G.lats = la[G.ij.min():G.ij.max() + 1]
        G.dal_basso = bool(ec.codes_get(h, "jScansPositively"))      # la griglia parte da sud: si rovescia
        if G.dal_basso:
            G.lats = G.lats[::-1]
        G.lons = lo[G.ii.min():G.ii.max() + 1]
        G.j0, G.j1, G.i0, G.i1 = G.ij.min(), G.ij.max() + 1, G.ii.min(), G.ii.max() + 1
    sotto = v[G.j0:G.j1, G.i0:G.i1]
    if G.dal_basso:
        sotto = sotto[::-1]
    out = sotto.astype("float32")
    if template:
        return out, h
    ec.codes_release(h)
    return out


def pila(ciclo, ora, var):
    """I 60 livelli del modello di una variabile, dall'alto al basso: (60, ny, nx)."""
    with ThreadPoolExecutor(12) as ex:
        liste = list(ex.map(lambda k: campo(scarica(url_file(ciclo, var, "model-level", ora, k))), range(1, NZ + 1)))
    return np.stack(liste)


def un_istante(ciclo, ora, out, z3, modello, lsm, hsurf):
    T3, Q3, U3, V3, P3 = (pila(ciclo, ora, v) for v in ("t", "qv", "u", "v", "p"))
    H3 = [pila(ciclo, ora, "qc"), np.zeros_like(T3), np.zeros_like(T3), np.zeros_like(T3), pila(ciclo, ora, "qi")]
    # ordine di S.IDROMETEORE: nube, pioggia, neve, graupel, ghiaccio
    sup = lambda var: campo(scarica(url_file(ciclo, var, "single-level", ora)))
    ps, pmsl, t2, td2, u10, v10, skin, neve = (sup(v) for v in ("ps", "pmsl", "t_2m", "td_2m", "u_10m", "v_10m", "t_g", "w_snow"))
    ts = [campo(scarica(url_file(ciclo, "t_so", "soil-level", ora, cm))) for cm in PROFONDITA_T_CM]
    strati_sm = [campo(scarica(url_file(ciclo, "w_so", "soil-level", ora, cm))) for cm in STRATI_W_SO_BASSO]
    liv = [p for p in S.LIVELLI_HPA if p * 100.0 >= P3[0].min() * 0.98]
    iso = S.a_pressione(P3, (T3, Q3, U3, V3), z3, ps, [p * 100.0 for p in liv], H3)
    dest = Path(out) / f"iconeu.t{ciclo:%H}z.f{ora:03d}.grib2"
    with open(dest, "wb") as f:
        for pl in liv:
            T, RH, U, V, Z, H = iso[pl * 100.0]
            for (c, n, val) in ((0, 0, T), (1, 1, RH), (2, 2, U), (2, 3, V), (3, 5, Z)):
                S.scrivi(modello, f, G.lats, G.lons, ciclo, ora, 0, c, n, 100, pl * 100.0, val)
            for ((c, n), _), val in zip(S.IDROMETEORE, H):
                S.scrivi(modello, f, G.lats, G.lons, ciclo, ora, 0, c, {82: 23}.get(n, n), 100, pl * 100.0, val, bits=24)
        W = lambda c, n, t1, v1, val, **k: S.scrivi(modello, f, G.lats, G.lons, ciclo, ora, 0, c, n, t1, v1, val, **k)
        W(2, 2, 103, 10, u10)
        W(2, 3, 103, 10, v10)
        W(0, 0, 103, 2, t2)
        W(0, 6, 103, 2, td2)
        W(3, 0, 1, 0, ps)
        W(3, 0, 101, 0, pmsl)
        W(0, 17, 1, 0, skin)
        S.scrivi(modello, f, G.lats, G.lons, ciclo, ora, 2, 0, 0, 1, 0, (lsm >= 0.5).astype("float32"), bits=8)
        W(1, 254, 1, 0, neve / 1000.0, bits=16)
        W(3, 4, 1, 0, S.G0 * hsurf)                                   # orografia del modello (m2 s-2)
        prof = np.array(PROFONDITA_T_CM, float)
        for (a, b) in S.STRATI_NOAH_CM:
            m = 0.5 * (a + b)
            k = int(np.clip(np.searchsorted(prof, m) - 1, 0, len(prof) - 2))
            w = (m - prof[k]) / (prof[k + 1] - prof[k])
            S.scrivi(modello, f, G.lats, G.lons, ciclo, ora, 2, 3, 18, 106, a, ts[k] * (1 - w) + ts[k + 1] * w, tipo2=106, sc2=2, v2=b, sc1=2)
        for (a, b), v in zip(S.STRATI_NOAH_CM, S.media_strati(strati_sm, None, S.STRATI_NOAH_CM)):
            S.scrivi(modello, f, G.lats, G.lons, ciclo, ora, 2, 0, 25, 106, a, v, tipo2=106, sc2=2, v2=b, sc1=2)
    print(f"ok +{ora:02d} h: {len(liv)} livelli di pressione, {dest.stat().st_size / 1e6:.1f} MB", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ciclo", default="", help="AAAAMMGGHH (00, 06, 12, 18 UTC); vuoto = l'ultimo completo")
    ap.add_argument("--ore", type=int, default=48)
    ap.add_argument("--passo", type=int, default=6)
    ap.add_argument("--out", default="iconeu")
    ap.add_argument("--solo-ciclo", action="store_true")
    a = ap.parse_args()
    testo = a.ciclo or ultimo_ciclo(a.ore)
    if a.solo_ciclo:
        print(testo)
        return
    ciclo = datetime.strptime(testo, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    # campi fissi: quota dei livelli (HHL, 61 livelli a meta' strato), orografia, maschera terra
    _, h = campo(scarica(url_file(ciclo, "ps", "single-level", 0)), template=True)     # fissa anche il ritaglio
    with ThreadPoolExecutor(12) as ex:
        hhl = list(ex.map(lambda k: campo(scarica(url_file(ciclo, "hhl", "time-invariant", None, k))), range(1, NZ + 2)))
    hhl = np.stack(hhl)
    z3 = 0.5 * (hhl[:-1] + hhl[1:])                           # quota dei 60 livelli pieni sul mare
    hsurf = campo(scarica(url_file(ciclo, "hsurf", "time-invariant", None)))
    lsm = campo(scarica(url_file(ciclo, "fr_land", "time-invariant", None)))
    for ora in range(0, a.ore + 1, a.passo):
        un_istante(ciclo, ora, a.out, z3, h, lsm, hsurf)


if __name__ == "__main__":
    main()
