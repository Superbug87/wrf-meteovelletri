#!/usr/bin/env python3
"""
Crea file wrfout_d03_* e wrfxtrm_d03_* INVENTATI (stessa struttura di quelli di WRF) per collaudare
postprocess.py senza far girare WRF. Non è una previsione.

  python wrf/prova_sintetica.py --out /tmp/sint
  python wrf/postprocess.py --uscite /tmp/sint --out /tmp/sint_mappe --senza-stazioni --da-ora 2026-10-16T08:00

Con --geo CARTELLA scrive anche dei geo_em.d0N.nc inventati (solo posizione e quota, sulla vera geometria dei
domini di dominio.py), per collaudare stazioni_wrf.py --prova senza geogrid.
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

G0, RD = 9.80665, 287.04
NX = NY = 151                                  # come il dominio a 2 km di dominio.py
PASSO_KM = 2.0
NZ = 39
LAT0, LON0 = 41.82, 12.72
ANGOLO = np.radians(1.5)                       # rotazione tra griglia di WRF e nord


def griglia():
    x = (np.arange(NX) - NX // 2) * PASSO_KM    # km
    X, Y = np.meshgrid(x, x)
    lat = LAT0 + Y / 111.32
    lon = LON0 + X / (111.32 * np.cos(np.radians(lat)))
    hgt = (1500 * np.exp(-((X - 10) ** 2 + (Y - 20) ** 2) / (2 * 25.0 ** 2))
           + 300 * (X / 150 + 1) + 400 * np.exp(-((X - 60) ** 2 + (Y + 30) ** 2) / (2 * 20.0 ** 2)))
    return lat, lon, np.clip(hgt, 0, None), X, Y


def stato(h, lat, lon, hgt, X, Y):
    """Campi 2D e 3D inventati all'ora h dall'inizio (00 UTC)."""
    ora_solare = (h + 1) % 24
    diurno = np.sin(2 * np.pi * (ora_solare - 9) / 24)
    instabile = np.clip(np.sin(np.pi * (ora_solare - 10) / 8), 0, 1)       # pomeriggio più instabile
    freddo = 8.0 * np.clip((h - 34) / 6.0, 0, 1)                           # irruzione fredda nell'ultimo giorno: un po' di neve in quota
    T2 = 291.0 + 5.0 * diurno - 0.0065 * hgt + 0.8 * np.sin(X / 30.0) * np.cos(Y / 25.0) - freddo
    td = T2 - 6.0 + 2 * instabile
    e = 611.2 * np.exp(17.62 * (td - 273.15) / (td - 273.15 + 243.12))
    PSFC = (101800.0 - 120 * np.sin(h / 14.0)) * np.exp(-hgt / 8300.0)
    Q2 = 0.622 * e / (PSFC - e)
    spinta = 1 + 0.0006 * (hgt - hgt.mean())
    vel = (3.0 + 2.0 * np.sin(h / 7.0) + 2.0 * instabile) * spinta
    dirz = np.radians(200 + 70 * np.sin(h / 17.0))
    ue, ve = -vel * np.sin(dirz), -vel * np.cos(dirz)
    U10 = ue * np.cos(ANGOLO) + ve * np.sin(ANGOLO)                        # in assi di griglia
    V10 = ve * np.cos(ANGOLO) - ue * np.sin(ANGOLO)

    gamma = 6.3e-3 + 0.9e-3 * instabile
    eta = np.linspace(1.0, 0.0, NZ + 1) ** 1.35
    p_stag = 5000.0 + eta[:, None, None] * (PSFC - 5000.0)[None]
    z_stag = np.zeros_like(p_stag)
    z_stag[0] = hgt
    T_stag = np.zeros_like(p_stag)
    T_stag[0] = T2
    for k in range(1, NZ + 1):
        Tk = T_stag[k - 1].copy()
        for _ in range(3):
            Tm = 0.5 * (T_stag[k - 1] + Tk)
            dz = RD * Tm / G0 * np.log(p_stag[k - 1] / p_stag[k])
            Tk = T_stag[k - 1] - gamma * dz * (z_stag[k - 1] + dz < 11000) - 0.0 * dz
        z_stag[k] = z_stag[k - 1] + RD * 0.5 * (T_stag[k - 1] + Tk) / G0 * np.log(p_stag[k - 1] / p_stag[k])
        T_stag[k] = Tk
    p = 0.5 * (p_stag[:-1] + p_stag[1:])
    tk = 0.5 * (T_stag[:-1] + T_stag[1:])
    z_m = 0.5 * (z_stag[:-1] + z_stag[1:])
    rh = np.clip(0.8 - 0.55 * (z_m - hgt) / 9000.0, 0.15, 0.8)
    es = 611.2 * np.exp(17.62 * (tk - 273.15) / (tk - 273.15 + 243.12))
    qv = rh * 0.622 * es / (p - es)
    qv[0] = Q2
    th = tk * (1e5 / p) ** (RD / 1004.0)

    # vento in quota: cresce con l'altezza e gira in senso orario (veering), come prima di un temporale organizzato
    zagl = z_m - hgt
    vq = vel[None] * (1 + 0.8 * zagl / 1000.0 / (1 + 0.15 * zagl / 1000.0)) + 0.002 * zagl
    dq = dirz + np.radians(45.0) * np.clip(zagl / 6000.0, 0, 1.5)
    ue3, ve3 = -vq * np.sin(dq), -vq * np.cos(dq)
    u3 = ue3 * np.cos(ANGOLO) + ve3 * np.sin(ANGOLO)                        # in assi di griglia
    v3 = ve3 * np.cos(ANGOLO) - ue3 * np.sin(ANGOLO)
    # nubi: basse con l'umidità, uno strato medio e dei cirri che vanno e vengono
    cld = np.clip((rh + 0.12 * np.sin(X / 40.0)[None] - 0.55) / 0.25, 0, 1)
    cld += 0.7 * np.exp(-((zagl - 5500.0) / 700.0) ** 2) * (0.5 + 0.5 * np.sin(h / 6.0))
    cld += 0.6 * np.exp(-((zagl - 9500.0) / 900.0) ** 2) * (0.5 + 0.5 * np.sin(h / 11.0 + 1))
    cld = np.clip(cld, 0, 1)
    # flussi e strato limite
    ust = 0.06 * vel + 0.25 * instabile
    pblh = 150.0 + 1600.0 * instabile
    hfx = -15.0 + 220.0 * instabile
    return dict(T2=T2, Q2=Q2, PSFC=PSFC, U10=U10, V10=V10, p=p, th=th, qv=qv, z_stag=z_stag, vel=vel,
                u3=u3, v3=v3, cld=cld, ust=ust, pblh=pblh, hfx=hfx)


def pioggia_cumulata(h, X, Y):
    """Un temporale inventato nell'angolo sud-ovest da h=30 a h=40 (nel dominio a 2 km: 300 km di lato)."""
    attivo = np.clip((h - 30) / 10.0, 0, 1)
    macchia = np.exp(-((X + 60) ** 2 + (Y + 50) ** 2) / (2 * 25.0 ** 2))
    return 14.0 * attivo ** 1.3 * macchia


