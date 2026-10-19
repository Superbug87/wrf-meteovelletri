#!/usr/bin/env python3
"""
pioggia_radar.py - la pioggia di WRF messa a confronto col radar, e il radar spinto avanti di 3 ore.

  python wrf/pioggia_radar.py --uscite cartella_con_wrfout --out mappe --ciclo 2026101600 [--pilota ecmwf]

Crea due immagini (e un CSV coi numeri) nella cartella --out:
  radar_1_verifica.png        le ultime ore (fino a 6): pioggia misurata dal radar e pioggia calcolata da WRF,
                              ora per ora, con qualche numero per capire quanto WRF ci ha preso;
  radar_2_prossime_3_ore.png  la pioggia delle prossime 3 ore: il radar spostato lungo il movimento che ha adesso,
                              WRF, e un mix dei due (all'inizio conta il radar, poi sempre di piu' WRF).
  radar_verifica.csv          le statistiche della verifica, da conservare per vedere nel tempo se WRF migliora.

Il radar e' quello nazionale della Protezione Civile (Radar-DPC, CC BY-SA 4.0). Se non risponde, lo script si ferma
senza toccare le mappe di WRF.

Cosa NON e' provato: l'estrapolazione del radar e' stata collaudata su pochi giorni (vedi README) e non migliora
sempre rispetto a "la pioggia resta dov'e'"; i pesi del mix sono una scelta di buon senso, non tarati.
"""
import argparse
import csv
import glob
import os
import sys
import textwrap
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["TZ"] = "Europe/Rome"      # come postprocess.py: le ore dei grafici sono quelle locali
time.tzset()
QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
sys.path.insert(0, str(QUI.parent))

import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from netCDF4 import Dataset  # noqa: E402
from scipy.ndimage import uniform_filter  # noqa: E402

import postprocess as pp  # noqa: E402
import previsioni_lazio as pc  # noqa: E402
import radar  # noqa: E402

warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="All-NaN")

ORE_STORIA = 6               # ore passate da verificare
MEZZO = 250                  # il ritaglio del radar e' un quadrato di 500 km di lato: la pioggia che arriva in 3 ore da lontano ci sta
N_GRIGLIA = 129              # punti per lato della griglia delle mappe (passo ~2 km: come i 2 km di WRF)
SOGLIE_FSS = (0.5, 2.0)      # mm in un'ora
LATO_FSS_KM = 10
ORE_AVANTI = 3
PASSO_GRIGLIA_KM = 2 * pc.MEZZO_LAT * 111.32 / (N_GRIGLIA - 1)
ROMA = ZoneInfo("Europe/Rome")
LIVELLI_1H = pc.LIVELLI_PIOGGIA_1H


def locale(dt_utc):
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(ROMA).replace(tzinfo=None)


def da_ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)


# ----------------------------------------------------------------------------
# WRF
# ----------------------------------------------------------------------------
def leggi_wrf(cartella, lat_f, lon_f):
    """Pioggia cumulata di WRF (RAINC + RAINNC) sulla griglia delle mappe: (tempi UTC, cumulata, quota) o None."""
    files = sorted(glob.glob(os.path.join(cartella, "**", "wrfout_d03_*"), recursive=True))
    if not files:
        return None
    cum, st = {}, None
    for f in files:
        with Dataset(f) as ds:
            ds.set_auto_mask(False)
            tempi = pp.tempi_file(ds)
            r = np.asarray(ds["RAINC"][:], float) + np.asarray(ds["RAINNC"][:], float)
            if st is None:
                st = {k: np.asarray(ds[n][0], float) for k, n in (("lat", "XLAT"), ("lon", "XLONG"), ("hgt", "HGT"))}
            for t, a in zip(tempi, r):                        # al riavvio il primo istante e' doppio: vale l'ultimo
                cum[t] = a
    tempi = sorted(cum)
    campi, z = pp.ricampiona({"rain": np.stack([cum[t] for t in tempi])}, st, lat_f, lon_f)
    return tempi, campi["rain"], z


