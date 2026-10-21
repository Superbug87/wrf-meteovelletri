#!/usr/bin/env python3
"""PROVA (opzione FISICA=radq): il radar spinge l'umidita' di WRF nella prima ora del run.

  radar_nudging.py --run CARTELLA_CON_wrfinput --ciclo 2026100806 [--minuti 60] [--passo 10]

Dove il radar (Radar-DPC, SRI) vede pioggia, si mettono delle "sonde finte" con umidita' relativa al 100% dalla base
della nube (circa 1 km dal suolo, 500 m con pioggia forte) fino alla cima dell'eco (prodotto ETM del radar; 6 km se
manca), una ogni 6 km nel dominio a 3 km e una ogni 18 km in quello a 9 km, ogni `passo` minuti per i primi `minuti`
minuti del run. WRF le usa con il nudging sulle osservazioni (solo umidita': config.py spegne temperatura e vento):
l'aria satura condensa, scalda, sale e il modello costruisce i temporali dove ci sono davvero.
E' il metodo di Davolio e Buzzi (2004, "rainfall nudging"), fatto con gli strumenti che WRF ha gia'.
La temperatura delle sonde e' quella iniziale di WRF (serve solo per passare da umidita' relativa a specifica).

Scrive OBS_DOMAIN101 (vuoto), OBS_DOMAIN201, OBS_DOMAIN301 e obs_fdda_end.txt nella cartella del run, nello stesso
formato di stazioni_wrf.py (share/wrf_fddaobs_in.F, in4dob: sonde con is_sound = T).
Esce con errore (e senza scrivere nulla) se il radar non risponde o non vede pioggia nel dominio.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar  # noqa: E402

MANCANTE = -888888.0
SOGLIA_MMH = 0.5          # pioggia minima (mm/h, media sulla cella) per saturare la colonna
CIMA_DEFAULT = 6000.0     # m sul mare, se il radar non da' la cima dell'eco
CIMA_MAX = 10000.0
DIRADA = {2: 2, 3: 2}     # una sonda ogni quante celle (18 km e 6 km)


def leggi_colonne(percorso):
    """Dal wrfinput: lat, lon, quota del suolo, quota dei livelli (m), pressione (Pa), temperatura (K)."""
    from netCDF4 import Dataset

    with Dataset(percorso) as ds:
        g = lambda n: ds.variables[n][0].astype("float64")  # noqa: E731
        lat, lon, hgt = g("XLAT"), g("XLONG"), g("HGT")
        p = g("P") + g("PB")
        th = g("T") + 300.0
        ph = (g("PH") + g("PHB")) / 9.81
    z = 0.5 * (ph[1:] + ph[:-1])
    t = th * (p / 1.0e5) ** 0.2857
    return lat, lon, hgt, z, p, t


def f11(x):
    return f"{x:11.3f}"


def sonda(istante, lat, lon, quota, livelli):
    """Una sonda (is_sound = T) con i livelli [(p Pa, z m, T K, rh %)]."""
    righe = [f" {istante:%Y%m%d%H%M%S}", f"  {lat:9.4f} {lon:9.4f} ",
             "  " + "RADAR".ljust(40) + "   " + "RADAR DPC SRI".ljust(40) + "   ",
             "  " + "FM-35 TEMP".ljust(16) + "  " + "RADAR".ljust(16) + "  " + f"{quota:8.1f}" + "  " + "   T" + "  "
             + "   F" + "  " + f"{len(livelli):5d}"]
    for p, z, t, rh in livelli:
        campi = [p, 0.0, z, 0.0, t, 0.0, MANCANTE, MANCANTE, MANCANTE, MANCANTE, rh, 0.0]
        righe.append(" " + "".join(f11(c) + " " for c in campi))
    return "\n".join(righe) + "\n"


def nubi(run, t0):
    """PROVA radnubi: pioggia, neve, nube e vapore iniziali dal radar nel wrfinput dei domini a 9 e 3 km.
    Nelle colonne dove il radar vede pioggia: vapore alla saturazione e 0,3 g/kg di acqua di nube dalla base alla cima
    dell'eco; acqua di pioggia (sotto lo zero termico) o neve (sopra) dal contenuto d'acqua M = 0,072 R^0,88 g/m3
    (Marshall-Palmer), con il numero di gocce di una distribuzione esponenziale (N0 = 8e6 m^-4) per Thompson.
    Dove il radar non vede nulla non si toglie niente (le nubi senza pioggia esistono)."""
    from netCDF4 import Dataset
    from scipy.ndimage import maximum_filter, uniform_filter

    ms = int(t0.timestamp() * 1000)
    sri = radar.scarica("SRI", ms)
    if sri is None:
        raise SystemExit("radar SRI mancante all'istante iniziale: niente nubi dal radar")
    etm = radar.scarica("ETM", ms)
    for d in (2, 3):
        lat, lon, hgt, z, p, t = leggi_colonne(run / f"wrfinput_d0{d}")
        lato = 9 if d == 2 else 3
        r = radar.campiona(uniform_filter(np.nan_to_num(sri), lato), lat, lon)
        v = radar.campiona(uniform_filter(np.isfinite(sri).astype("float32"), lato), lat, lon)
        c = radar.campiona(maximum_filter(np.nan_to_num(etm), lato), lat, lon) if etm is not None else np.full(lat.shape, np.nan)
        umida = (r >= SOGLIA_MMH) & (v > 0.9)
        base = hgt + np.where(r >= 5, 500.0, 1000.0)
        top = np.where(np.isfinite(c) & (c > base + 1000), c, np.maximum(CIMA_DEFAULT, base + 1500))
        top = np.minimum(top, CIMA_MAX)
        es = 611.2 * np.exp(17.67 * (t - 273.15) / (t - 29.65))
        qs = 0.622 * es / np.maximum(p - es, 1.0)
        rho = p / (287.0 * t)
        dentro = umida[None] & (z >= base[None]) & (z <= top[None])
        sotto_cima = umida[None] & (z <= top[None])
        m = 0.072e-3 * np.maximum(r, 0)[None] ** 0.88 / rho            # kg/kg
        liquida = sotto_cima & (t > 273.15)
        ghiaccio = sotto_cima & (t <= 273.15)
        with Dataset(run / f"wrfinput_d0{d}", "r+") as ds:
            def agg(nome, nuovo, dove):
                if nome in ds.variables:
                    x = ds.variables[nome][0]
                    ds.variables[nome][0] = np.where(dove, np.maximum(x, nuovo), x)
            agg("QVAPOR", qs, dentro)
            agg("QCLOUD", np.full(qs.shape, 3e-4), dentro)
            agg("QRAIN", m, liquida)
            agg("QSNOW", m, ghiaccio)
            if "QNRAIN" in ds.variables:
                lam = (np.pi * 1000.0 * 8e6 / np.maximum(rho * m, 1e-12)) ** 0.25
                agg("QNRAIN", 8e6 / lam / rho, liquida)
        print(f"nubi dal radar nel dominio {d}: {int(umida.sum())} colonne con pioggia su {umida.size}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=".")
    ap.add_argument("--ciclo", required=True)
    ap.add_argument("--minuti", type=int, default=60)
    ap.add_argument("--passo", type=int, default=10)
    ap.add_argument("--nubi", action="store_true", help="pioggia, neve e nubi iniziali dal radar nel wrfinput (radnubi)")
    ap.add_argument("--niente-nudging", action="store_true", help="solo --nubi, senza sonde per il nudging")
    a = ap.parse_args()
    run = Path(a.run)
    t0 = datetime.strptime(a.ciclo, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    if a.nubi:
        nubi(run, t0)
    if a.niente_nudging:
        return
    istanti = [t0 + timedelta(minutes=m) for m in range(0, a.minuti + 1, a.passo)]

    domini = {d: leggi_colonne(run / f"wrfinput_d0{d}") for d in (2, 3)}
    lat_c, lon_c = float(domini[2][0].mean()), float(domini[2][1].mean())
    mezzo = int(max(np.ptp(domini[2][0]) * 111, np.ptp(domini[2][1]) * 83) / 2) + 20

    testi = {2: [], 3: []}
    n_umide = {2: 0, 3: 0}
    for ist in istanti:
        ms = int(ist.timestamp() * 1000)
        sri = radar.scarica("SRI", ms)
        if sri is None:
            print(f"radar SRI mancante alle {ist:%H:%M}: salto questo istante")
            continue
        etm = radar.scarica("ETM", ms)
        sri, orig = radar.finestra(sri, lat_c, lon_c, mezzo)
        etm = radar.finestra(etm, lat_c, lon_c, mezzo)[0] if etm is not None else None
        from scipy.ndimage import maximum_filter, uniform_filter

        for d, (lat, lon, hgt, z, p, t) in domini.items():
            k = DIRADA[d]
            lato = 9 if d == 2 else 3                                   # km della cella
            media = uniform_filter(np.nan_to_num(sri), lato * k)         # pioggia media attorno al punto
            valida = uniform_filter(np.isfinite(sri).astype("float32"), lato * k)
            cima = maximum_filter(np.nan_to_num(etm), lato * k) if etm is not None else None
            sel = (slice(k // 2, None, k), slice(k // 2, None, k))
            la, lo, hg = lat[sel], lon[sel], hgt[sel]
            r = radar.campiona(media, la, lo, orig)
            v = radar.campiona(valida, la, lo, orig)
            c = radar.campiona(cima, la, lo, orig) if cima is not None else np.full(la.shape, np.nan)
            zz, pp, tt = z[(slice(None),) + sel], p[(slice(None),) + sel], t[(slice(None),) + sel]
            for j, i in zip(*np.where((r >= SOGLIA_MMH) & (v > 0.9))):
                base = hg[j, i] + (500.0 if r[j, i] >= 5 else 1000.0)
                top = c[j, i] if np.isfinite(c[j, i]) and c[j, i] > base + 1000 else max(CIMA_DEFAULT, base + 1500)
                top = min(top, CIMA_MAX)
                lv = [(pp[q, j, i], zz[q, j, i], tt[q, j, i], 100.0) for q in range(zz.shape[0]) if base <= zz[q, j, i] <= top]
                if lv:
                    testi[d].append(sonda(ist, la[j, i], lo[j, i], hg[j, i], lv))
                    n_umide[d] += 1
    if not testi[3]:
        sys.exit("il radar non vede pioggia nel dominio a 3 km: niente nudging dal radar")
    (run / "OBS_DOMAIN101").write_text("")
    for d in (2, 3):
        (run / f"OBS_DOMAIN{d}01").write_text("".join(testi[d]))
    (run / "obs_fdda_end.txt").write_text(f"{a.minuti}\n")
    (run / "obs_fonti.txt").write_text("radar\n")
    print(f"sonde finte dal radar: {n_umide[3]} nel dominio a 3 km, {n_umide[2]} in quello a 9 km, "
          f"{len(istanti)} istanti fino a +{a.minuti} min")


if __name__ == "__main__":
    main()
