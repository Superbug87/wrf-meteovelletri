#!/usr/bin/env python3
"""Verifica del ciclo rapido con il radar (prove in ombra di .github/workflows/radar-prove.yml lanciate dal regista ogni 3 ore).

  valuta_ciclo_rapido.py --pioggia CARTELLA --ufficiali CARTELLA --stato ciclo_rapido.json [--testo ciclo_rapido.txt]

Per ogni emissione T (ora piena multiplo di 3) con i file pioggia_lazio-ecmwf-<fisica>_<T>.npz e passate almeno 6 ore e 10 minuti,
si scarica il radar (SRI del DPC, 5 minuti) e si confrontano le piogge orarie a +1..+6 ore (ora che finisce a T+j) nel riquadro
centrale di 160 x 160 km, a 2 km:
  fermo     l'ultimo mosaico radar tenuto fermo
  nowcast   ANVIL di pysteps, lo stesso del foglio "prossime 6 ore" del sito
  rad0      WRF corto senza radar (stessa partenza, stessi dati ECMWF)
  radq-radnubi   WRF corto con il radar nell'avvio (umidita' e nubi/pioggia iniziali)
  ecmwf_uff / icon2i_uff   ultima previsione ufficiale pronta all'ora T (ECMWF e ICON-2I restano distinti)
Metriche: FSS a 20 km (soglie 0,5 e 2 mm/h) e rapporto tra pioggia prevista e osservata, sommati su tutte le emissioni.
Lo stato (somme per emissione) e' un JSON cumulativo: le emissioni gia' valutate non si rifanno.
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(QUI, "..", "zone", "lazio", "wrf"))
import radar  # noqa: E402

LAT0, LON0 = 41.9, 12.7
NH, BOX, NB = 6, 80, 10
SOGLIE = (0.5, 2.0)
FISICHE = ("rad0", "radq-radnubi")
NOMI = {"fermo": "radar fermo", "nowcast": "nowcast radar (ANVIL)", "rad0": "WRF corto senza radar",
        "radq-radnubi": "WRF corto con radar", "ecmwf_uff": "WRF ECMWF ufficiale", "icon2i_uff": "WRF ICON-2I ufficiale"}


def blocchi2(a):
    h, w = a.shape[-2:]
    s = a[..., :h // 2 * 2, :w // 2 * 2].reshape(a.shape[:-2] + (h // 2, 2, w // 2, 2))
    return np.nanmean(s, axis=(-3, -1))


def lonlat_griglia(orig, H, W):
    from pyproj import Transformer
    tr = Transformer.from_crs(radar.PROJ, "EPSG:4326", always_xy=True)
    r = orig[0] + 2 * np.arange(H) + 1.0
    c = orig[1] + 2 * np.arange(W) + 1.0
    C, R = np.meshgrid(c, r)
    lon, lat = tr.transform(radar.X0 + C * radar.LATO, radar.Y0 - R * radar.LATO)
    return lat, lon


def regrid(lat_w, lon_w, campi, lat_r, lon_r):
    from pyproj import Transformer
    from scipy.interpolate import griddata
    tr = Transformer.from_crs("EPSG:4326", radar.PROJ, always_xy=True)
    xw, yw = tr.transform(lon_w.ravel(), lat_w.ravel())
    xr, yr = tr.transform(lon_r.ravel(), lat_r.ravel())
    return np.array([np.nan_to_num(griddata((xw, yw), c.ravel(), (xr, yr), method="linear").reshape(lat_r.shape)) for c in campi], np.float32)


def fss_acc(fc, obs, ok):
    from scipy.ndimage import uniform_filter
    fp = uniform_filter((fc * ok).astype(float), NB, mode="constant")
    fo = uniform_filter((obs * ok).astype(float), NB, mode="constant")
    return float(((fp - fo) ** 2).sum()), float((fp ** 2).sum() + (fo ** 2).sum())


def ufficiale(dir_uff, pilota, T, ritardo_h):
    """(estratto, ore di scarto) dell'ultima previsione ufficiale pronta all'ora T; None se manca."""
    for dh in range(0, 49, 12):
        C = (T - timedelta(hours=ritardo_h + dh)).replace(minute=0)
        C = C.replace(hour=C.hour - C.hour % 12)
        f = os.path.join(dir_uff, f"estratto_lazio-{pilota}_{C:%Y%m%d%H}.npz")
        if os.path.exists(f):
            return np.load(f, allow_pickle=True), int((T - C).total_seconds() // 3600)
    return None


def campi_wrf(z_ore, z_pioggia, lat, lon, off, lat_b, lon_b):
    ore = list(z_ore)
    cs = [z_pioggia[ore.index(off + j)].astype(np.float32) if (off + j) in ore else np.full(lat.shape, np.nan, np.float32)
          for j in range(1, NH + 1)]
    return regrid(lat, lon, cs, lat_b, lon_b)


def valuta_emissione(T, files, dir_uff):
    ult = int(T.timestamp() * 1000)
    fine = ult + NH * 3600000
    n = int((fine - (ult - 40 * 60000)) / 300000) + 1
    ist, fr, orig = radar.serie("SRI", fine, n, ritaglio=(LAT0, LON0, 250))
    idx = {t: i for i, t in enumerate(ist)}
    ingresso = [ult - k * 600000 for k in (3, 2, 1, 0)]
    if any(fr[idx[t]] is None for t in ingresso):
        return {"errore": "radar mancante all'avvio"}
    H, W = next(f for f in fr if f is not None).shape
    H2, W2 = H // 2, W // 2
    r0, c0 = (H2 - BOX) // 2, (W2 - BOX) // 2
    box = (slice(r0, r0 + BOX), slice(c0, c0 + BOX))
    lat_r, lon_r = lonlat_griglia(orig, H2, W2)
    lat_b, lon_b = lat_r[box], lon_r[box]
    obs, ok = [], []
    for j in range(1, NH + 1):
        mos = [fr[idx[ult + (12 * (j - 1) + k) * 300000]] for k in range(1, 13)]
        if sum(m is None for m in mos) > 3:
            return {"errore": f"radar mancante a +{j} h"}
        o = np.nanmean(np.stack([m for m in mos if m is not None]), axis=0)
        o2 = blocchi2(o)[box]
        obs.append(np.nan_to_num(o2)); ok.append(np.isfinite(o2))
    obs, ok = np.array(obs, np.float32), np.array(ok)
    prev = {}
    prev["fermo"] = np.repeat(blocchi2(np.nan_to_num(fr[idx[ult]]))[None][:, box[0], box[1]], NH, 0)
    try:
        import pioggia_radar
        ore = pioggia_radar.ore_anvil([fr[idx[t]] for t in ingresso], ingresso)
        if ore is not None:
            prev["nowcast"] = np.array([blocchi2(o)[box] for o in ore[:NH]], np.float32)
    except Exception as e:
        print("  nowcast non disponibile:", str(e)[:100])
    for fis, f in files.items():
        z = np.load(f)
        prev[fis] = campi_wrf(z["ore"], z["pioggia"], z["lat"], z["lon"], 0, lat_b, lon_b)
    for nome, pilota, rit in (("ecmwf_uff", "ecmwf", 11), ("icon2i_uff", "icon2i", 6)):
        u = ufficiale(dir_uff, pilota, T, rit)
        if u is not None:
            d, off = u
            prev[nome] = campi_wrf(d["ore"], d["griglia_pioggia"], d["griglia_lat"], d["griglia_lon"], off, lat_b, lon_b)
            prev[nome + "_scarto_h"] = off
    out = {"pioggia_osservata_max": float(np.nanmax(obs)), "metodi": {}}
    for m, F in prev.items():
        if m.endswith("_scarto_h"):
            out["metodi"].setdefault(m[:-9], {})["scarto_h"] = F
            continue
        r = out["metodi"].setdefault(m, {})
        r["lead"] = []
        for j in range(NH):
            e = {"fc": float(np.nansum(F[j] * ok[j])), "obs": float(np.sum(obs[j] * ok[j]))}
            for s in SOGLIE:
                e[f"fss{s:g}"] = fss_acc(np.nan_to_num(F[j]) >= s, obs[j] >= s, ok[j])
            r["lead"].append(e)
    return out


def riepilogo(stato):
    righe = []
    ems = [v for v in stato.values() if "metodi" in v]
    righe.append(f"Ciclo rapido con il radar: {len(ems)} emissioni valutate "
                 f"({sum(v['pioggia_osservata_max'] >= 0.5 for v in ems)} con pioggia nel riquadro)")
    metodi = [m for m in NOMI if any(m in v["metodi"] for v in ems)]
    for s in SOGLIE:
        righe.append(f"\nFSS a 20 km, soglia {s:g} mm/h (1 = perfetto)  [emissioni usate]")
        righe.append(f"{'':28s}" + "".join(f"  +{j + 1}h  " for j in range(NH)))
        for m in metodi:
            tut = [v["metodi"][m] for v in ems if m in v["metodi"] and "lead" in v["metodi"][m]]
            cel = []
            for j in range(NH):
                num = sum(t["lead"][j][f"fss{s:g}"][0] for t in tut)
                den = sum(t["lead"][j][f"fss{s:g}"][1] for t in tut)
                cel.append(f"{1 - num / den:6.3f} " if den > 0 else "   -   ")
            righe.append(f"{NOMI[m]:28s}" + " ".join(cel) + f"   [{len(tut)}]")
    righe.append("\nPioggia prevista / osservata (1 = giusta)")
    for m in metodi:
        tut = [v["metodi"][m] for v in ems if m in v["metodi"] and "lead" in v["metodi"][m]]
        cel = []
        for j in range(NH):
            o = sum(t["lead"][j]["obs"] for t in tut)
            cel.append(f"{sum(t['lead'][j]['fc'] for t in tut) / o:6.2f} " if o > 0 else "   -   ")
        righe.append(f"{NOMI[m]:28s}" + " ".join(cel))
    return "\n".join(righe) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pioggia", required=True)
    ap.add_argument("--ufficiali", required=True)
    ap.add_argument("--stato", required=True)
    ap.add_argument("--testo")
    a = ap.parse_args()
    stato = json.load(open(a.stato)) if os.path.exists(a.stato) and os.path.getsize(a.stato) > 0 else {}
    per_T = {}
    for f in glob.glob(os.path.join(a.pioggia, "pioggia_lazio-ecmwf-*_*.npz")):
        m = re.match(r"pioggia_lazio-ecmwf-([\w-]+)_(\d{10})\.npz$", os.path.basename(f))
        if m and m.group(1) in FISICHE:
            per_T.setdefault(m.group(2), {})[m.group(1)] = f
    adesso = datetime.now(timezone.utc)
    for c in sorted(per_T):
        T = datetime.strptime(c, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        if c in stato or adesso < T + timedelta(hours=NH, minutes=10) or len(per_T[c]) < 2:
            continue
        print("valuto", c, sorted(per_T[c]), flush=True)
        try:
            stato[c] = valuta_emissione(T, per_T[c], a.ufficiali)
        except Exception as e:
            print("  errore:", str(e)[:200])
            continue
        if "errore" in stato[c]:
            print("  ", stato[c]["errore"])
            del stato[c]                                   # si riprova alla prossima verifica
    json.dump(stato, open(a.stato, "w"))
    t = riepilogo(stato)
    print(t)
    if a.testo:
        open(a.testo, "w").write(t)


if __name__ == "__main__":
    main()