def cumulata(tempi, cum, t):
    """Pioggia cumulata di WRF all'istante t (UTC), interpolata nel tempo tra le uscite; None se fuori dal periodo."""
    s = np.array([(x - tempi[0]).total_seconds() for x in tempi])
    ts = (t - tempi[0]).total_seconds()
    if ts < s[0] - 1 or ts > s[-1] + 1 or len(s) < 2:
        return None
    i = int(np.clip(np.searchsorted(s, ts, side="right") - 1, 0, len(s) - 2))
    w = float(np.clip((ts - s[i]) / (s[i + 1] - s[i]), 0, 1))
    return (1 - w) * cum[i] + w * cum[i + 1]


def pioggia_wrf(wrf, a, b):
    """mm di pioggia di WRF tra gli istanti a e b (UTC), o None se WRF non copre il periodo."""
    if wrf is None:
        return None
    ca, cb = cumulata(wrf[0], wrf[1], a), cumulata(wrf[0], wrf[1], b)
    return None if ca is None or cb is None else np.clip(cb - ca, 0, None)


# ----------------------------------------------------------------------------
# Confronto
# ----------------------------------------------------------------------------
def fss(prev, oss, soglia, n):
    """Fractions Skill Score: 1 = pioggia sopra soglia negli stessi punti (entro n pixel), 0 = mai sovrapposta."""
    fp = uniform_filter((prev >= soglia).astype(float), n)
    fo = uniform_filter((oss >= soglia).astype(float), n)
    den = (fp ** 2).mean() + (fo ** 2).mean()
    return float("nan") if den < 1e-9 else 1.0 - ((fp - fo) ** 2).mean() / den


def griglia_radar(campo, orig, LAT, LON):
    """Il radar (pixel da 1 km) sulla griglia delle mappe (2 km): media sui pixel attorno a ogni punto, come fa WRF
    con una cella da 2 km, invece di prendere il valore di un solo pixel."""
    n = max(1, int(round(PASSO_GRIGLIA_KM)))
    if n > 1:
        ok = np.isfinite(campo)
        somma = uniform_filter(np.where(ok, campo, 0.0), n)
        peso = uniform_filter(ok.astype("float32"), n)
        campo = np.where(peso > 0.5, somma / np.maximum(peso, 1e-6), np.nan)
    return radar.campiona(campo, LAT, LON, orig)


def verifica(frames, istanti, orig, wrf, LAT, LON, passo_km):
    """Per ognuna delle ultime ore intere: pioggia oraria del radar e di WRF sulla griglia, e le statistiche."""
    n_fss = int(round(LATO_FSS_KM / passo_km)) | 1
    ult = istanti[-1]
    ora_piena = ult - ult % 3600000
    out = []
    for k in range(ORE_STORIA - 1, -1, -1):
        fine = ora_piena - k * 3600000
        idx = [i for i, t in enumerate(istanti) if fine - 3600000 < t <= fine and frames[i] is not None]
        if len(idx) < 9:
            continue                                         # troppi mosaici mancanti in quest'ora
        mm = np.nanmean(np.stack([frames[i] for i in idx]), axis=0)      # mm/h medio = mm nell'ora
        r = griglia_radar(mm, orig, LAT, LON)
        w = pioggia_wrf(wrf, da_ms(fine - 3600000), da_ms(fine))
        if w is None:
            continue
        ok = np.isfinite(r)
        if ok.mean() < 0.8:
            continue                                         # l'area e' in parte fuori dalla copertura del radar
        r0, w0 = np.where(ok, r, 0.0), np.where(ok, w, 0.0)
        out.append(dict(
            fine=da_ms(fine), radar=np.where(ok, r, np.nan), wrf=w,
            mm_radar=float(r0.sum() / ok.sum()), mm_wrf=float(w0.sum() / ok.sum()),
            bagnato_radar=float((r0 >= 0.2).sum() / ok.sum() * 100), bagnato_wrf=float((w0 >= 0.2).sum() / ok.sum() * 100),
            fss=[fss(w0, r0, s, n_fss) for s in SOGLIE_FSS]))
    return out


