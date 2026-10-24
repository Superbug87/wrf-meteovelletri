#!/usr/bin/env python3
"""
postprocess.py - dal dominio a 1 km di WRF alle stesse mappe a fogli di previsioni_castelli.py.

  python wrf/postprocess.py --uscite cartella_con_wrfout --out mappe [--ciclo 2026101600]

Legge wrfout_d03_* (e wrfxtrm_d03_*, se ci sono, per le raffiche), calcola le grandezze che WRF non
scrive già pronte (vedi diagnostiche.py: temperature a 925 e 850 hPa, zero termico, nuvolosità, CAPE e CIN,
shear, SRH, indici di rischio, raffiche, neve; qui pressione al livello del mare, umidità, pioggia),
le ricampiona su una griglia regolare lat/lon di ~0,5 km e disegna un'immagine per variabile.

Stime, da dichiarare sempre:
  - raffica = vento massimo orario (diagnostica di WRF) + termine turbolento tipo ECMWF (7,71 u* f)
  - CAPE/CIN = parcelle di superficie sollevate a passi (non è wrf-python); MU = la più instabile, ML = strato 100 hPa
  - rischio fulmini/supercelle/tornado = indici (LPI e correnti ascendenti, SCP, STP), non probabilità
"""
import argparse
import glob
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["TZ"] = "Europe/Rome"      # datetime.now() delle funzioni di previsioni_castelli = ora locale
time.tzset()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from netCDF4 import Dataset, chartostring  # noqa: E402
from scipy.interpolate import LinearNDInterpolator  # noqa: E402
from scipy.spatial import Delaunay  # noqa: E402

import diagnostiche as dg  # noqa: E402
import previsioni_castelli as pc  # noqa: E402
from orarie import disegna_orarie  # noqa: E402

G0, RD, CP, EPS = dg.G0, dg.RD, dg.CP, dg.EPS
KAPPA = dg.KAPPA
FATTORE_RAFFICA = 1.4                  # solo se mancano UST/HFX/PBLH (vecchio metodo)
FATTORE_TURBOLENZA = float(os.environ.get("WRF_FATTORE_TURBOLENZA", "1.0"))   # taratura del termine turbolento delle raffiche
p_sat, r_sat, td_da_e = dg.p_sat, dg.r_sat, dg.td_da_e


# ----------------------------------------------------------------------------
# Lettura di WRF
# ----------------------------------------------------------------------------
def tempi_file(ds):
    return [datetime.strptime(str(s).strip(), "%Y-%m-%d_%H:%M:%S") for s in chartostring(ds["Times"][:])]


def _leggi(ds, nome, i):
    """Un campo del file all'istante i (float64), o None se WRF non lo ha scritto."""
    if nome not in ds.variables:
        return None
    return np.asarray(ds[nome][i], dtype=np.float64)