def neve_cumulata(h, hgt):
    """Neve inventata sopra i 700 m durante l'irruzione fredda (h da 36 a 46): 5 mm d'acqua."""
    return 5.0 * np.clip((h - 36) / 10.0, 0, 1) * (hgt > 700)


def cella(h, X, Y):
    """Forma (0-1) del temporale inventato nell'angolo sud-ovest, attivo tra h=30 e h=40."""
    attivo = float(np.clip(1 - abs(h - 35) / 5.0, 0, 1))
    return attivo * np.exp(-((X + 60) ** 2 + (Y + 50) ** 2) / (2 * 20.0 ** 2))


def scrivi(percorso, ore, tempi_utc, lat, lon, hgt, X, Y, xtrm=False):
    with Dataset(percorso, "w") as ds:
        ds.createDimension("Time", None)
        ds.createDimension("DateStrLen", 19)
        ds.createDimension("west_east", NX)
        ds.createDimension("south_north", NY)
        if xtrm:
            v = ds.createVariable("SPDUV10MAX", "f4", ("Time", "south_north", "west_east"))
            tv = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
            for i, (h, t) in enumerate(zip(ore, tempi_utc)):
                s = stato(h, lat, lon, hgt, X, Y)
                ue = np.hypot(s["U10"], s["V10"])
                v[i] = ue * 1.15
                tv[i] = np.array(list(t.strftime("%Y-%m-%d_%H:%M:%S")), dtype="S1")
            return
        ds.createDimension("bottom_top", NZ)
        ds.createDimension("bottom_top_stag", NZ + 1)
        ds.setncattr("USE_THETA_M", 0)
        f2 = lambda n: ds.createVariable(n, "f4", ("Time", "south_north", "west_east"))
        f3 = lambda n, d="bottom_top": ds.createVariable(n, "f4", ("Time", d, "south_north", "west_east"))
        tv = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
        ds.createDimension("west_east_stag", NX + 1)
        ds.createDimension("south_north_stag", NY + 1)
        var2 = {n: f2(n) for n in ("XLAT", "XLONG", "HGT", "T2", "Q2", "PSFC", "U10", "V10", "COSALPHA",
                                  "SINALPHA", "RAINC", "RAINNC", "SNOWNC", "UST", "PBLH", "HFX", "WSPD10MAX",
                                  "W_UP_MAX", "GRPL_MAX", "UP_HELI_MAX", "LPI")}
        P, PB, T, QV, CLD = f3("P"), f3("PB"), f3("T"), f3("QVAPOR"), f3("CLDFRA")
        UU = ds.createVariable("U", "f4", ("Time", "bottom_top", "south_north", "west_east_stag"))
        VV = ds.createVariable("V", "f4", ("Time", "bottom_top", "south_north_stag", "west_east"))
        PH, PHB = f3("PH", "bottom_top_stag"), f3("PHB", "bottom_top_stag")
        for i, (h, t) in enumerate(zip(ore, tempi_utc)):
            s = stato(h, lat, lon, hgt, X, Y)
            tv[i] = np.array(list(t.strftime("%Y-%m-%d_%H:%M:%S")), dtype="S1")
            var2["XLAT"][i], var2["XLONG"][i], var2["HGT"][i] = lat, lon, hgt
            for n in ("T2", "Q2", "PSFC", "U10", "V10"):
                var2[n][i] = s[n]
            var2["COSALPHA"][i], var2["SINALPHA"][i] = np.cos(ANGOLO), np.sin(ANGOLO)
            pioggia = pioggia_cumulata(h, X, Y)
            var2["RAINC"][i], var2["RAINNC"][i] = 0.6 * pioggia, 0.4 * pioggia
            P[i], PB[i] = 0.0, s["p"]
            T[i], QV[i] = s["th"] - 300.0, s["qv"]
            PH[i], PHB[i] = 0.0, G0 * s["z_stag"]
            CLD[i] = s["cld"]
            u3, v3 = s["u3"], s["v3"]                                        # ai punti di massa; U e V sono sfalsati
            UU[i] = np.concatenate([u3[:, :, :1], 0.5 * (u3[:, :, :-1] + u3[:, :, 1:]), u3[:, :, -1:]], axis=2)
            VV[i] = np.concatenate([v3[:, :1], 0.5 * (v3[:, :-1] + v3[:, 1:]), v3[:, -1:]], axis=1)
            var2["SNOWNC"][i] = neve_cumulata(h, hgt)
            var2["UST"][i], var2["PBLH"][i], var2["HFX"][i] = s["ust"], s["pblh"], s["hfx"]
            var2["WSPD10MAX"][i] = s["vel"] * 1.15
            ce = cella(h, X, Y)                                              # massimi dell'ora nel temporale
            var2["W_UP_MAX"][i], var2["GRPL_MAX"][i] = 16.0 * ce + 0.5, 6.0 * ce
            var2["UP_HELI_MAX"][i], var2["LPI"][i] = 120.0 * ce, 5.0 * ce