# ----------------------------------------------------------------------------
# Grafica
# ----------------------------------------------------------------------------
def disegna_griglia(percorso, titolo, sotto, banner, riquadri, colonne, testo, z_f, lat_f, lon_f, W=6.4):
    """Griglia di riquadri con la scala della pioggia oraria. riquadri: lista di dict(didascalia, campo)."""
    MARG, GAP, ETICH, HEAD, CB = 0.3, 0.1, 0.34, 1.55, 0.95
    lato = (W - 2 * MARG - (colonne - 1) * GAP) / colonne
    passo_riga = lato + ETICH + 0.06
    righe = -(-len(riquadri) // colonne)
    foot = 0.65 + 0.2 * (testo.count("\n") + 1)
    H = HEAD + righe * passo_riga + CB + foot
    halo = [pe.withStroke(linewidth=2.5, foreground=pc.SFONDO)]
    LON, LAT = np.meshgrid(lon_f, lat_f)
    iy = int(np.abs(lat_f - pc.STAZIONE[1]).argmin())
    ix = int(np.abs(lon_f - pc.STAZIONE[2]).argmin())
    liv = list(LIVELLI_1H)
    cols = pc.colori_fasce("pioggia", len(liv))

    def riempi(ax, campo):
        if np.nanmax(campo) < liv[0]:
            return None
        cs = ax.contourf(LON, LAT, np.nan_to_num(campo), levels=liv, colors=cols[:-1], extend="max", zorder=1)
        cs.cmap.set_over(cols[-1])
        cs.changed()
        return cs

    fig = plt.figure(figsize=(W, H), facecolor=pc.SFONDO)
    fig.text(MARG / W, 1 - 0.40 / H, titolo, fontsize=16, weight="bold", color=pc.INK, va="center")
    fig.text(MARG / W, 1 - 0.80 / H, sotto, fontsize=11, color=pc.SEC, va="center")
    fig.text(MARG / W, 1 - 1.20 / H, banner, fontsize=10.5, color=pc.SEC, va="center")
    for i, q in enumerate(riquadri):
        r, c = divmod(i, colonne)
        x0 = MARG + c * (lato + GAP)
        alto = HEAD + r * passo_riga + ETICH
        ax = fig.add_axes([x0 / W, 1 - (alto + lato) / H, lato / W, lato / H])
        ax.set_facecolor(pc.SFONDO)
        ax.set_xlim(pc.LON_MIN, pc.LON_MAX)
        ax.set_ylim(pc.LAT_MIN, pc.LAT_MAX)
        ax.set_aspect(1 / np.cos(np.radians(pc.LAT0)))
        ax.set_xticks([])
        ax.set_yticks([])
        for sp_ in ax.spines.values():
            sp_.set_color(pc.LINEA)
        riempi(ax, q["campo"])
        pc.disegna_confini(ax, scala=0.55)
        ax.contour(lon_f, lat_f, z_f, levels=pc.LIVELLI_QUOTA, colors=pc.TENUE, linewidths=0.4,
                   alpha=0.8, zorder=2)
        ax.plot(pc.STAZIONE[2], pc.STAZIONE[1], "*", ms=8, color=pc.INK, mec=pc.SFONDO, mew=0.6, zorder=8)
        v = q["campo"][iy, ix]
        ax.text(0.96, 0.04, "–" if not np.isfinite(v) else f"{v:.1f}", transform=ax.transAxes, ha="right",
                va="bottom", fontsize=11, weight="bold", color=pc.INK, zorder=9, path_effects=halo)
        fig.text(x0 / W, 1 - (alto - 0.05) / H, q["didascalia"], fontsize=10.5, weight="bold", color=pc.INK, va="bottom")

    asse = fig.add_axes([0, 0, 0.01, 0.01])                  # barra dei colori: stessi livelli dei riquadri
    asse.set_visible(False)
    ref = np.linspace(liv[0], liv[-1] * 1.5, LON.size).reshape(LON.shape)
    cs = riempi(asse, ref)
    y_cb = HEAD + righe * passo_riga + 0.35
    cax = fig.add_axes([(W - 5.0) / 2 / W, 1 - (y_cb + 0.17) / H, 5.0 / W, 0.17 / H])
    cb = fig.colorbar(cs, cax=cax, orientation="horizontal")
    tk = pc.tick_utili(liv, 14)
    cb.set_ticks(tk)
    cb.set_ticklabels([f"{t:g}" for t in tk])
    cb.ax.tick_params(labelsize=10, colors=pc.SEC, length=3)
    cb.outline.set_visible(False)
    fig.text(MARG / W, 1 - (HEAD + righe * passo_riga + CB + 0.05) / H, testo, fontsize=9.5, color=pc.SEC, va="top",
             linespacing=1.4)
    fig.savefig(percorso, dpi=150, facecolor=pc.SFONDO)
    plt.close(fig)
    return percorso


def disegna_testo(percorso, titolo, sotto, banner, messaggio, dettaglio, testo, W=6.4):
    """Foglio senza riquadri (niente pioggia, o niente da confrontare)."""
    MARG = 0.3
    foot = 0.65 + 0.2 * (testo.count("\n") + 1)
    H = 2.9 + foot
    fig = plt.figure(figsize=(W, H), facecolor=pc.SFONDO)
    fig.text(MARG / W, 1 - 0.40 / H, titolo, fontsize=16, weight="bold", color=pc.INK, va="center")
    fig.text(MARG / W, 1 - 0.80 / H, sotto, fontsize=11, color=pc.SEC, va="center")
    fig.text(MARG / W, 1 - 1.20 / H, banner, fontsize=10.5, color=pc.SEC, va="center")
    fig.text(MARG / W, 1 - 1.85 / H, messaggio, fontsize=15, weight="bold", color=pc.INK, va="center")
    fig.text(MARG / W, 1 - 2.30 / H, dettaglio, fontsize=11, color=pc.SEC, va="center")
    fig.text(MARG / W, 1 - 2.95 / H, testo, fontsize=9.5, color=pc.SEC, va="top", linespacing=1.4)
    fig.savefig(percorso, dpi=150, facecolor=pc.SFONDO)
    plt.close(fig)
    return percorso


def a_capo(righe, larghezza=74):
    return "\n".join(textwrap.fill(r, larghezza) for r in righe)


FONTE_RADAR = ("Radar: Radar-DPC, Dipartimento della Protezione Civile (radar.protezionecivile.it), licenza CC BY-SA "
               "4.0; immagini ricavate da questi dati, stessa licenza.")
NON_UFFICIALE = "Non sostituisce le previsioni ufficiali."


# ----------------------------------------------------------------------------
# Le due immagini
# ----------------------------------------------------------------------------
def foglio_verifica(ris, banner, z_f, lat_f, lon_f, out, ora_radar_loc, ciclo_txt):
    sotto_base = f"ultime {len(ris)} ore, radar alle {ora_radar_loc:%H:%M}"
    mx_r = max(float(np.nanmax(r["radar"])) for r in ris)
    mx_w = max(float(r["wrf"].max()) for r in ris)
    if max(mx_r, mx_w) < 0.2:
        return disegna_testo(
            Path(out) / "radar_1_verifica.png", "Pioggia: radar e WRF a confronto", sotto_base, banner,
            "Asciutto, per radar e per WRF",
            f"Massimo in un'ora sull'area: radar {mx_r:.1f} mm, WRF {mx_w:.1f} mm.",
            a_capo([FONTE_RADAR, NON_UFFICIALE]))
    scelti = ris[::-1][::2][::-1] if len(ris) > 3 else ris          # una riga ogni due ore, al massimo 3 righe
    scelti = scelti[-3:]
    riq = []
    for r in scelti:
        a, b = locale(r["fine"] - timedelta(hours=1)), locale(r["fine"])
        riq.append(dict(didascalia=f"Radar · {a:%H}–{b:%H}", campo=r["radar"]))
        riq.append(dict(didascalia=f"WRF · {a:%H}–{b:%H}", campo=r["wrf"]))
    righe = []
    for r in ris:
        a, b = locale(r["fine"] - timedelta(hours=1)), locale(r["fine"])
        f = "–" if not np.isfinite(r["fss"][0]) else f"{r['fss'][0]:.2f}"
        righe.append(f"{a:%H}–{b:%H}: radar {r['mm_radar']:.1f} mm, WRF {r['mm_wrf']:.1f} mm · "
                     f"area bagnata {r['bagnato_radar']:.0f}% / {r['bagnato_wrf']:.0f}% · accordo {f}")
    tot_r, tot_w = sum(r["mm_radar"] for r in ris), sum(r["mm_wrf"] for r in ris)
    f_ok = [r["fss"][0] for r in ris if np.isfinite(r["fss"][0])]
    righe.append(f"Totale: radar {tot_r:.1f} mm, WRF {tot_w:.1f} mm"
                 + (f"; accordo medio {np.mean(f_ok):.2f}" if f_ok else ""))
    testo = a_capo(
        ["★ = Velletri: il suo valore (mm in un'ora) è scritto in basso a destra di ogni riquadro.",
         "Valori medi sull'area delle mappe (circa 256 x 256 km, il Lazio e dintorni). «Area bagnata» = quota dell'area con almeno 0.2 mm nell'ora "
         "(radar / WRF). «Accordo» = pioggia da almeno 0.5 mm negli stessi punti, controllata su circa 10 km (3 pixel): "
         "1 = coincide, 0 = mai."] + righe
        + ["Il radar è un'osservazione indiretta: può sbagliare (echi dei monti, schermature, calibrazione).",
           FONTE_RADAR, NON_UFFICIALE])
    return disegna_griglia(Path(out) / "radar_1_verifica.png", "Pioggia: radar e WRF a confronto", sotto_base, banner,
                           riq, 2, testo, z_f, lat_f, lon_f)


def peso_radar(ore_dal_via):
    """Quanto conta il radar nel mix, a meta' dell'ora indicata: 1 subito, 0 dopo 3 ore (scelta di buon senso)."""
    return float(np.clip(1 - ore_dal_via / ORE_AVANTI, 0, 1))


def foglio_avanti(frames, istanti, orig, wrf, LAT, LON, banner, z_f, lat_f, lon_f, out, ciclo_txt):
    ult_ms = istanti[-1]
    R0 = frames[-1]
    if R0 is None:
        print("Ultimo mosaico mancante: salto le prossime 3 ore.", file=sys.stderr)
        return None
    t0 = da_ms(ult_ms)
    i0 = next((i for i in range(len(frames) - 1, -1, -1)
               if frames[i] is not None and 25 * 60000 <= ult_ms - istanti[i] <= 45 * 60000), None)
    if i0 is None:
        v, info = np.zeros(2), "manca il mosaico di mezz'ora fa"
    else:
        v, info = radar.movimento(frames[i0], R0, round((ult_ms - istanti[i0]) / (radar.PASSO_MIN * 60000)))
    flusso = np.broadcast_to(np.asarray(v, "float32"), R0.shape + (2,)).copy()
    R0z = np.nan_to_num(R0)
    ore = []
    for j in range(1, ORE_AVANTI + 1):
        tassi = [radar.estrapola(R0z, flusso, k) for k in range(12 * (j - 1) + 1, 12 * j + 1)]
        ore.append(griglia_radar(np.mean(tassi, axis=0), orig, LAT, LON))       # mm/h medio = mm nell'ora
    wrf_ore = [pioggia_wrf(wrf, t0 + timedelta(hours=j - 1), t0 + timedelta(hours=j)) for j in range(1, ORE_AVANTI + 1)]
    ha_wrf = all(w is not None for w in wrf_ore)

    loc0 = locale(t0)
    finestre = ", ".join(f"+{j} h = {loc0 + timedelta(hours=j - 1):%H:%M}–{loc0 + timedelta(hours=j):%H:%M}"
                         for j in range(1, ORE_AVANTI + 1))
    km_h = float(np.hypot(*v)) * 12 * radar.LATO / 1000
    if km_h > 1.0:
        dest = pc.nome_direzione(-v[0], v[1])                  # v = (colonne verso est, righe verso sud): verso nord = -v[1]
        mov = f"La pioggia si sposta verso {dest} a circa {km_h:.0f} km/h (stima dal radar)."
    else:
        mov = f"Movimento della pioggia non stimabile ({info}): il radar è lasciato fermo."
    sotto = f"dalle {loc0:%H:%M}, ora del radar"

    max_r = max(float(np.nanmax(o)) for o in ore)
    max_w = max(float(w.max()) for w in wrf_ore) if ha_wrf else 0.0
    if max(max_r, max_w) < 0.1:
        return disegna_testo(
            Path(out) / "radar_2_prossime_3_ore.png", "Pioggia nelle prossime 3 ore", sotto, banner,
            "Nessuna pioggia prevista",
            f"Radar spostato: max {max_r:.1f} mm/h" + (f"; WRF: max {max_w:.1f} mm/h." if ha_wrf else "."),
            a_capo([FONTE_RADAR, NON_UFFICIALE]))

    riq = []
    for j, o in enumerate(ore, start=1):
        if ha_wrf:
            w = peso_radar(j - 0.5)
            mix = w * np.nan_to_num(o) + (1 - w) * wrf_ore[j - 1]
            riq.append(dict(didascalia=f"Mix +{j} h · radar {w * 100:.0f}%", campo=mix))
        else:
            riq.append(dict(didascalia=f"Radar +{j} h", campo=np.nan_to_num(o)))
    if ha_wrf:
        riq += [dict(didascalia=f"Radar spostato +{j} h", campo=np.nan_to_num(o)) for j, o in enumerate(ore, start=1)]
        riq += [dict(didascalia=f"WRF +{j} h", campo=w) for j, w in enumerate(wrf_ore, start=1)]
    testo = a_capo(
        ["★ = Velletri: il suo valore (mm nell'ora) è scritto in basso a destra di ogni riquadro.",
         f"{finestre}. {mov}",
         (f"Mix = radar spostato e WRF, con il peso del radar che scende da {peso_radar(0.5) * 100:.0f}% nella prima "
          f"ora a {peso_radar(2.5) * 100:.0f}% nella terza. I pesi sono una scelta di buon senso, non tarata." if ha_wrf
          else "WRF non copre queste ore: solo il radar spostato, che dopo un'ora o due perde molto valore."),
         "Spostare il radar aiuta poco, e solo se la pioggia si muove veloce (oltre circa 24 km/h): altrimenti resta "
         "ferma. I temporali isolati nascono e muoiono e non si prevedono così.",
         FONTE_RADAR, NON_UFFICIALE])
    return disegna_griglia(Path(out) / "radar_2_prossime_3_ore.png", "Pioggia nelle prossime 3 ore", sotto, banner,
                           riq, 3, testo, z_f, lat_f, lon_f)


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uscite", required=True, help="cartella con i wrfout_d03_* (anche in sottocartelle)")
    ap.add_argument("--out", default="mappe")
    ap.add_argument("--ciclo", default="", help="AAAAMMGGHH del ciclo usato, solo per il titolo e il CSV")
    ap.add_argument("--pilota", default="icon2i", choices=["icon2i", "ecmwf", "gfs", "iconeu"])
    ap.add_argument("--radar-fine", default="", help="prova: istante UTC ISO dell'ultimo mosaico (default: l'ultimo pubblicato)")
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)

    lat_f, lon_f = pc.griglia_fine(N_GRIGLIA)
    LON, LAT = np.meshgrid(lon_f, lat_f)
    passo_km = 2 * pc.MEZZO_LAT * 111.32 / (N_GRIGLIA - 1)

    wrf = leggi_wrf(a.uscite, lat_f, lon_f)
    if wrf is None:
        sys.exit(f"Nessun file wrfout_d03_* in {a.uscite}")
    z_f = np.clip(wrf[2], 0, None)
    wrf = (wrf[0], wrf[1])
    print(f"WRF: {len(wrf[0])} istanti, da {wrf[0][0]:%d %H:%M} a {wrf[0][-1]:%d %H:%M} UTC")

    try:
        if a.radar_fine:
            fine = int(datetime.fromisoformat(a.radar_fine).replace(tzinfo=timezone.utc).timestamp() * 1000)
        else:
            fine = radar.ultimo_istante("SRI")
        fine -= fine % (radar.PASSO_MIN * 60000)
        istanti, frames, orig = radar.serie("SRI", fine, 12 * (ORE_STORIA + 1) + 1, ritaglio=(pc.LAT0, pc.LON0, MEZZO))
    except RuntimeError as e:
        sys.exit(f"Radar non disponibile: {e}")
    n_ok = sum(f is not None for f in frames)
    print(f"Radar-DPC: {n_ok} mosaici su {len(frames)}, ultimo alle {da_ms(istanti[-1]):%d %H:%M} UTC")
    if frames[-1] is None or n_ok < len(frames) // 2:
        sys.exit("Troppi mosaici radar mancanti: salto.")

    pil = a.pilota.upper()
    ciclo_txt = f"{pil} {a.ciclo[:8]} {a.ciclo[8:]}Z" if a.ciclo else pil
    banner = f"WRF 2 km ({ciclo_txt}) + Radar-DPC"
    ora_radar_loc = locale(da_ms(istanti[-1]))

    ris = verifica(frames, istanti, orig, wrf, LAT, LON, passo_km)
    if ris:
        foglio_verifica(ris, banner, z_f, lat_f, lon_f, a.out, ora_radar_loc, ciclo_txt)
        with open(Path(a.out) / "radar_verifica.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ciclo", "pilota", "ora_fine_utc", "mm_radar", "mm_wrf", "bagnato_radar_pct", "bagnato_wrf_pct",
                        "fss_0.5mm_10km", "fss_2mm_10km"])
            for r in ris:
                w.writerow([a.ciclo, a.pilota, f"{r['fine']:%Y-%m-%dT%H:%M}", f"{r['mm_radar']:.3f}", f"{r['mm_wrf']:.3f}",
                            f"{r['bagnato_radar']:.1f}", f"{r['bagnato_wrf']:.1f}"] + ["" if not np.isfinite(x) else f"{x:.3f}" for x in r["fss"]])
        for r in ris:
            print(f"{locale(r['fine']):%d %H}:00  radar {r['mm_radar']:.2f} mm  WRF {r['mm_wrf']:.2f} mm  "
                  f"accordo {r['fss'][0]:.2f}")
    else:
        print("Verifica: WRF non copre le ultime ore del radar (o il radar non copre l'area): salto.")

    p = foglio_avanti(frames, istanti, orig, wrf, LAT, LON, banner, z_f, lat_f, lon_f, a.out, ciclo_txt)
    for f in sorted(Path(a.out).glob("radar_*")):
        print("Creato:", f)
    return p


if __name__ == "__main__":
    main()