def elabora_istante(percorso, i):
    """
    Un istante di un wrfout: ritorna (tempo UTC, campi derivati 2D, campi statici).
    Un istante per volta: un campo 3D a 2 km pesa 3-4 MB per variabile e il file ha 17 istanti.
    """
    with Dataset(percorso) as ds:
        ds.set_auto_mask(False)
        L = lambda nome: _leggi(ds, nome, i)
        tempo = tempi_file(ds)[i]

        T2, Q2, PSFC, hgt = L("T2"), L("Q2"), L("PSFC"), L("HGT")
        U10, V10 = L("U10"), L("V10")
        cosa, sina = L("COSALPHA"), L("SINALPHA")
        ny, nx = T2.shape
        ue10 = U10 * cosa - V10 * sina                 # da vento "della griglia" a est/nord
        ve10 = V10 * cosa + U10 * sina
        out = {"ue": ue10, "ve": ve10, "rain": L("RAINC") + L("RAINNC"), "T2": T2}
        if "SNOWNC" in ds.variables:
            out["snow"] = L("SNOWNC")

        e = Q2 * PSFC / (EPS + Q2)
        out["Td2"] = np.minimum(td_da_e(e), T2)
        # pressione al livello del mare (riduzione barometrica con temperatura media dello strato)
        tv = T2 * (1 + 0.61 * Q2)
        out["slp"] = PSFC * np.exp(G0 * hgt / (RD * (tv + 0.0065 * hgt / 2))) / 100.0

        # grandezze 3D, forma (nz, ny, nx); le colonne per diagnostiche.py sono (ny*nx, nz)
        p = L("P") + L("PB")
        qv = L("QVAPOR")
        th = L("T") + 300.0
        if "USE_THETA_M" in ds.ncattrs() and int(ds.getncattr("USE_THETA_M")) == 1:
            th = th / (1 + 1.61 * qv)
        tk = th * (p / 1e5) ** KAPPA
        ph = (L("PH") + L("PHB")) / G0
        z = 0.5 * (ph[:-1] + ph[1:])
        nz = p.shape[0]
        col = lambda a: a.reshape(nz, -1).T
        P_, T_, W_, Z_ = col(p), col(tk), col(qv), col(z)
        quanti = (ny, nx)

        out["T925"] = dg.a_pressione(P_, T_, 92500.0, T_[:, 0]).reshape(quanti)
        out["T850"] = dg.a_pressione(P_, T_, 85000.0, T_[:, 0]).reshape(quanti)
        out["zero_t"] = dg.zero_termico(T_, Z_).reshape(quanti)

        cf = L("CLDFRA")
        if cf is not None:
            for nome, a in dg.nuvolosita(col(cf), P_, PSFC.ravel()).items():
                out[nome] = a.reshape(quanti)

        par = dg.parcelle(P_, T_, W_, Z_)
        # vento in quota: da U e V (sfalsati) ai punti di massa, ruotati su est/nord
        Ust, Vst = L("U"), L("V")
        u3 = 0.5 * (Ust[:, :, :-1] + Ust[:, :, 1:])
        v3 = 0.5 * (Vst[:, :-1, :] + Vst[:, 1:, :])
        ue3, ve3 = u3 * cosa - v3 * sina, v3 * cosa + u3 * sina
        Uq, Vq = dg.vento_a_quote(col(ue3), col(ve3), Z_ - hgt.reshape(-1, 1), ue10.ravel(), ve10.ravel())
        sh = dg.shear_srh(Uq, Vq)

        cape_ml = par["cape_ml"]
        out["cape_mu"] = par["cape_mu"].reshape(quanti)
        out["cape_ml"] = cape_ml.reshape(quanti)
        out["cin"] = np.where(cape_ml >= 10.0, par["cin_ml"], 0.0).reshape(quanti)    # la CIN conta dove c'è CAPE
        for k in ("shear_01", "shear_03", "shear_06", "srh_01", "srh_03"):
            out[k] = sh[k].reshape(quanti)
        out["tornado"] = dg.indice_tornado(cape_ml, par["cin_ml"], par["lcl_ml"], sh["srh_01"], sh["shear_06"]).reshape(quanti)
        scp = dg.indice_supercella(par["cape_mu"], par["cin_mu"], sh["srh_03"], sh["shear_06"]).reshape(quanti)
        uh = L("UP_HELI_MAX")                    # massimo orario dell'elicità delle correnti ascendenti (WRF, nwp_diagnostics)
        out["supercelle"] = scp if uh is None else np.maximum(scp, np.clip(uh, 0.0, None) / 75.0)
        f = dg.indice_fulmini(L("W_UP_MAX"), L("GRPL_MAX"), L("LPI"))
        if f is not None:
            out["fulmini"] = f

        # raffica: vento medio massimo dell'ora + termine turbolento
        ust, pblh, hfx = L("UST"), L("PBLH"), L("HFX")
        if ust is not None and pblh is not None and hfx is not None:
            u_med = L("WSPD10MAX")
            u_med = np.hypot(ue10, ve10) if u_med is None else np.maximum(u_med, np.hypot(ue10, ve10))
            out["G"] = dg.raffica_turbolenta(u_med, ust, pblh, hfx, T2, PSFC, FATTORE_TURBOLENZA)

        statici = {"lat": L("XLAT"), "lon": L("XLONG"), "hgt": hgt} if i == 0 else None
    return tempo, {k: np.asarray(a, dtype=np.float32) for k, a in out.items()}, statici


def _lavoro(args):
    return elabora_istante(*args)


