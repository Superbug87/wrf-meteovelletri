#!/usr/bin/env python3
"""
scarica_icon2i.py - dati di partenza e di bordo per WRF da ICON-2I (2,2 km, Agenzia ItaliaMeteo / MeteoHub).

  scarica_icon2i.py --ciclo 2026101712 --ore 48 --passo 6 --out icon2i

Cosa fa, per ogni istante (da +0 a +ore ogni `passo` ore):
  1. entra in MeteoHub con l'account (segreti MH_EMAIL e MH_PASSWORD) e chiede i campi del run, una richiesta per
     istante (le richieste su molti istanti insieme restano in coda per minuti; quelle su un istante solo rispondono in secondi);
  2. scarica i campi sui livelli del modello (T, umidità specifica, vento, pressione; 65 livelli) e quelli di
     superficie e suolo; ritaglia l'area che serve a WRF;
  3. porta i livelli del modello su livelli di pressione (interpolazione in ln p) e riscrive tutto in GRIB2 con gli
     stessi codici dei file ECMWF preparati da wrf/scarica_ecmwf.py: così si riusa la stessa Vtable (wrf/Vtable.ECMWF_OD),
     ungrib, metgrid e real senza cambiare nulla. Uscita: icon2i/icon2i.tHHz.fXXX.grib2.

Note:
  - ICON-2I dà la griglia regolare lat/lon (0,025° x 0,02°); i livelli del modello sono "generalVerticalLayer" 1..65
    dall'alto al basso; le quote vengono dalla quota dei 66 livelli "a metà" (l'ultimo è il suolo).
  - Umidità del suolo: ICON-2I la dà per strati (0-1, 1-3, 3-9, 9-27, 27-81, 81-243, 243-729 cm) in kg m-2: si converte in
    frazione di volume e si media sugli strati di Noah (0-7, 7-28, 28-100, 100-289 cm). La temperatura del suolo si
    interpola in profondità. Sono stime: il modello di suolo di ICON (TERRA) non è quello di WRF (Noah).
  - Il mare: la temperatura "skin" sul mare è la SST di ICON-2I; con wrf/scarica_sst.py la si sostituisce con MUR.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

BASE = "https://meteohub.agenziaitaliameteo.it"
MODELLO, SUPERFICIE = "ICON_2I_MODEL_LEVELS", "ICON_2I_SURFACE_PRESSURE_LEVELS"
TIPO_MOD = 150           # tipo di livello GRIB2 dei livelli del modello (generalVerticalLayer); la prova lo cambia
# area ritagliata: un po' più grande del dominio esterno più grande (Lazio: 37,6-46,0 N, 7,0-18,4 E)
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 36.6, 47.0, 6.0, 19.6
# livelli di pressione (hPa) su cui si riscrive; sopra il top del modello non si scrive
LIVELLI_HPA = [1000, 975, 950, 925, 900, 875, 850, 825, 800, 775, 750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500,
               475, 450, 425, 400, 375, 350, 325, 300, 275, 250, 225, 200, 175, 150, 125, 100, 70, 50]
RD, G0, CP, GAMMA = 287.04, 9.80665, 1004.0, 0.0065
STRATI_NOAH_CM = [(0, 7), (7, 28), (28, 100), (100, 289)]
STRATI_ICON_CM = [(0, 1), (1, 3), (3, 9), (9, 27), (27, 81), (81, 243), (243, 729)]
PROFONDITA_T_CM = [0, 2, 6, 18, 54, 162, 486, 1458]


# ----------------------------------------------------------------------------
# MeteoHub
# ----------------------------------------------------------------------------
class Hub:
    def __init__(self):
        email, pw = os.environ.get("MH_EMAIL", ""), os.environ.get("MH_PASSWORD", "")
        if not email or not pw:
            sys.exit("Mancano i segreti MH_EMAIL e MH_PASSWORD (account MeteoHub).")
        r = requests.post(f"{BASE}/auth/login", json={"username": email, "password": pw}, timeout=60)
        if r.status_code != 200:
            sys.exit(f"Login a MeteoHub rifiutato (HTTP {r.status_code}).")
        j = r.json()
        self.h = {"Authorization": f"Bearer {j if isinstance(j, str) else j.get('token')}"}
        self.campi = {}

    def elenco(self, dataset):
        if dataset not in self.campi:
            r = requests.get(f"{BASE}/api/fields", params={"datasets": dataset}, headers=self.h, timeout=120)
            r.raise_for_status()
            self.campi[dataset] = r.json()["items"]
        return self.campi[dataset]

    def cancella(self, rid):
        try:
            requests.delete(f"{BASE}/api/requests/{rid}", headers=self.h, timeout=60)
        except requests.RequestException:
            pass

    def estrai(self, dataset, ciclo, filtri, nome, attesa=1200):
        """Una richiesta; ritorna i byte del GRIB (vuoti se non c'è nulla)."""
        t = ciclo.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        corpo = {"request_name": nome, "dataset_names": [dataset], "only_reliable": False,
                 "reftime": {"from": t, "to": t}, "filters": filtri}
        for tentativo in range(4):
            r = requests.post(f"{BASE}/api/data", json=corpo, headers=self.h, timeout=120)
            if r.status_code == 202:
                break
            print(f"[{nome}] POST HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            time.sleep(10 * (tentativo + 1))
        else:
            sys.exit(f"[{nome}] richiesta rifiutata")
        rid = r.json()["request_id"]
        t0 = time.time()
        while time.time() - t0 < attesa:
            time.sleep(3 if time.time() - t0 < 60 else 10)
            for v in requests.get(f"{BASE}/api/requests", headers=self.h, timeout=60).json():
                if v["id"] != rid:
                    continue
                if v["status"] == "SUCCESS":
                    d = requests.get(f"{BASE}/api/data/{v['fileoutput']}", headers=self.h, timeout=1800)
                    d.raise_for_status()
                    self.cancella(rid)             # lo spazio dell'account è di circa 1 GB: si libera subito
                    return d.content
                if v["status"] in ("FAILURE", "REVOKED"):
                    self.cancella(rid)
                    if "empty" in v.get("error_message", ""):
                        return b""
                    sys.exit(f"[{nome}] richiesta fallita: {v.get('error_message')}")
        sys.exit(f"[{nome}] ancora in corso dopo {attesa} s")


def scegli(voci, test):
    return [v for v in voci if test(v)]


def tempo(voci, ora):
    """Il tipo di tempo giusto: analisi all'ora 0, previsione istantanea a +ora."""
    d = "Analysis or observation, istantaneous value" if ora == 0 else f"Forecast at t+{60 * ora} minute, instantaneous value"
    return scegli(voci, lambda t: t["desc"] == d)


# ----------------------------------------------------------------------------
# GRIB
# ----------------------------------------------------------------------------
def messaggi(dati):
    import eccodes as ec

    pos = 0
    while True:
        i = dati.find(b"GRIB", pos)
        if i < 0:
            return
        lung = int.from_bytes(dati[i + 8:i + 16], "big")
        yield ec.codes_new_from_message(dati[i:i + lung])
        pos = i + lung


def leggi(dati):
    """Ritorna (lista di (chiave, valori 2D ritagliati), lats, lons, modello di messaggio).
    chiave = (disciplina, categoria, numero, tipo livello 1, valore 1, tipo livello 2, valore 2)."""
    import eccodes as ec

    out, lats, lons, modello = [], None, None, None
    for h in messaggi(dati):
        ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
        la1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees")
        lo1 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
        di, dj = ec.codes_get(h, "iDirectionIncrementInDegrees"), ec.codes_get(h, "jDirectionIncrementInDegrees")
        if lats is None:
            la = la1 + dj * np.arange(nj) if ec.codes_get(h, "jScansPositively") else la1 - dj * np.arange(nj)
            lo = lo1 + di * np.arange(ni)
            ij = np.where((la >= LAT_MIN) & (la <= LAT_MAX))[0]
            ii = np.where((lo >= LON_MIN) & (lo <= LON_MAX))[0]
            j0, j1, i0, i1 = ij.min(), ij.max() + 1, ii.min(), ii.max() + 1
            lats, lons = la[j0:j1], lo[i0:i1]
            if lats[0] < lats[-1]:               # si lavora da nord a sud, come ECMWF
                lats = lats[::-1]
                inverti = True
            else:
                inverti = False
            modello = ec.codes_clone(h)
        v = ec.codes_get_values(h).reshape(nj, ni)[j0:j1, i0:i1]
        if inverti:
            v = v[::-1]
        g = lambda k: ec.codes_get_long(h, k)
        rif = datetime.strptime(f"{g('dataDate'):08d}{g('dataTime'):04d}", "%Y%m%d%H%M")
        val = datetime.strptime(f"{g('validityDate'):08d}{g('validityTime'):04d}", "%Y%m%d%H%M")
        lead = int(round((val - rif).total_seconds() / 3600.0))
        chiave = (g("discipline"), g("parameterCategory"), g("parameterNumber"), g("typeOfFirstFixedSurface"),
                  g("scaledValueOfFirstFixedSurface") if g("scaleFactorOfFirstFixedSurface") == 0 else
                  g("scaledValueOfFirstFixedSurface") * 10.0 ** -g("scaleFactorOfFirstFixedSurface"),
                  g("typeOfSecondFixedSurface"),
                  g("scaledValueOfSecondFixedSurface") if g("typeOfSecondFixedSurface") < 255 else 0, lead)
        out.append((chiave, v.astype("float32")))
        ec.codes_release(h)
    return out, lats, lons, modello


# ----------------------------------------------------------------------------
# Fisica
# ----------------------------------------------------------------------------
def rh_da_q(q, t, p):
    e = q * p / (0.622 + 0.378 * q)
    tc = t - 273.15
    ew = 611.2 * np.exp(17.62 * tc / (243.12 + tc))
    ei = 611.2 * np.exp(22.46 * tc / (272.62 + tc))
    a = np.clip((t - 250.16) / (273.16 - 250.16), 0.0, 1.0) ** 2            # miscela acqua-ghiaccio come nell'IFS
    return np.clip(100.0 * e / (a * ew + (1 - a) * ei), 0.0, 100.0)


def a_pressione(p3, campi, z3, ps, livelli_pa):
    """p3, T, q, u, v, z: (nz, ny, nx) dall'alto al basso. Ritorna dict livello -> (T, RH, U, V, Z)."""
    T3, Q3, U3, V3 = campi
    nz = p3.shape[0]
    ln = np.log(p3)
    out = {}
    for pl in livelli_pa:
        conta = (p3 < pl).sum(axis=0)                          # livelli sopra (pressione minore) il livello cercato
        k1 = np.clip(conta, 1, nz - 1)[None]
        k0 = k1 - 1
        a0, a1 = (np.take_along_axis(ln, k, 0)[0] for k in (k0, k1))
        w = np.clip((np.log(pl) - a0) / (a1 - a0), 0.0, 1.0)
        res = []
        for A in (T3, Q3, U3, V3, z3):
            x0, x1 = (np.take_along_axis(A, k, 0)[0] for k in (k0, k1))
            res.append(x0 + (x1 - x0) * w)
        T, Q, U, V, Z = res
        sotto = pl > p3[-1]                                    # il livello sta sotto il livello più basso del modello
        if sotto.any():
            pb, tb = p3[-1], T3[-1]
            Te = tb * (pl / pb) ** (RD * GAMMA / G0)
            Ze = z3[-1] - (0.5 * (Te + tb)) * RD / G0 * np.log(pl / pb)
            T, Z = np.where(sotto, Te, T), np.where(sotto, Ze, Z)
            U, V, Q = np.where(sotto, U3[-1], U), np.where(sotto, V3[-1], V), np.where(sotto, Q3[-1], Q)
        out[pl] = (T, rh_da_q(np.maximum(Q, 0.0), T, np.float32(pl)), U, V, Z)
    return out


def media_strati(strati_icon_kgm2, spessori_cm, destinazione):
    """Frazione di volume media negli strati di Noah da strati ICON (kg m-2 per strato)."""
    out = []
    for a, b in destinazione:
        peso, acqua = 0.0, 0.0
        for (s0, s1), w in zip(STRATI_ICON_CM, strati_icon_kgm2):
            ov = max(0.0, min(b, s1) - max(a, s0))
            if ov > 0:
                acqua = acqua + w * (ov / (s1 - s0))
                peso += ov
        if peso == 0:
            raise ValueError(f"nessuno strato ICON copre {a}-{b} cm")
        out.append(np.clip(acqua / (peso * 10.0), 0.02, 0.6))        # kg m-2 = mm: frazione = mm / (cm x 10)
    return out


def t_suolo(temp_per_profondita, destinazione):
    prof = np.array(PROFONDITA_T_CM, float)
    out = []
    for a, b in destinazione:
        m = 0.5 * (a + b)
        k = int(np.clip(np.searchsorted(prof, m) - 1, 0, len(prof) - 2))
        w = (m - prof[k]) / (prof[k + 1] - prof[k])
        out.append(temp_per_profondita[k] * (1 - w) + temp_per_profondita[k + 1] * w)
    return out


# ----------------------------------------------------------------------------
# Scrittura
# ----------------------------------------------------------------------------
def scrivi(modello, f, lats, lons, ciclo, ora, disc, cat, num, tipo1, v1, valori, tipo2=None, sc2=0, v2=0, sc1=0, bits=16):
    import eccodes as ec

    m = ec.codes_clone(modello)
    ec.codes_set_long(m, "discipline", disc)
    ec.codes_set_long(m, "parameterCategory", cat)
    ec.codes_set_long(m, "parameterNumber", num)
    ec.codes_set_long(m, "typeOfFirstFixedSurface", tipo1)
    ec.codes_set_long(m, "scaleFactorOfFirstFixedSurface", sc1)
    ec.codes_set_long(m, "scaledValueOfFirstFixedSurface", int(v1))
    if tipo2 is None:
        ec.codes_set_long(m, "typeOfSecondFixedSurface", 255)
    else:
        ec.codes_set_long(m, "typeOfSecondFixedSurface", tipo2)
        ec.codes_set_long(m, "scaleFactorOfSecondFixedSurface", sc2)
        ec.codes_set_long(m, "scaledValueOfSecondFixedSurface", int(v2))
    ec.codes_set_long(m, "indicatorOfUnitOfTimeRange", 1)
    ec.codes_set_long(m, "forecastTime", int(ora))
    ec.codes_set_long(m, "dataDate", int(ciclo.strftime("%Y%m%d")))
    ec.codes_set_long(m, "dataTime", ciclo.hour * 100)
    ec.codes_set(m, "packingType", "grid_simple")
    ec.codes_set(m, "bitsPerValue", bits)
    ny, nx = valori.shape
    ec.codes_set_long(m, "jScansPositively", 0)
    ec.codes_set_long(m, "Ni", nx)
    ec.codes_set_long(m, "Nj", ny)
    ec.codes_set(m, "latitudeOfFirstGridPointInDegrees", float(lats[0]))
    ec.codes_set(m, "latitudeOfLastGridPointInDegrees", float(lats[-1]))
    ec.codes_set(m, "longitudeOfFirstGridPointInDegrees", float(lons[0]))
    ec.codes_set(m, "longitudeOfLastGridPointInDegrees", float(lons[-1]))
    ec.codes_set_values(m, np.asarray(valori, float).ravel())
    ec.codes_write(m, f)
    ec.codes_release(m)


def maschera_terra(hub, ciclo):
    """Land-sea mask (1 = terra): campo statico, si chiede senza tipo di tempo."""
    fs = hub.elenco(SUPERFICIE)
    prod = scegli(fs["product"], lambda p: p["desc"].startswith("Land cover"))
    campi, _, _, _ = leggi(hub.estrai(SUPERFICIE, ciclo, {"product": prod}, "icon2i_lsm"))
    if not campi:
        sys.exit("Manca la maschera terra-mare di ICON-2I.")
    return campi[0][1]


def estrai_con_attesa(hub, dataset, ciclo, filtri, nome, tentativi=8, pausa=150):
    """Come estrai, ma se non c'e' ancora nulla (ciclo in arrivo su MeteoHub) aspetta e ritenta."""
    for i in range(tentativi):
        dati = hub.estrai(dataset, ciclo, filtri, nome)
        if dati:
            return dati
        print(f"[{nome}] nessun dato (ciclo ancora in arrivo?), riprovo fra {pausa} s ({i + 1}/{tentativi})", flush=True)
        time.sleep(pausa)
    sys.exit(f"[{nome}] nessun dato dopo {tentativi} tentativi")


def superficie_tutti(hub, ciclo, ore):
    """Campi di superficie e suolo per tutti gli istanti in una sola richiesta (le richieste sono contate: 1 ogni ora si esaurisce)."""
    fs = hub.elenco(SUPERFICIE)
    ps_ = lambda desc: scegli(fs["product"], lambda p: p["desc"] == desc)
    descr = {"Analysis or observation, istantaneous value" if h == 0 else f"Forecast at t+{60 * h} minute, instantaneous value" for h in ore}
    filtri = {"product": ps_("Temperature (K)") + ps_("Dewpoint temperature (K)") + ps_("u-component of wind (m/s)")
              + ps_("v-component of wind (m/s)") + ps_("Pressure (Pa)") + ps_("Pressure reduced to MSL (Pa)")
              + ps_("Snow depth water equivalent (kg m-2)") + ps_("Soil temperature (K)")
              + ps_("Column-integrated soil moisture (kg m-2)"),
              "level": scegli(fs["level"], lambda l: "desc" in l and not l["desc"].startswith(("pl ", "102 ", "4 Level", "GRIB2S(192"))),
              "timerange": scegli(fs["timerange"], lambda t: t["desc"] in descr)}
    campi, _, _, modello = leggi(estrai_con_attesa(hub, SUPERFICIE, ciclo, filtri, f"icon2i_{ciclo:%Y%m%d%H}_sup"))
    return campi, modello        # il messaggio modello e' di superficie (senza coordinate verticali): si clona per scrivere


def un_istante(hub, ciclo, ora, out, z3=None, lsm=None, sup_tutti=None, modello=None):
    """Scarica, converte e scrive l'istante +ora. Ritorna z3 (quota dei 65 livelli del modello sul mare, fissa nel tempo)."""
    nome = f"icon2i_{ciclo:%Y%m%d%H}_f{ora:03d}"
    # --- livelli del modello
    fm = hub.elenco(MODELLO)
    prod = lambda desc: scegli(fm["product"], lambda p: p["desc"] == desc)
    # i campi sui livelli pieni (T, q, u, v, p) sono "strati" tra i livelli a meta' k e k+1 (stile GRIB2D); la quota e' sui livelli a meta'
    liv_mod = scegli(fm["level"], lambda l: l.get("style") == "GRIB2D" and l.get("l1") == TIPO_MOD and l.get("l2") == TIPO_MOD
                     and l.get("value2") == l.get("value1") + 1 and 1 <= l["value1"] <= 65)
    prodotti = (prod("Temperature (K)") + prod("Specific humidity (kg/kg)") + prod("u-component of wind (m/s)")
                + prod("v-component of wind (m/s)") + prod("Pressure (Pa)"))
    livelli = liv_mod
    if ora == 0:          # la quota dei livelli (sul mare, uno strato per livello come gli altri campi) si chiede all'ora 0
        prodotti = prodotti + prod("Geometric height (m)")
    filtri = {"product": prodotti, "level": livelli, "timerange": tempo(fm["timerange"], ora)}
    campi, lats, lons, _ = leggi(estrai_con_attesa(hub, MODELLO, ciclo, filtri, nome + "_mod"))
    nz = 65

    def pila(num_par):
        cat, num = num_par
        d = {int(round(k[4])): v for k, v in campi if (k[1], k[2]) == (cat, num) and k[3] == TIPO_MOD and k[5] == TIPO_MOD}
        mancanti = [i for i in range(1, nz + 1) if i not in d]
        if mancanti:
            sys.exit(f"{nome}: mancano {len(mancanti)} livelli del campo {num_par} (primo: {mancanti[0]})")
        return np.stack([d[i] for i in range(1, nz + 1)])

    T3, Q3, U3, V3, P3 = pila((0, 0)), pila((1, 0)), pila((2, 2)), pila((2, 3)), pila((3, 0))
    # --- quota dei livelli (solo all'ora 0: non cambia nel tempo); e' la quota sul livello del mare dei 65 livelli pieni
    if ora == 0:
        z3 = pila((3, 6))
    # --- superficie e suolo (gia' scaricati per tutti gli istanti)
    sup = [(k, v) for k, v in sup_tutti if k[-1] == ora]
    if not sup:
        sys.exit(f"{nome}: nessun campo di superficie per +{ora} h")
    S = {}
    for k, v in sup:
        S.setdefault(k[:3], []).append((k, v))

    def uno(cod, tipo1, valore1=None, tipo2=None):
        for k, v in S.get(cod, []):
            if k[3] == tipo1 and (valore1 is None or abs(k[4] - valore1) < 1e-6) and (tipo2 is None or k[5] == tipo2):
                return v
        return None

    ps = uno((0, 3, 0), 1) if uno((0, 3, 0), 1) is not None else P3[-1]
    t2, td2, u10, v10 = uno((0, 0, 0), 103, 2), uno((0, 0, 6), 103, 2), uno((0, 2, 2), 103, 10), uno((0, 2, 3), 103, 10)
    pmsl, skin = uno((0, 3, 1), 101), uno((0, 0, 0), 1)       # pressione ridotta al mare = categoria 3, numero 1
    neve = uno((0, 1, 60), 1) if uno((0, 1, 60), 1) is not None else None
    terra = lsm
    for n, v in (("t2", t2), ("td2", td2), ("u10", u10), ("v10", v10), ("pmsl", pmsl), ("skin", skin)):
        if v is None:
            sys.exit(f"{nome}: manca il campo di superficie {n}; campi trovati: {sorted(S)}")
    # suolo
    ts = []
    for cm in PROFONDITA_T_CM:
        v = None
        for k, x in S.get((2, 3, 18), []):
            if k[3] == 106 and abs(k[4] - (cm / 100.0)) < 1e-6:
                v = x
        if v is None and cm == 0:
            v = skin
        if v is None:
            sys.exit(f"{nome}: manca la temperatura del suolo a {cm} cm")
        ts.append(v)
    strati_sm = []
    for a, b in STRATI_ICON_CM:
        v = None
        for k, x in S.get((2, 3, 20), []):
            if k[3] == 106 and k[5] == 106 and abs(k[4] - a / 100.0) < 1e-6 and k[6] == b:
                v = x
                break
        if v is None:
            sys.exit(f"{nome}: manca l'umidita' del suolo nello strato {a}-{b} cm")
        strati_sm.append(v)
    # --- pressione -> livelli
    liv = [p for p in LIVELLI_HPA if p * 100.0 >= P3[0].min() * 0.98]
    iso = a_pressione(P3, (T3, Q3, U3, V3), z3, ps, [p * 100.0 for p in liv])
    dest = Path(out) / f"icon2i.t{ciclo:%H}z.f{ora:03d}.grib2"
    with open(dest, "wb") as f:
        for pl in liv:
            T, RH, U, V, Z = iso[pl * 100.0]
            for (c, n, val) in ((0, 0, T), (1, 1, RH), (2, 2, U), (2, 3, V), (3, 5, Z)):
                scrivi(modello, f, lats, lons, ciclo, ora, 0, c, n, 100, pl * 100.0, val, bits=16 if c != 3 else 16)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 2, 2, 103, 10, u10)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 2, 3, 103, 10, v10)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 0, 0, 103, 2, t2)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 0, 6, 103, 2, td2)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 3, 0, 1, 0, ps)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 3, 0, 101, 0, pmsl)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 0, 17, 1, 0, skin)
        if terra is not None:
            scrivi(modello, f, lats, lons, ciclo, ora, 2, 0, 0, 1, 0, (terra >= 0.5).astype("float32"), bits=8)
        if neve is not None:
            scrivi(modello, f, lats, lons, ciclo, ora, 0, 1, 254, 1, 0, neve / 1000.0, bits=16)
        scrivi(modello, f, lats, lons, ciclo, ora, 0, 3, 4, 1, 0, G0 * (z3[-1] - 10.0))            # orografia (m2 s-2): il livello più basso sta a circa 10 m dal suolo
        for (a, b), v in zip(STRATI_NOAH_CM, t_suolo(ts, STRATI_NOAH_CM)):
            scrivi(modello, f, lats, lons, ciclo, ora, 2, 3, 18, 106, a, v, tipo2=106, sc2=2, v2=b, sc1=2)
        for (a, b), v in zip(STRATI_NOAH_CM, media_strati(strati_sm, None, STRATI_NOAH_CM)):
            scrivi(modello, f, lats, lons, ciclo, ora, 2, 0, 25, 106, a, v, tipo2=106, sc2=2, v2=b, sc1=2)
    print(f"ok +{ora:02d} h: {len(liv)} livelli di pressione, {dest.stat().st_size / 1e6:.1f} MB", flush=True)
    return z3