def geo_finti(cartella):
    """geo_em.d01/02/03.nc con XLAT_M, XLONG_M e HGT_M, sulla proiezione di Lambert dei domini veri."""
    import sys
    from pyproj import Proj, Transformer

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dominio as D

    lcc = Proj(proj="lcc", lat_1=D.TRUELAT1, lat_2=D.TRUELAT2, lat_0=D.LAT0, lon_0=D.LON0, R=6370000)
    a_ll = Transformer.from_proj(lcc, "EPSG:4326", always_xy=True)
    starts = D.parent_starts()
    origini = []
    for d in range(3):                                   # angolo sud-ovest di ogni dominio, in metri dal centro
        if d == 0:
            origini.append((-D.celle(0) * D.DX[0] / 2,) * 2)
        else:
            px, py = origini[d - 1]
            origini.append((px + (starts[d] - 1) * D.DX[d - 1], py + (starts[d] - 1) * D.DX[d - 1]))
    Path(cartella).mkdir(parents=True, exist_ok=True)
    for d in range(3):
        n = D.celle(d)
        x = origini[d][0] + (np.arange(n) + 0.5) * D.DX[d]
        y = origini[d][1] + (np.arange(n) + 0.5) * D.DX[d]
        X, Y = np.meshgrid(x, y)
        lon, lat = a_ll.transform(X, Y)
        hgt = 300 + 500 * np.exp(-((X + 5000) ** 2 + (Y - 4000) ** 2) / (2 * 12000.0 ** 2))
        with Dataset(Path(cartella) / f"geo_em.d0{d + 1}.nc", "w") as ds:
            ds.createDimension("Time", 1)
            ds.createDimension("south_north", n)
            ds.createDimension("west_east", n)
            for nome, val in (("XLAT_M", lat), ("XLONG_M", lon), ("HGT_M", hgt)):
                ds.createVariable(nome, "f4", ("Time", "south_north", "west_east"))[0] = val
    print("creati i geo_em inventati in", cartella)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="/tmp/sint")
    ap.add_argument("--ciclo", default="2026101600")
    ap.add_argument("--geo", default="", help="scrive anche dei geo_em.d0N.nc inventati in questa cartella")
    a = ap.parse_args()
    if a.geo:
        geo_finti(a.geo)
    c = datetime.strptime(a.ciclo, "%Y%m%d%H")
    lat, lon, hgt, X, Y = griglia()
    for seg, (da, fino) in enumerate([(0, 16), (16, 32), (32, 48)]):       # come i tre segmenti da 16 h
        cart = Path(a.out) / f"uscite-{seg}"
        cart.mkdir(parents=True, exist_ok=True)
        ore = list(range(da, fino + 1))
        tempi = [c + timedelta(hours=h) for h in ore]
        nome = tempi[0].strftime("%Y-%m-%d_%H:%M:%S")
        scrivi(cart / f"wrfout_d03_{nome}", ore, tempi, lat, lon, hgt, X, Y)
        scrivi(cart / f"wrfxtrm_d03_{nome}", ore, tempi, lat, lon, hgt, X, Y, xtrm=True)
        print("creati i file del segmento", seg, "da", nome)


if __name__ == "__main__":
    main()