def raffiche_da_xtrm(cartella):
    """Massimo orario del vento a 10 m dai file wrfxtrm_d03_* (dict istante -> campo), o {}."""
    res = {}
    for f in sorted(glob.glob(os.path.join(cartella, "**", "wrfxtrm_d03_*"), recursive=True)):
        with Dataset(f) as ds:
            ds.set_auto_mask(False)
            nome = next((n for n in ("SPDUV10MAX", "WSPD10MAX") if n in ds.variables), None)
            if nome is None:
                continue
            for t, campo in zip(tempi_file(ds), np.asarray(ds[nome][:], dtype=np.float64)):
                res[t] = campo
    return res


def carica(cartella, processi=None):
    files = sorted(glob.glob(os.path.join(cartella, "**", "wrfout_d03_*"), recursive=True))
    if not files:
        sys.exit(f"Nessun file wrfout_d03_* in {cartella}")
    lavori = []
    for f in files:
        with Dataset(f) as ds:
            n = len(ds.dimensions["Time"])
        print("leggo", Path(f).name, f"({n} istanti)", flush=True)
        lavori += [(f, i) for i in range(n)]
    processi = processi or max(1, min(4, os.cpu_count() or 1))
    frames, statici = {}, None
    if processi > 1:
        with ProcessPoolExecutor(max_workers=processi) as pool:
            risultati = list(pool.map(_lavoro, lavori))
    else:
        risultati = [_lavoro(x) for x in lavori]
    for t, campi, st in risultati:                    # al riavvio il primo istante è doppio: vale l'ultimo
        statici = statici or st
        frames[t] = campi
    tempi = sorted(frames)
    chiavi = set.intersection(*(set(frames[t]) for t in tempi))
    dati = {k: np.stack([frames[t][k] for t in tempi]) for k in sorted(chiavi)}
    return tempi, dati, statici, raffiche_da_xtrm(cartella)


# ----------------------------------------------------------------------------
# Dal reticolo di WRF alla griglia regolare delle mappe
# ----------------------------------------------------------------------------
def ricampiona(campi, statici, lat_f, lon_f):
    punti = np.column_stack([statici["lon"].ravel(), statici["lat"].ravel()])
    tri = Delaunay(punti)
    LON, LAT = np.meshgrid(lon_f, lat_f)
    out = {}
    for nome, a in campi.items():
        nt = a.shape[0]
        f = LinearNDInterpolator(tri, a.reshape(nt, -1).T)
        r = np.moveaxis(f(LON, LAT), -1, 0)
        if np.isnan(r).any():
            sys.exit(f"'{nome}': la griglia delle mappe esce dal dominio a 1 km di WRF")
        out[nome] = r
    z = LinearNDInterpolator(tri, statici["hgt"].ravel())(LON, LAT)
    return out, np.clip(z, 0, None)