def ultimo_ciclo(hub):
    """L'ultimo ciclo (00 o 12 UTC) elencato da MeteoHub."""
    s = hub.elenco(MODELLO)["summarystats"]["e"]
    return datetime(s[0], s[1], s[2], s[3], tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ciclo", default="", help="AAAAMMGGHH (00 o 12 UTC); vuoto = l'ultimo ciclo di MeteoHub")
    ap.add_argument("--ore", type=int, default=48)
    ap.add_argument("--passo", type=int, default=6, help="ore tra un file e l'altro (bordi di WRF)")
    ap.add_argument("--out", default="icon2i")
    ap.add_argument("--solo-ciclo", action="store_true")
    a = ap.parse_args()
    hub = Hub()
    ciclo = datetime.strptime(a.ciclo, "%Y%m%d%H").replace(tzinfo=timezone.utc) if a.ciclo else ultimo_ciclo(hub)
    if a.solo_ciclo:
        print(f"{ciclo:%Y%m%d%H}")
        return
    Path(a.out).mkdir(parents=True, exist_ok=True)
    ore = list(range(0, a.ore + 1, a.passo))
    lsm = maschera_terra(hub, ciclo)
    sup, modello = superficie_tutti(hub, ciclo, ore)
    z3 = un_istante(hub, ciclo, 0, a.out, lsm=lsm, sup_tutti=sup, modello=modello)
    with ThreadPoolExecutor(2) as ex:                      # ogni richiesta pesa ~300 MB e lo spazio dell account e ~1 GB
        for _ in ex.map(lambda h: un_istante(hub, ciclo, h, a.out, z3, lsm, sup, modello), ore[1:]):
            pass


if __name__ == "__main__":
    main()