def _chiave_wu(segreto):
    """Chiave WU da usare per le richieste: il segreto, o una trovata nella pagina pubblica di una stazione se WU lo rifiuta
    (strumenti/wu_chiave.py). Non solleva mai eccezioni: nel peggiore dei casi resta il segreto."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strumenti"))
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from wu_chiave import chiave_funzionante
        return chiave_funzionante(segreto)
    except Exception:
        return segreto


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uscite", required=True, help="cartella con i wrfout_d03_* (anche in sottocartelle)")
    ap.add_argument("--out", default="mappe")
    ap.add_argument("--ciclo", default="", help="AAAAMMGGHH del ciclo usato, solo per il titolo")
    ap.add_argument("--pilota", default="icon2i", choices=["icon2i", "ecmwf", "gfs", "iconeu"], help="modello globale usato, solo per il titolo")
    ap.add_argument("--n", type=int, default=80, help="punti per lato della griglia delle mappe (80 = una cella ogni 0,5 km)")
    ap.add_argument("--senza-stazioni", action="store_true")
    ap.add_argument("--max-stazioni", type=int, default=60)
    ap.add_argument("--senza-orarie", action="store_true", help="non disegnare le immagini orarie (una per ora e variabile)")
    ap.add_argument("--da-ora", default="", help="ora locale ISO da cui partire (default: l'ora attuale)")
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)

    tempi_utc, dati, statici, xtrm = carica(a.uscite)
    roma = ZoneInfo("Europe/Rome")
    tempi = [t.replace(tzinfo=timezone.utc).astimezone(roma).replace(tzinfo=None) for t in tempi_utc]

    # raffica: dal metodo turbolento (u*, flusso di calore, strato limite) se WRF li ha scritti, altrimenti x 1,4
    vento = np.hypot(dati["ue"], dati["ve"])
    G = None
    metodo_raffiche = "turbolento"
    if "G" in dati:
        G = np.maximum(dati["G"], vento)
    elif xtrm:
        mx = np.stack([xtrm.get(t, np.zeros_like(vento[0])) for t in tempi_utc])
        G = np.maximum(FATTORE_RAFFICA * mx, vento)
        metodo_raffiche = "fattore"
    # pioggia oraria (dalle somme cumulate) e T in °C
    P = np.clip(np.diff(dati["rain"], axis=0, prepend=dati["rain"][:1]), 0, None)
    campi_nativi = {
        "T": dati["T2"] - 273.15, "Td": dati["Td2"] - 273.15, "T925": dati["T925"] - 273.15, "T850": dati["T850"] - 273.15,
        "p": dati["slp"], "u": dati["ue"], "v": dati["ve"], "P": P, "zero_t": dati["zero_t"],
    }
    for k in ("cape_mu", "cape_ml", "cin", "shear_01", "shear_03", "shear_06", "srh_01", "srh_03", "tornado",
              "supercelle", "fulmini", "nubi_tot", "nubi_bassa", "nubi_media", "nubi_alta"):
        if k in dati:
            campi_nativi[k] = dati[k]
    if "snow" in dati:                                   # neve fresca oraria (cm) dalla precipitazione solida
        solida = np.clip(np.diff(dati["snow"], axis=0, prepend=dati["snow"][:1]), 0, None)
        campi_nativi["N"] = dg.neve_cm(solida, dati["T2"])
    if G is not None:
        campi_nativi["G"] = G

    lat_f, lon_f = pc.griglia_fine(a.n)
    c, z_f = ricampiona(campi_nativi, statici, lat_f, lon_f)
    c["RH"] = pc.umidita_relativa(c["T"], c["Td"])
    c["G"] = c.get("G")
    for k in ("P", "cape_mu", "cape_ml", "cin", "shear_01", "shear_03", "shear_06", "srh_01", "srh_03", "tornado",
              "supercelle", "fulmini", "nubi_tot", "nubi_bassa", "nubi_media", "nubi_alta", "N", "zero_t"):
        if k in c:
            c[k] = np.clip(c[k], 0, None)
    for k in ("nubi_tot", "nubi_bassa", "nubi_media", "nubi_alta"):
        if k in c:
            c[k] = np.clip(c[k], 0, 100)
    for n in (3, 6, 12, 24):
        c[f"P{n}"] = pc.somma_mobile(c["P"], n)
    if "N" in c:
        c["N6"], c["N24"] = pc.somma_mobile(c["N"], 6), pc.somma_mobile(c["N"], 24)

    # si parte dall'ora attuale (il modello è partito qualche ora fa)
    ora = datetime.fromisoformat(a.da_ora) if a.da_ora else datetime.now().replace(minute=0, second=0, microsecond=0)
    i0 = next((i for i, t in enumerate(tempi) if t >= ora), 0)
    tempi = tempi[i0:]
    tempi_utc = list(tempi_utc[i0:])
    c = {k: (v[i0:] if isinstance(v, np.ndarray) else v) for k, v in c.items()}
    print(f"{len(tempi)} ore da {tempi[0]:%a %d %H:%M} a {tempi[-1]:%a %d %H:%M} (ora locale)")

    pil = {"ICON2I": "ICON-2I", "ICONEU": "ICON-EU"}.get(a.pilota.upper(), a.pilota.upper())
    ciclo_txt = f"{pil} {a.ciclo[:8]} {a.ciclo[8:]}Z" if a.ciclo else pil
    banner = f"WRF by Meteo Velletri · 1 km · base {ciclo_txt}"
    raff_txt = ("Raffiche: stima, vento massimo orario più un termine turbolento tipo ECMWF (7,71 u* f)."
                if metodo_raffiche == "turbolento" else "Raffiche: stima, massimo orario del vento a 10 m x 1,4.")
    modello_txt = (f"WRF: modello fisico con tre domini annidati (9, 3 e 1 km) alimentato da {pil}; le mappe "
                   f"sono ricampionate a {2 * pc.MEZZO_LAT * 111.32 / (a.n - 1):.1f} km. {raff_txt} "
                   "CAPE e CIN: parcelle sollevate dal modello (MU: la più instabile nei primi 300 hPa; ML: strato di 100 hPa). "
                   "Fulmini, supercelle e tornado sono indici di ambiente favorevole (LPI e correnti ascendenti, SCP, STP): "
                   "non sono probabilità e non sono tarati sull'Italia.")

    fonte = {"icon2i": "ICON-2I, Agenzia ItaliaMeteo con Arpae-SIMC (CC BY 4.0), modificati", "ecmwf": "ECMWF IFS open data (CC BY 4.0), modificati"}.get(a.pilota, "NOAA/NCEP GFS")
    if list(Path(a.uscite).rglob("sst_usata.txt")):
        fonte += "; mare: MUR SST (NASA JPL)"
    modello_txt += f" Dati di partenza: {fonte}."

    # se le stazioni sono già state assimilate da WRF (nudging) non si corregge una seconda volta
    file_obs = sorted(Path(a.uscite).rglob("obs_fdda_end.txt"))
    nudging = bool(file_obs) and bool(a.ciclo)
    if nudging:
        fine_utc = (datetime.strptime(a.ciclo, "%Y%m%d%H") + timedelta(minutes=int(file_obs[0].read_text().split()[0])))
        fine_loc = fine_utc.replace(tzinfo=timezone.utc).astimezone(roma)
        # obs_fonti.txt (se c'e'): "aeronautica=7 regione=45 wu=120"; senza, erano solo Wunderground
        fonti = {}
        for f_ in Path(a.uscite).rglob("obs_fonti.txt"):
            fonti = dict(x.split("=") for x in f_.read_text().split() if "=" in x)
            break
        nomi = [n for k, n in (("regione", "Regione Lazio"), ("aeronautica", "Aeronautica Militare"), ("wu", "Wunderground"))
                if k in fonti] or ["Wunderground"]
        banner += " + stazioni " + ("WU" if nomi == ["Wunderground"] else "ufficiali" if "Wunderground" not in nomi else "ufficiali e WU") + " assimilate"
        modello_txt += (f" Le stazioni {', '.join(nomi[:-1]) + ' e ' + nomi[-1] if len(nomi) > 1 else nomi[0]} sono state "
                        f"assimilate nel modello (temperatura e umidità, nudging) fino alle {fine_loc:%H:%M}; "
                        f"dopo il modello prosegue libero.")
        print(f"Stazioni assimilate in WRF fino alle {fine_loc:%H:%M} (ora locale): nessuna correzione successiva.")

    if not nudging and not a.senza_stazioni and os.environ.get("WU_API_KEY"):
        chiave = os.environ["WU_API_KEY"]
        chiave = _chiave_wu(chiave)
        pc.SEGRETI.append(chiave)
        try:
            scelte = pc.diradamento(pc.cerca_stazioni(chiave), a.max_stazioni)
            c, info = pc.correggi_con_stazioni(c, tempi, pc.leggi_stazioni(chiave, scelte), z_f, lat_f, lon_f)
            print(info["testo"])
            if info["n"] >= 3:
                banner += f" + {info['n']} stazioni WU"
                modello_txt += " " + info["nota"]
        except Exception as e:                            # la correzione è facoltativa
            print("Stazioni Wunderground non disponibili:", pc._pulisci(str(e)), file=sys.stderr)

    creati = pc.disegna_fogli(c, z_f, lat_f, lon_f, tempi, banner, modello_txt, a.out)
    for f in creati:
        print("Creato:", f)
    if not a.senza_orarie:
        disegna_orarie(c, z_f, lat_f, lon_f, tempi, tempi_utc, a.ciclo, a.out, pil)

    iy = int(np.abs(lat_f - pc.STAZIONE[1]).argmin())
    ix = int(np.abs(lon_f - pc.STAZIONE[2]).argmin())
    print(f"Velletri: T {c['T'][:, iy, ix].min():.0f}-{c['T'][:, iy, ix].max():.0f} °C, "
          f"vento max {np.hypot(c['u'], c['v'])[:, iy, ix].max() * 3.6:.0f} km/h, "
          f"pioggia 6 h max {c['P6'][:, iy, ix].max():.1f} mm, CAPE max {c['cape_mu'][:, iy, ix].max():.0f} J/kg")


if __name__ == "__main__":
    main()
