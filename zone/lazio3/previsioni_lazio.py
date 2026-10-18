#!/usr/bin/env python3
"""
previsioni_lazio.py - mappe meteo sul Lazio per le prossime 48 ore.

Come funziona:
  1. scarica da Open-Meteo un modello meteo su una griglia grossolana 13x13 (~21 km, su 256 x 256 km)
  2. scarica le quote del terreno su una griglia fine (n x n, passo ~3 km)
  3. downscaling: interpola i campi sulla griglia fine e li corregge con la quota
       temperatura  -6.5 C/km
       punto di rugiada  -1.8 C/km
       vento 10 m   piu' forte in quota
  4. disegna 2 immagini verticali, pensate per essere lette da smartphone

Uso:
  python previsioni_lazio.py                  dati reali (serve internet)
  python previsioni_lazio.py --simula         dati INVENTATI, per provare codice e grafica
  python previsioni_lazio.py --ora 30         2 immagini di dettaglio (con le città) a +30 h da adesso
  python previsioni_lazio.py --modello icon_d2

Questo file è anche la libreria grafica di wrf/postprocess.py: la griglia delle mappe (LAT0, LON0, MEZZO_LAT,
MEZZO_LON) deve stare dentro il dominio a 2 km di WRF (wrf/dominio.py).

Senza --ora crea un'immagine per variabile (temperatura a 2 m e a 850 hPa, vento medio, raffiche,
pioggia 1/3/6 ore, pressione, umidita', CAPE) con un riquadro ogni 6 ore sulle prossime 48 ore.

Dipendenze:  pip install numpy scipy matplotlib requests
"""
import argparse
import hashlib
import json
import os
import re
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, to_rgb
from matplotlib.ticker import MaxNLocator
from scipy.interpolate import RectBivariateSpline

# ----------------------------------------------------------------------------
# Dominio delle mappe: quadrato di circa 256 km di lato su tutto il Lazio (40,67-42,97 N, 11,18-14,26 E)
# ----------------------------------------------------------------------------
LAT0, LON0 = 41.82, 12.72
MEZZO_LAT, MEZZO_LON = 1.15, 1.54      # 1,15° di latitudine = 128 km; 1,54° di longitudine a 41,8 N = 128 km
LAT_MIN, LAT_MAX = LAT0 - MEZZO_LAT, LAT0 + MEZZO_LAT
LON_MIN, LON_MAX = LON0 - MEZZO_LON, LON0 + MEZZO_LON
N_GROSSA = 13         # punti per lato della griglia del modello (uno ogni ~21 km)
ORE = 49              # ora 0 + 48 ore
PASSATO = 5           # ore passate scaricate solo per le somme della pioggia (3 e 6 ore)
GRAD_T = 0.0065       # C/m
GRAD_TD = 0.0018      # C/m

# Punto di riferimento (la stella ★ e il valore scritto nei riquadri): metti qui le coordinate della tua stazione
STAZIONE = ("Velletri", 41.686, 12.778)
PAESI = [   # città scritte sulle mappe di dettaglio e sulle orarie: posizioni approssimate, a mano
    ("Roma", 41.893, 12.483),
    ("Viterbo", 42.417, 12.105),
    ("Rieti", 42.404, 12.857),
    ("Latina", 41.467, 12.904),
    ("Frosinone", 41.640, 13.340),
    ("Terracina", 41.287, 13.249),
    ("Cassino", 41.490, 13.832),
]
LIVELLI_QUOTA = [500, 1000, 1500, 2000]      # linee grigie delle mappe (m)

# Palette di base (stessi colori in tutte le mappe, una tinta per variabile)
SFONDO, INK, SEC, TENUE, LINEA = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#c3c2b7"
ARANCIO, VIOLA, VERDE = "#eb6834", "#6250d6", "#008300"
BLU, AQUA, ROSSO = "#2a78d6", "#1baf7a", "#e34948"
MAGENTA = "#d55181"

GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
PUNTI = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]


def griglia_grossa():
    return np.linspace(LAT_MIN, LAT_MAX, N_GROSSA), np.linspace(LON_MIN, LON_MAX, N_GROSSA)


def griglia_fine(n):
    return np.linspace(LAT_MIN, LAT_MAX, n), np.linspace(LON_MIN, LON_MAX, n)


# ----------------------------------------------------------------------------
# Dati reali: Open-Meteo (gratuito, nessuna chiave)
# ----------------------------------------------------------------------------
URL_PREVISIONI = "https://api.open-meteo.com/v1/forecast"
URL_QUOTE = "https://api.open-meteo.com/v1/elevation"
CACHE = Path(".cache_open_meteo")
URL_VICINE = "https://api.weather.com/v3/location/near"
URL_STAZIONE = "https://api.weather.com/v2/pws/observations/current"
SEGRETI = []          # chiavi API da nascondere nei messaggi d'errore
_locale = threading.local()      # una connessione riusata è più stabile di tante nuove (una per thread)
_wu_lucchetto = threading.Lock()
_wu_prossimo = [0.0]
WU_AL_SECONDO = float(os.environ.get("WU_CHIAMATE_AL_SECONDO", "40"))


def _attendi_wu():
    """Distanzia le chiamate a Wunderground di tutti i thread insieme (al massimo WU_AL_SECONDO al secondo)."""
    with _wu_lucchetto:
        ora = time.monotonic()
        inizio = max(ora, _wu_prossimo[0])
        _wu_prossimo[0] = inizio + 1.0 / WU_AL_SECONDO
    if inizio > ora:
        time.sleep(inizio - ora)
VARIABILI = ("temperature_2m,dew_point_2m,pressure_msl,precipitation,wind_speed_10m,wind_direction_10m,cape,"
             "wind_gusts_10m,temperature_850hPa")


def _lista(valori):
    return ",".join(f"{v:.4f}" for v in valori)


def _pulisci(testo):
    """Toglie le chiavi API dai messaggi d'errore (le URL le contengono)."""
    for s in SEGRETI:
        testo = testo.replace(s, "***")
    return re.sub(r"apiKey=[^&\s'\"]+", "apiKey=***", testo)


def _get(url, params, tentativi=5):
    """GET con pazienza: se Open-Meteo risponde 429 (troppe richieste) o 5xx aspetta e riprova."""
    import requests

    for i in range(tentativi):
        if "weather.com" in url:
            _attendi_wu()
        try:
            if getattr(_locale, "sessione", None) is None:
                _locale.sessione = requests.Session()
            r = _locale.sessione.get(url, params=params, timeout=60)
        except requests.exceptions.ConnectionError:
            _locale.sessione = None
            if i == tentativi - 1:
                raise
            attesa = 5 * (i + 1)
            print(f"Connessione fallita, riprovo tra {attesa} s...", file=sys.stderr)
            time.sleep(attesa)
            continue
        if r.status_code in (429, 500, 502, 503, 504) and i < tentativi - 1:
            attesa = int(r.headers.get("Retry-After", 0) or 0) or 15 * (i + 1)
            print(f"Il servizio risponde {r.status_code}, riprovo tra {attesa} s...", file=sys.stderr)
            time.sleep(attesa)
            continue
        r.raise_for_status()
        return r


def _get_json(url, params, validita):
    """Come _get ma salva la risposta in .cache_open_meteo/ per `validita` secondi."""
    firma = params if isinstance(params, str) else repr(sorted(params.items()))
    chiave = hashlib.md5((url + firma).encode()).hexdigest()
    f = CACHE / f"{chiave}.json"
    if f.exists() and time.time() - f.stat().st_mtime < validita:
        return json.loads(f.read_text())
    dati = _get(url, params).json()
    CACHE.mkdir(exist_ok=True)
    f.write_text(json.dumps(dati))
    return dati


def scarica_open_meteo(modello):
    lat_c, lon_c = griglia_grossa()
    LA, LO = np.meshgrid(lat_c, lon_c, indexing="ij")
    la, lo = LA.ravel(), LO.ravel()
    risposta = []
    for i in range(0, la.size, 50):                    # a pacchetti: con centinaia di punti una sola richiesta è troppo pesante
        params = {
            "latitude": _lista(la[i:i + 50]),
            "longitude": _lista(lo[i:i + 50]),
            "hourly": VARIABILI,
            "wind_speed_unit": "ms",
            "timezone": "Europe/Rome",
            "forecast_hours": ORE,
            "past_hours": PASSATO,
            "models": modello,
        }
        r = _get_json(URL_PREVISIONI, params, validita=1800)
        risposta.extend([r] if isinstance(r, dict) else r)
        time.sleep(1.0)

    tempi = [datetime.fromisoformat(s) for s in risposta[0]["hourly"]["time"]]
    nt = len(tempi)

    def campo(nome):
        a = np.full((nt, N_GROSSA, N_GROSSA), np.nan)
        for i, loc in enumerate(risposta):
            a[:, i // N_GROSSA, i % N_GROSSA] = np.array(loc["hourly"][nome], dtype=float)
        return a

    z_c = np.array([loc["elevation"] for loc in risposta], dtype=float).reshape(N_GROSSA, N_GROSSA)
    vel, direz = campo("wind_speed_10m"), np.radians(campo("wind_direction_10m"))
    cape = campo("cape")
    if np.isnan(cape).all():
        print("Attenzione: questo modello non fornisce il CAPE, uso 0.", file=sys.stderr)
    d = {
        "tempi": tempi, "lat_c": lat_c, "lon_c": lon_c, "z_c": z_c,
        "T": campo("temperature_2m"), "Td": campo("dew_point_2m"),
        "p": campo("pressure_msl"), "P": np.nan_to_num(campo("precipitation")),
        "u": -vel * np.sin(direz), "v": -vel * np.cos(direz),
        "cape": np.nan_to_num(cape),
    }
    for k in ("T", "Td", "p", "u", "v"):
        if np.isnan(d[k]).any():
            raise ValueError(f"dati incompleti per '{k}' (prova un altro --modello)")
    for nome, chiave in (("wind_gusts_10m", "G"), ("temperature_850hPa", "T850")):
        a = campo(nome)
        d[chiave] = None if np.isnan(a).any() else a
        if d[chiave] is None:
            print(f"Attenzione: questo modello non fornisce '{nome}': quella mappa verrà saltata.", file=sys.stderr)
    d["P3"], d["P6"] = somma_mobile(d["P"], 3), somma_mobile(d["P"], 6)
    # si parte dall'ora corrente: le ore passate servivano solo per le somme della pioggia
    adesso = datetime.now().replace(minute=0, second=0, microsecond=0)
    i0 = next((i for i, t in enumerate(tempi) if t >= adesso), 0)
    fisse = ("lat_c", "lon_c", "z_c")
    return {k: (v[i0:] if k not in fisse and isinstance(v, (list, np.ndarray)) else v) for k, v in d.items()}


def quote_reali(lat_f, lon_f):
    LA, LO = np.meshgrid(lat_f, lon_f, indexing="ij")
    la, lo = LA.ravel(), LO.ravel()
    out = []
    for i in range(0, la.size, 40):                    # richieste corte: quelle lunghe a volte vengono scartate
        params = ("latitude=" + ",".join(f"{v:.3f}" for v in la[i:i + 40])
                  + "&longitude=" + ",".join(f"{v:.3f}" for v in lo[i:i + 40]))
        out.extend(_get_json(URL_QUOTE, params, validita=30 * 86400)["elevation"])
        time.sleep(0.5)
    return np.array(out, dtype=float).reshape(LA.shape)


# ----------------------------------------------------------------------------
# Stazioni Weather Underground: la "situazione attuale" per correggere il modello
# La chiave si legge dalla variabile d'ambiente WU_API_KEY (mai scritta nei file)
# ----------------------------------------------------------------------------
def cerca_stazioni(chiave):
    """Tutte le stazioni Wunderground nel riquadro delle mappe. Ogni ricerca ne dà al massimo 10: si divide il riquadro
    in parti e si suddivide ancora dove le 10 più vicine non bastano (la procedura è in wrf/stazioni_wrf.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "wrf"))
    import stazioni_wrf as sw

    class Riquadro:
        lat = np.array([LAT_MIN, LAT_MAX])
        lon = np.array([LON_MIN, LON_MAX])

        @staticmethod
        def contiene(la, lo):
            return LAT_MIN <= la <= LAT_MAX and LON_MIN <= lo <= LON_MAX

    sw.SEGRETI.extend(SEGRETI)
    trovate, _ = sw.cerca(chiave, {3: Riquadro}, datetime.now(timezone.utc))
    return {sid: pos for sid, pos in trovate.items() if Riquadro.contiene(*pos)}


def diradamento(stazioni, massimo):
    """Se sono troppe ne tiene `massimo`, il più possibile distanziate tra loro."""
    ids = list(stazioni)
    if len(ids) <= massimo:
        return ids
    k = 111.32
    xy = {i: ((stazioni[i][1] - LON0) * k * np.cos(np.radians(LAT0)), (stazioni[i][0] - LAT0) * k) for i in ids}
    scelte = [min(ids, key=lambda i: np.hypot(*xy[i]))]
    dmin = {i: np.hypot(xy[i][0] - xy[scelte[0]][0], xy[i][1] - xy[scelte[0]][1]) for i in ids}
    while len(scelte) < massimo:
        nuovo = max((i for i in ids if i not in scelte), key=lambda i: dmin[i])
        scelte.append(nuovo)
        for i in ids:
            dmin[i] = min(dmin[i], np.hypot(xy[i][0] - xy[nuovo][0], xy[i][1] - xy[nuovo][1]))
    return scelte


def leggi_stazioni(chiave, ids):
    """Ultima osservazione di ogni stazione (risposta tenuta in cache per 5 minuti), con molte chiamate in parallelo."""
    import requests

    def una(sid):
        p = {"stationId": sid, "format": "json", "units": "m", "numericPrecision": "decimal", "apiKey": chiave}
        try:
            o = _get_json(URL_STAZIONE, p, validita=300)["observations"][0]
            m = o["metric"]
            return sid, {
                "lat": float(o["lat"]), "lon": float(o["lon"]), "quota": m.get("elev"),
                "ora": datetime.strptime(o["obsTimeLocal"], "%Y-%m-%d %H:%M:%S"),
                "T": m.get("temp"), "Td": m.get("dewpt"),
            }
        except (KeyError, IndexError, ValueError, TypeError, requests.RequestException):
            return sid, None                           # stazione senza dati o che non risponde

    with ThreadPoolExecutor(int(os.environ.get("WU_THREAD", "24"))) as pool:
        return {sid: o for sid, o in pool.map(una, list(ids)) if o is not None}


def _filtra(valori):
    """Maschera dei valori non anomali: scarto dalla mediana entro max(3 MAD, 2.5)."""
    v = np.asarray(valori, float)
    if v.size >= 5:
        med = np.median(v)
        mad = 1.4826 * np.median(np.abs(v - med))
        return np.abs(v - med) <= max(3 * mad, 2.5)
    return np.abs(v) <= 6


def correggi_con_stazioni(c, tempi, oss, z_f, lat_f, lon_f, scala_km=5.0, lam=0.25, tau_h=6.0):
    """
    Confronta le stazioni con il modello già adattato al terreno e corregge temperatura e umidità.
    Controlli: dato recente (max 30 min), valori plausibili, posizione dentro il dominio,
    quota dichiarata coerente col terreno (max 350 m), scarto dal modello non anomalo rispetto alle altre.
    La correzione si spande attorno alle stazioni (scala 5 km) e si smorza in circa 6 ore.
    """
    nt = c["T"].shape[0]
    adesso = datetime.now()
    sp = lambda campo: RectBivariateSpline(lat_f, lon_f, campo, kx=1, ky=1)
    sp_z = sp(z_f)
    buone = {}
    for sid, o in oss.items():
        if o["T"] is None or o["Td"] is None:
            continue
        if not (timedelta(minutes=-10) <= adesso - o["ora"] <= timedelta(minutes=30)):
            continue
        if not (-30 < o["T"] < 50) or o["Td"] < -40:
            continue
        if not (LAT_MIN <= o["lat"] <= LAT_MAX and LON_MIN <= o["lon"] <= LON_MAX):
            continue
        if o["quota"] is not None and abs(float(o["quota"]) - float(sp_z.ev(o["lat"], o["lon"]))) > 350:
            continue                                   # posizione o quota incoerenti
        k = int(np.clip(round((o["ora"] - tempi[0]).total_seconds() / 3600), 0, nt - 1))
        o["rT"] = o["T"] - float(sp(c["T"][k]).ev(o["lat"], o["lon"]))
        o["rTd"] = min(o["Td"], o["T"]) - float(sp(c["Td"][k]).ev(o["lat"], o["lon"]))
        buone[sid] = o

    info = {"n": 0, "testo": "", "nota": ""}
    ids = list(buone)
    ids_T = [i for i, f in zip(ids, _filtra([buone[i]["rT"] for i in ids])) if f] if len(ids) >= 3 else []
    ids_Td = [i for i, f in zip(ids, _filtra([buone[i]["rTd"] for i in ids])) if f] if len(ids) >= 3 else []
    if len(ids_T) < 3:
        info["testo"] = (f"Stazioni: {len(oss)} lette, {len(ids)} recenti e coerenti, {len(ids_T)} attendibili: "
                         "troppo poche, nessuna correzione.")
        return c, info

    LON, LAT = np.meshgrid(lon_f, lat_f)
    km = 111.32
    X, Y = (LON - LON0) * km * np.cos(np.radians(LAT0)), (LAT - LAT0) * km

    def analisi(sel, chiave):
        num, den = np.zeros_like(X), np.zeros_like(X)
        for i in sel:
            xs = (buone[i]["lon"] - LON0) * km * np.cos(np.radians(LAT0))
            ys = (buone[i]["lat"] - LAT0) * km
            w = np.exp(-((X - xs) ** 2 + (Y - ys) ** 2) / (2 * scala_km ** 2))
            num += w * buone[i][chiave]
            den += w
        return num / (den + lam)

    corr_T = analisi(ids_T, "rT")
    corr_Td = analisi(ids_Td, "rTd") if len(ids_Td) >= 3 else np.zeros_like(X)
    t_rif = max(buone[i]["ora"] for i in ids_T)
    fatt = np.array([np.exp(-max(0.0, (t - t_rif).total_seconds() / 3600) / tau_h) for t in tempi])[:, None, None]

    out = dict(c)
    out["T"] = c["T"] + fatt * corr_T
    out["Td"] = np.minimum(c["Td"] + fatt * corr_Td, out["T"] - 0.1)
    out["RH"] = umidita_relativa(out["T"], out["Td"])
    bias = float(np.mean([buone[i]["rT"] for i in ids_T]))
    info["n"] = len(ids_T)
    info["testo"] = (f"Stazioni: {len(oss)} lette, {len(ids)} recenti e coerenti, {len(ids_T)} usate dopo il "
                     f"controllo qualità; scarto medio stazioni − modello {bias:+.1f} °C (ore {t_rif:%H:%M}).")
    info["nota"] = (f"Temperatura e umidità corrette con {len(ids_T)} stazioni Wunderground alle {t_rif:%H:%M} "
                    f"(scarto medio {bias:+.1f} °C), correzione smorzata in circa {tau_h:.0f} h; "
                    "vento e pioggia non corretti.")
    return out, info


# ----------------------------------------------------------------------------
# Dati INVENTATI (solo per --simula): terreno e "modello" finti ma plausibili
# ----------------------------------------------------------------------------
def dem_simulato(lat, lon):
    """Terreno inventato ma plausibile: mare a ovest, pianura costiera, Appennino a est e a nord-est, Colli Albani."""
    lat, lon = np.broadcast_arrays(np.asarray(lat, float), np.asarray(lon, float))

    def gauss(la, lo, h, sla, slo):
        return h * np.exp(-(((lat - la) / sla) ** 2 + ((lon - lo) / slo) ** 2) / 2)

    costa = 12.35 + 0.35 * np.sin((lat - 41.0) * 2.2)        # la costa tirrenica, spostata un po' a est a nord
    z = 20 + 380 * np.clip((lon - costa) / 1.6, 0, 1) ** 1.3
    z = np.where(lon < costa, 0.0, z)
    z = z + gauss(42.47, 12.98, 1800, 0.18, 0.22)             # Terminillo e Reatino
    z = z + gauss(41.95, 13.30, 1300, 0.20, 0.16)             # Simbruini
    z = z + gauss(41.72, 13.45, 1300, 0.20, 0.20)             # Ernici
    z = z + gauss(41.45, 13.10, 700, 0.15, 0.20)              # Lepini e Ausoni
    z = z + gauss(41.755, 12.71, 750, 0.10, 0.12)             # Colli Albani
    z = z + gauss(42.40, 12.20, 600, 0.12, 0.12)              # Cimini
    z = z + 25 * np.sin(lat * 19) * np.cos(lon * 23) + 18 * np.sin(lat * 21 + 1) * np.sin(lon * 26)
    return np.clip(z, 0, None)


def dati_simulati(ora0):
    lat_c, lon_c = griglia_grossa()
    LA, LO = np.meshgrid(lat_c, lon_c, indexing="ij")
    z_c = dem_simulato(LA, LO)
    tempi = [ora0 + timedelta(hours=h) for h in range(ORE)]
    t = np.arange(ORE, dtype=float)[:, None, None]
    h = np.array([x.hour + x.minute / 60 for x in tempi])[:, None, None]
    LA3, LO3 = LA[None], LO[None]
    x = (LO3 - LON_MIN) / (LON_MAX - LON_MIN)

    # Temperatura e rugiada ridotte al livello del mare, poi portate alla quota del punto
    T_mare = 17.0 + 5.0 * (1 + 0.35 * x) * np.cos(2 * np.pi * (h - 15) / 24) - 0.06 * t
    Td_mare = np.minimum(10.5 + 0.045 * t + 0.8 * (1 - x), T_mare - 0.3)
    T = T_mare - GRAD_T * z_c[None]
    Td = Td_mare - GRAD_TD * z_c[None]

    # Pressione al livello del mare: scende per l'arrivo di una perturbazione
    p = 1016.8 - 0.14 * t + 1.2 * (x - 0.5)

    # Vento: brezza di mare da OSO di giorno, brezza di terra da NE di notte
    w = np.clip(0.5 + 0.6 * np.sin(2 * np.pi * (h - 10) / 24), 0, 1)
    pre = 1 + 0.7 * t / 48
    vs, vl = 4.0 * (1.25 - 0.5 * x) * pre, 1.6 * pre
    ds, dl = np.radians(240), np.radians(40)
    u = w * (-vs * np.sin(ds)) + (1 - w) * (-vl * np.sin(dl))
    v = w * (-vs * np.cos(ds)) + (1 - w) * (-vl * np.cos(dl))

    # Pioggia: celle temporalesche che attraversano il dominio verso ENE il secondo pomeriggio
    def cella(tc, intensita, dla=0.0, dlo=0.0):
        la_c = 41.65 + 0.030 * (t - 28) + dla
        lo_c = 12.49 + 0.070 * (t - 28) + dlo
        spazio = np.exp(-(((LA3 - la_c) / 0.20) ** 2 + ((LO3 - lo_c) / 0.28) ** 2) / 2)
        return intensita * np.exp(-(((t - tc) / 2.8) ** 2)) * spazio

    P = cella(33, 14) + cella(34, 7, 0.45, -0.35) + 0.25 * np.exp(-(((t - 27) / 4) ** 2)) * (1 + 0.3 * x)

    # CAPE: ciclo giornaliero, piu' alto il secondo giorno
    ampiezza = 250 + 900 / (1 + np.exp(-(t - 24) / 6))
    forma = np.maximum(0, np.cos(2 * np.pi * (h - 15) / 24)) ** 2
    zona_calda = np.exp(-(((LA3 - 41.66) / 0.35) ** 2 + ((LO3 - 12.62) / 0.45) ** 2) / 2)
    cape = np.clip(ampiezza * forma * (1.15 - 0.3 * x + 0.35 * zona_calda) + 20, 0, 1500)

    G = 1.0 + 1.7 * np.hypot(u, v)
    T850 = 11.0 - 0.07 * t + 0.4 * (x - 0.5) + 0.45 * np.cos(2 * np.pi * (h - 15) / 24)
    return {"tempi": tempi, "lat_c": lat_c, "lon_c": lon_c, "z_c": z_c,
            "T": T, "Td": Td, "p": p, "P": P, "u": u, "v": v, "cape": cape,
            "G": G, "T850": T850, "P3": somma_mobile(P, 3), "P6": somma_mobile(P, 6)}


# ----------------------------------------------------------------------------
# Downscaling: dalla griglia grossa a quella fine, con correzione di quota
# ----------------------------------------------------------------------------
def interpola(campo, lat_c, lon_c, lat_f, lon_f, ordine=3):
    out = np.empty((campo.shape[0], lat_f.size, lon_f.size))
    for i in range(campo.shape[0]):
        out[i] = RectBivariateSpline(lat_c, lon_c, campo[i], kx=ordine, ky=ordine)(lat_f, lon_f)
    return out


def umidita_relativa(T, Td):
    a, b = 17.625, 243.04
    return np.clip(100 * np.exp(a * Td / (b + Td) - a * T / (b + T)), 0, 100)


def downscaling(d, z_f, lat_f, lon_f):
    lc, oc, zc = d["lat_c"], d["lon_c"], d["z_c"][None]

    def it(campo, ordine=3):
        return interpola(campo, lc, oc, lat_f, lon_f, ordine)

    z_visto = it(zc)[0]                                   # quota "vista" dal modello sulla griglia fine
    T = it(d["T"] + GRAD_T * zc) - GRAD_T * z_f           # via dal mare e di nuovo alla quota vera
    Td = np.minimum(it(d["Td"] + GRAD_TD * zc) - GRAD_TD * z_f, T - 0.1)
    fattore = np.clip(1 + 0.0004 * (z_f - z_visto), 0.8, 1.7)
    u, v = it(d["u"]) * fattore, it(d["v"]) * fattore
    G = d.get("G")
    if G is not None:
        G = np.maximum(it(G) * fattore, np.hypot(u, v))      # la raffica non è mai sotto il vento medio
    T850 = d.get("T850")
    return {
        "T": T,
        "Td": Td,
        "RH": umidita_relativa(T, Td),
        "T850": None if T850 is None else it(T850),          # a 850 hPa (~1500 m) il terreno non conta
        "p": it(d["p"]),
        "u": u,
        "v": v,
        "G": G,
        "P": np.clip(it(d["P"], 1), 0, None),
        "P3": np.clip(it(d["P3"], 1), 0, None),
        "P6": np.clip(it(d["P6"], 1), 0, None),
        "cape": np.clip(it(d["cape"], 1), 0, None),
    }


# ----------------------------------------------------------------------------
# Grafica
# ----------------------------------------------------------------------------
def _mix(c1, c2, f):
    return tuple(np.array(to_rgb(c1)) * (1 - f) + np.array(to_rgb(c2)) * f)


def rampa(base):
    """Una sola tinta, dal chiaro (vicino allo sfondo) allo scuro."""
    return LinearSegmentedColormap.from_list("r", [_mix(base, "#ffffff", 0.78), base, _mix(base, "#000000", 0.55)])


# Scale di colori a più tinte, come nelle carte di Meteociel. Quelle "assolute" legano un colore a un valore
# (15 °C è sempre giallo, 30 km/h sempre giallo-verde...): si leggono allo stesso modo ogni giorno.
ANCORE = {
    "temp": [(-20, "#5a2d82"), (-12, "#5b4bb7"), (-6, "#3a6fd8"), (0, "#3fa7e0"), (4, "#46c3d6"), (8, "#3dbb8c"),
             (11, "#6fcb4b"), (14, "#bfe03a"), (17, "#f5e03a"), (20, "#f7b733"), (23, "#f58a2e"), (26, "#ee5a2a"),
             (29, "#d62f2a"), (33, "#a81d3a"), (37, "#7a1659"), (42, "#4b1260")],
    "vento": [(0, "#d9eef9"), (8, "#9ad6ee"), (16, "#6fcf9a"), (24, "#a9dc5a"), (32, "#f3e04a"), (40, "#f6b13c"),
              (50, "#f07a30"), (60, "#e0402f"), (75, "#b5233f"), (90, "#8f1f7d"), (110, "#5e1b92"), (140, "#3a1466")],
    "umid": [(0, "#8a4b0f"), (15, "#c98a2e"), (30, "#e8c35a"), (45, "#f1e79a"), (55, "#cdeaa1"), (65, "#8fd6a0"),
             (75, "#52c0b4"), (85, "#3a96c9"), (95, "#2b5fb5"), (100, "#24409a")],
}
# colori per fasce con livelli fissi (l'ultimo è per i valori oltre l'ultimo livello)
FASCE = {
    "pioggia": ["#d2ecf8", "#9bd3f2", "#58aeea", "#2f7fda", "#2a4fc0", "#22a9a0", "#37b95a", "#9fd33f", "#f2e03a",
                "#f6b333", "#f0752c", "#d93a2d", "#b0238c", "#701c9e"],
    "cape": ["#d4eccb", "#97d98f", "#54c05f", "#e4df3a", "#f5a73b", "#e2452e", "#b3226f", "#5c1a8c"],
    # quota dello zero termico: bassa = viola/blu (freddo), alta = arancio/rosso
    "zero": ["#5a2d82", "#3a6fd8", "#3fa7e0", "#46c3d6", "#3dbb8c", "#6fcb4b", "#bfe03a", "#f5e03a", "#f7b733",
             "#f58a2e", "#ee5a2a", "#d62f2a"],
    "nubi": ["#eef2f6", "#d9e0e8", "#c2ccd8", "#a9b5c4", "#8f9db0", "#76859b", "#5e6e85", "#48566b", "#333f52"],
    "neve": ["#e1f2fb", "#bcdff4", "#8fc5ec", "#62a3de", "#3d7bcc", "#4a58b9", "#6c41a9", "#98309b", "#c92f86", "#e8508a"],
    "cin": ["#e7e3f3", "#c6bde4", "#a395d3", "#8068c2", "#5f3fae", "#3f2380", "#241254"],
    "shear": ["#f6efb2", "#f2d35c", "#f1a73a", "#ea7430", "#d9422f", "#b5233f", "#8a1d7a", "#52176f"],
    "rischio": ["#bfe3b4", "#7fcf6f", "#e4df3a", "#f5a73b", "#e2452e", "#b3226f", "#5c1a8c"],
}
# pressione: la variazione sul dominio è di pochi hPa, quindi la scala segue i valori del giorno
ARCOBALENO = ["#4b3a9e", "#3a73c9", "#3fb0c7", "#5fc47f", "#c3dc58", "#f5d83f", "#f29a33", "#d9432f"]
PASSI = (1, 2, 3, 5, 10, 15, 20, 25, 50)
LIVELLI_PIOGGIA_1H = [0.1, 0.2, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32]


def colori_fasce(nome, n):
    """n colori per le fasce (l'ultimo è per i valori oltre l'ultimo livello), campionati dalla tavolozza."""
    base = FASCE[nome]
    if len(base) == n:
        return list(base)
    cmap = LinearSegmentedColormap.from_list("fasce", base)
    return [cmap(x) for x in np.linspace(0, 1, n)]


def tick_utili(liv, massimo=10):
    """Non più di `massimo` etichette sulla barra dei colori."""
    passo = -(-len(liv) // massimo)
    return list(liv)[::passo]


def scala_assoluta(nome, valori, passo, massimo=None, max_fasce=12, min_fasce=4):
    """Livelli a passo regolare e un colore per fascia, preso dalla tavolozza in base al valore di mezzo."""
    ancore = ANCORE[nome]
    v = np.array([a[0] for a in ancore], float)
    cmap = LinearSegmentedColormap.from_list("anc", list(zip((v - v[0]) / (v[-1] - v[0]), [a[1] for a in ancore])))
    lo, hi = float(np.min(valori)), float(np.max(valori))
    for p_ in [q for q in PASSI if q >= passo]:
        a, b = np.floor(lo / p_) * p_, np.ceil(hi / p_) * p_
        if massimo is not None:
            b = min(b, massimo)
        if (b - a) / p_ <= max_fasce:
            break
    k = 0
    while (b - a) / p_ < min_fasce:                      # campo quasi uniforme: allarga per avere qualche fascia
        a, b = (a - p_, b) if k % 2 == 0 else (a, b + p_)
        if massimo is not None:
            b = min(b, massimo)
            if k > 6:
                break
        k += 1
    liv = np.arange(a, b + p_ / 2, p_)
    medi = (liv[:-1] + liv[1:]) / 2
    cols = [cmap(float(np.clip((m - v[0]) / (v[-1] - v[0]), 0, 1))) for m in medi]
    return liv, cols


def scala_arcobaleno(valori, n=8, span_min=1.5):
    liv = livelli(valori, n=n, span_min=span_min)
    cmap = LinearSegmentedColormap.from_list("arc", ARCOBALENO)
    return liv, [cmap(x) for x in np.linspace(0.04, 0.96, len(liv) - 1)]


def etichetta(dt):
    return f"{GIORNI[dt.weekday()]} {dt.day} {MESI[dt.month - 1]}, {dt:%H:%M}"


def nome_direzione(u, v):
    gradi = np.degrees(np.arctan2(-u, -v)) % 360     # direzione di provenienza
    return PUNTI[int((gradi + 22.5) // 45) % 8]


def livelli(campo, n=9, span_min=0.0, massimo=None):
    lo, hi = float(campo.min()), float(campo.max())
    if hi - lo < span_min:
        m = (hi + lo) / 2
        lo, hi = m - span_min / 2, m + span_min / 2
    lv = MaxNLocator(nbins=n).tick_values(lo, hi)
    if massimo is not None:
        lv = np.unique(np.clip(lv, None, massimo))
    return lv


_CONFINI = None
FILE_CONFINI = Path(__file__).resolve().parent / "dati" / "confini_lazio.json"


def carica_confini():
    """(terraferma del Lazio, isole del Lazio, province del Lazio, altre province) come liste di anelli, array (n, 2) lon/lat.
    Il file lo fa wrf/prepara_confini.py dai confini ISTAT; se manca le mappe escono senza confini."""
    global _CONFINI
    if _CONFINI is None:
        try:
            d = json.loads(FILE_CONFINI.read_text(encoding="utf-8"))
            anelli = [np.array(a) for a in d["regione"]]
            anelli.sort(key=lambda a: -np.ptp(a[:, 0]) * np.ptp(a[:, 1]))     # il primo è la terraferma
            reg, isole = anelli[:1], anelli[1:]
            nel = [np.array(a) for p in d["province"] if p["regione"] == "Lazio" for a in p["anelli"]]
            fuori = [np.array(a) for p in d["province"] if p["regione"] != "Lazio" for a in p["anelli"]]
            _CONFINI = (reg, isole, nel, fuori)
        except (OSError, ValueError, KeyError) as e:
            print(f"Confini non disponibili ({FILE_CONFINI.name}: {e}): mappe senza confini", file=sys.stderr)
            _CONFINI = ([], [], [], [])
    return _CONFINI


_LAGHI = None
FILE_LAGHI = Path(__file__).resolve().parent / "dati" / "laghi_lazio.json"


def carica_laghi():
    """Lista di laghi, ognuno (nome, anelli esterni, anelli interni/isole) come array (n, 2) lon/lat.
    Il file lo fa wrf/prepara_laghi.py da OpenStreetMap; se manca le mappe escono senza laghi."""
    global _LAGHI
    if _LAGHI is None:
        try:
            d = json.loads(FILE_LAGHI.read_text(encoding="utf-8"))
            _LAGHI = [(l["nome"], [np.array(a) for a in l["esterni"]], [np.array(a) for a in l["interni"]]) for l in d["laghi"]]
        except (OSError, ValueError, KeyError) as e:
            print(f"Laghi non disponibili ({FILE_LAGHI.name}: {e}): mappe senza laghi", file=sys.stderr)
            _LAGHI = []
    return _LAGHI


def _verso(anello, antiorario):
    """L'anello percorso in senso antiorario (o orario): serve perché le isole di un lago siano buchi."""
    x, y = anello[:, 0], anello[:, 1]
    area = 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))
    return anello if (area > 0) == antiorario else anello[::-1]


def disegna_laghi(ax, scala=1.0):
    """I laghi con il loro contorno vero (isole comprese), azzurro con bordo blu."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as Percorso

    for _nome, esterni, interni in carica_laghi():
        v, c = [], []
        for a, ant in [(a, True) for a in esterni] + [(a, False) for a in interni]:
            a = _verso(a, ant)
            v += [*a, a[0]]
            c += [Percorso.MOVETO] + [Percorso.LINETO] * (len(a) - 1) + [Percorso.CLOSEPOLY]
        ax.add_patch(PathPatch(Percorso(v, c), fc="#cde2fb", ec=BLU, lw=0.8 * scala, zorder=5))


def disegna_confini(ax, scala=1.0):
    """Confine del Lazio (linea spessa), confini delle sue province (più sottile) e delle province vicine (sottile e grigia).
    `scala` assottiglia le linee nei riquadri piccoli."""
    reg, isole, nel, fuori = carica_confini()
    for a in fuori:
        ax.plot(a[:, 0], a[:, 1], color=SEC, lw=0.7 * scala, alpha=0.75, zorder=3, solid_joinstyle="round")
    for a in nel:
        ax.plot(a[:, 0], a[:, 1], color=INK, lw=1.0 * scala, alpha=0.85, zorder=3.5, solid_joinstyle="round")
    for a in reg:
        ax.plot(a[:, 0], a[:, 1], color=INK, lw=2.6 * scala, zorder=4, solid_joinstyle="round", solid_capstyle="round")
    for a in isole:                                       # le isole pontine: linea fina, come le province
        ax.plot(a[:, 0], a[:, 1], color=INK, lw=1.0 * scala, zorder=4, solid_joinstyle="round")


def disegna_flusso(ax, lon_f, lat_f, u, v, densita=1.5, spessore=0.6, scala=1.0, zorder=6):
    """Il vento come linee di flusso, come le carte di Meteociel: linee continue sottili con tante freccette piene
    lungo la linea (una ogni ~18 km sulla mappa) che danno il verso. `densita` 1 = circa 30 linee per lato della mappa;
    `scala` ingrandisce freccette e distanza tra le freccette nei riquadri piccoli."""
    from matplotlib.collections import PolyCollection

    u = np.ma.masked_invalid(np.asarray(u, float))
    v = np.ma.masked_invalid(np.asarray(v, float))
    flusso = ax.streamplot(lon_f, lat_f, u, v, density=densita, color=INK, linewidth=spessore, arrowstyle="-",
                           integration_direction="both", broken_streamlines=True, zorder=zorder)   # senza le sue frecce (una per linea):
    # le rifacciamo noi, più fitte e piene

    kx, ky = 111.32 * np.cos(np.radians(LAT0)), 111.32      # km per grado: le freccette si disegnano in km (mappa isotropa)
    passo, punta, largo = 18.0 * scala, 3.0 * scala, 2.0 * scala
    triangoli, linea = [], []

    def freccette(pt):
        pt = np.asarray(pt)
        if len(pt) < 2:
            return
        d = np.hypot(*np.diff(pt, axis=0).T)
        lungo = np.concatenate([[0.0], np.cumsum(d)])
        if lungo[-1] < 2 * punta:
            return
        pos = np.arange(passo / 2, lungo[-1] - punta, passo) if lungo[-1] > passo else [lungo[-1] / 2]
        def en(t):
            return np.array([np.interp(t, lungo, pt[:, 0]), np.interp(t, lungo, pt[:, 1])])

        for sc in pos:
            a_, b_ = en(max(sc - punta / 2, 0.0)), en(min(sc + punta / 2, lungo[-1]))
            dx, dy = b_ - a_
            n = np.hypot(dx, dy)
            if n == 0:
                continue
            dx, dy = dx / n, dy / n
            c = en(sc)
            tip, base = c + np.array([dx, dy]) * punta / 2, c - np.array([dx, dy]) * punta / 2
            lato = np.array([-dy, dx]) * largo / 2
            triangoli.append(np.array([tip, base + lato, base - lato]) / [kx, ky])      # di nuovo in gradi

    for seg in flusso.lines.get_segments():                # una linea di flusso per elemento (nelle versioni vecchie, a pezzetti)
        pt = list(np.asarray(seg) * [kx, ky])
        if linea and np.allclose(pt[0], linea[-1], atol=1e-9):
            linea += pt[1:]
        else:
            freccette(linea)
            linea = pt
    freccette(linea)
    if triangoli:
        ax.add_collection(PolyCollection(triangoli, facecolors=INK, edgecolors="none", zorder=zorder))


def decora(ax, z_f, lat_f, lon_f):
    halo = [pe.withStroke(linewidth=3, foreground=SFONDO)]
    disegna_confini(ax)
    ax.contour(lon_f, lat_f, z_f, levels=LIVELLI_QUOTA, colors=TENUE,
               linewidths=0.6, alpha=0.8, zorder=2)
    disegna_laghi(ax)
    for nome, la, lo in PAESI:
        ax.plot(lo, la, "o", ms=4, color=INK, mec=SFONDO, mew=0.8, zorder=7)
        ax.text(lo + 0.04, la, nome, fontsize=11, color=INK, va="center", zorder=7, path_effects=halo)
    nome, la, lo = STAZIONE
    ax.plot(lo, la, "*", ms=13, color=INK, mec=SFONDO, mew=0.8, zorder=8)
    ax.text(lo + 0.05, la - 0.05, nome, fontsize=10.5, weight="bold", color=INK, va="center",
            zorder=8, path_effects=halo)
    x0, y0 = LON_MIN + 0.08, LAT_MIN + 0.08
    lung = 50 / (111.32 * np.cos(np.radians(LAT0)))
    ax.plot([x0, x0 + lung], [y0, y0], color=INK, lw=2.5, zorder=8, solid_capstyle="butt")
    ax.text(x0 + lung / 2, y0 + 0.05, "50 km", fontsize=9.5, ha="center", color=INK, zorder=8, path_effects=halo)


def disegna_mappe(c, z_f, lat_f, lon_f, k, tempi, titolo_banner, modello_txt, out, sim):
    LON, LAT = np.meshgrid(lon_f, lat_f)
    iy = int(np.abs(lat_f - STAZIONE[1]).argmin())
    ix = int(np.abs(lon_f - STAZIONE[2]).argmin())

    u, v = c["u"] * 3.6, c["v"] * 3.6                        # km/h
    vel_tot = np.hypot(u, v)
    T, RH, p, P, C = c["T"][k], c["RH"][k], c["p"][k], c["P"][k], c["cape"][k]
    uk, vk, vel = u[k], v[k], vel_tot[k]
    st = lambda a: float(a[iy, ix])                          # valore alla stazione

    def p_temp(ax):
        liv, cols = scala_assoluta("temp", T, 1, max_fasce=24)
        cs = ax.contourf(LON, LAT, T, levels=liv, colors=cols, zorder=1)
        return cs, tick_utili(liv)

    def p_vento(ax):
        liv, cols = scala_assoluta("vento", vel, 2, max_fasce=20)
        cs = ax.contourf(LON, LAT, vel, levels=liv, colors=cols, zorder=1)
        disegna_flusso(ax, lon_f, lat_f, uk, vk, densita=1.6)
        return cs, tick_utili(liv)

    def p_press(ax):
        lv, cols = scala_arcobaleno(p)
        cs = ax.contourf(LON, LAT, p, levels=lv, colors=cols, zorder=1)
        iso = ax.contour(LON, LAT, p, levels=lv, colors=INK, linewidths=0.7, zorder=3)
        try:
            ax.clabel(iso, fmt="%.1f", fontsize=9)
        except Exception:
            pass
        return cs, None

    def p_pioggia(ax):
        if P.max() < 0.1:
            ax.text(0.62, 0.12, "Nessuna pioggia\nprevista a quest'ora", ha="center", va="center",
                    transform=ax.transAxes, fontsize=14, color=SEC)
            return None, None
        liv = LIVELLI_PIOGGIA_1H
        cols = colori_fasce("pioggia", len(liv))
        cs = ax.contourf(LON, LAT, P, levels=liv, colors=list(cols[:-1]), extend="max", zorder=1)
        cs.cmap.set_over(cols[-1])
        cs.changed()
        return cs, liv

    def p_umid(ax):
        liv, cols = scala_assoluta("umid", RH, 5, massimo=100)
        cs = ax.contourf(LON, LAT, RH, levels=liv, colors=cols, zorder=1)
        return cs, liv

    def p_cape(ax):
        if C.max() < 50:
            ax.text(0.62, 0.12, "Instabilità trascurabile\n(CAPE sotto 50 J/kg)", ha="center", va="center",
                    transform=ax.transAxes, fontsize=14, color=SEC)
            return None, None
        liv = [50] + list(range(100, 1600, 100))          # fasce da 100 J/kg: si vede il gradiente
        cols = [FASCE["cape"][min(7, int(np.searchsorted([100, 200, 300, 500, 750, 1000, 1500], l, side="right")))]
                for l in liv]
        cs = ax.contourf(LON, LAT, C, levels=liv, colors=list(cols[:-1]), extend="max", zorder=1)
        cs.cmap.set_over(cols[-1])
        cs.changed()
        return cs, [100, 300, 500, 700, 900, 1100, 1300, 1500]

    pannelli = {
        "temp": ("Temperatura a 2 m (°C)",
                 f"min {T.min():.1f} · max {T.max():.1f}   |   {STAZIONE[0]} {st(T):.1f} °C", p_temp),
        "vento": ("Vento a 10 m (km/h)",
                  f"max {vel.max():.0f}   |   {STAZIONE[0]} {st(vel):.0f} da {nome_direzione(st(uk), st(vk))}", p_vento),
        "press": ("Pressione al livello del mare (hPa)",
                  f"{p.min():.1f}–{p.max():.1f}   |   {STAZIONE[0]} {st(p):.1f}", p_press),
        "pioggia": ("Pioggia oraria (mm/h)",
                    f"max {P.max():.1f}   |   {STAZIONE[0]} {st(P):.1f} mm/h", p_pioggia),
        "umid": ("Umidità relativa (%)",
                 f"min {RH.min():.0f} · max {RH.max():.0f}   |   {STAZIONE[0]} {st(RH):.0f} %", p_umid),
        "cape": ("Instabilità, CAPE (J/kg)",
                 f"max {C.max():.0f}   |   {STAZIONE[0]} {st(C):.0f} J/kg", p_cape),
    }

    # Riepilogo 48 h alla stazione
    Ts, Ps = c["T"][:, iy, ix], c["P"][:, iy, ix]
    riep1 = (f"{STAZIONE[0]}, prossime {len(tempi) - 1} h:  T {Ts.min():.0f}–{Ts.max():.0f} °C"
             f" · pioggia {Ps.sum():.0f} mm · vento max {vel_tot[:, iy, ix].max():.0f} km/h")
    riep2 = (f"umidità min {c['RH'][:, iy, ix].min():.0f}% · CAPE max {c['cape'][:, iy, ix].max():.0f} J/kg"
             f" · pressione {c['p'][0, iy, ix]:.0f} → {c['p'][-1, iy, ix]:.0f} hPa")

    gruppi = [
        ("1/2", ["temp", "vento", "press"], "lazio_1_temp_vento_pressione.png"),
        ("2/2", ["pioggia", "umid", "cape"], "lazio_2_pioggia_umidita_cape.png"),
    ]
    def testo_note(sigla):
        note = ["Linee scure: confine del Lazio (spessa) e confini provinciali (sottili); linee grigie chiare: quota ogni 500 m. "
                "Posizioni delle città approssimate. Confini: ISTAT, elaborati da Openpolis (CC BY 4.0). "
                "Laghi: © OpenStreetMap contributors (ODbL).", modello_txt,
                "Non sostituisce le previsioni ufficiali."]
        if sigla == "2/2":
            note.insert(1, "CAPE alto = ambiente favorevole ai temporali, non una certezza.")
        return "\n".join(textwrap.fill(r, 74) for r in note)

    righe_note = max(testo_note(s).count("\n") + 1 for s in ("1/2", "2/2"))
    W, HEAD, MAPPA, BLOCCO = 6.4, 2.4, 5.4, 6.95
    FOOT = 0.35 + 0.19 * righe_note                      # il piede cresce con la lunghezza delle note
    H = HEAD + 3 * BLOCCO + FOOT
    fx, fy = (lambda xi: xi / W), (lambda yi: 1 - yi / H)
    file_out = []

    for sigla, chiavi, nome_file in gruppi:
        fig = plt.figure(figsize=(W, H), facecolor=SFONDO)
        fig.text(fx(0.5), fy(0.45), f"Lazio · {sigla}", fontsize=20, weight="bold", color=INK, va="center")
        fig.text(fx(0.5), fy(0.88), f"+{k} h · {etichetta(tempi[k])}", fontsize=14, color=SEC, va="center")
        fig.text(fx(0.5), fy(1.35), titolo_banner, fontsize=12, weight="bold", color=INK, va="center",
                 bbox=dict(boxstyle="round,pad=0.45", fc="#ffffff", ec=INK, lw=1.2))
        fig.text(fx(0.5), fy(1.88), riep1, fontsize=11, color=SEC, va="center")
        fig.text(fx(0.5), fy(2.12), riep2, fontsize=11, color=SEC, va="center")

        for j, chiave in enumerate(chiavi):
            titolo, sotto, disegna = pannelli[chiave]
            top = HEAD + j * BLOCCO
            fig.text(fx(0.5), fy(top + 0.30), titolo, fontsize=15, weight="bold", color=INK, va="center")
            fig.text(fx(0.5), fy(top + 0.58), sotto, fontsize=12, color=SEC, va="center")
            ax = fig.add_axes([fx(0.5), fy(top + 0.75 + MAPPA), fx(MAPPA), MAPPA / H])
            ax.set_facecolor(SFONDO)
            ax.set_xlim(LON_MIN, LON_MAX)
            ax.set_ylim(LAT_MIN, LAT_MAX)
            ax.set_aspect(1 / np.cos(np.radians(LAT0)))
            ax.set_xticks([])
            ax.set_yticks([])
            for s in ax.spines.values():
                s.set_color(LINEA)
            cs, ticks = disegna(ax)
            decora(ax, z_f, lat_f, lon_f)
            if cs is not None:
                cax = fig.add_axes([fx(0.5), fy(top + 0.75 + MAPPA + 0.38), fx(MAPPA), 0.16 / H])
                cb = fig.colorbar(cs, cax=cax, orientation="horizontal")
                if ticks is not None:
                    cb.set_ticks(ticks)
                cb.ax.tick_params(labelsize=12, colors=SEC, length=3)
                cb.outline.set_visible(False)

        testo = testo_note(sigla)
        fig.text(fx(0.5), fy(HEAD + 3 * BLOCCO + 0.05), testo, fontsize=9.5, color=SEC, va="top", linespacing=1.4)

        percorso = Path(out) / nome_file
        fig.savefig(percorso, dpi=140, facecolor=SFONDO)
        plt.close(fig)
        file_out.append(percorso)
    return file_out


def somma_mobile(P, n):
    """Somma delle n ore che finiscono a ogni istante (all'inizio, solo le ore disponibili)."""
    cum = np.cumsum(P, axis=0)
    out = cum.copy()
    out[n:] = cum[n:] - cum[:-n]
    return out


LIVELLI_CAPE = [50, 100, 250, 500, 1000, 1500, 2000, 3000, 4000]
NOTA_RISCHIO = "indice, non probabilità: da 1 in su l'ambiente è favorevole"


def definisci_fogli(c):
    kmh = 3.6
    G = c.get("G")
    fogli = [
        dict(nome="temperatura_2m", titolo="Temperatura a 2 m", unita="°C", dati=c["T"], scala="temp",
             fmt="{:.0f}", passo=1, max_fasce=24),
        dict(nome="temperatura_925", titolo="Temperatura a 925 hPa", unita="°C", dati=c.get("T925"),
             scala="temp", fmt="{:.0f}", passo=1, max_fasce=24, nota="circa 750 m di quota", opzionale=True),
        dict(nome="temperatura_850", titolo="Temperatura a 850 hPa", unita="°C", dati=c.get("T850"),
             scala="temp", fmt="{:.0f}", passo=1, max_fasce=24, nota="circa 1500 m di quota"),
        dict(nome="zero_termico", titolo="Quota dello zero termico", unita="m", dati=c.get("zero_t"), scala="zero",
             fmt="{:.0f}", livelli=[0, 250, 500, 750, 1000, 1250, 1500, 2000, 2500, 3000, 3500, 4000],
             nota="m sul livello del mare; se l'aria al suolo è sotto zero è la quota teorica, anche sotto il suolo",
             opzionale=True),
        dict(nome="umidita", titolo="Umidità relativa", unita="%", dati=c["RH"], scala="umid", fmt="{:.0f}",
             passo=5, massimo=100),
        dict(nome="vento_medio", titolo="Vento medio a 10 m", unita="km/h", dati=np.hypot(c["u"], c["v"]) * kmh,
             scala="vento", fmt="{:.0f}", passo=2, max_fasce=20, frecce=(c["u"] * kmh, c["v"] * kmh),
             nota="linee di flusso, la freccetta dà il verso"),
        dict(nome="raffiche", titolo="Raffiche a 10 m", unita="km/h", dati=None if G is None else G * kmh,
             scala="vento", fmt="{:.0f}", passo=2, max_fasce=20, frecce=(c["u"] * kmh, c["v"] * kmh),
             nota="colore: raffica; linee di flusso: direzione del vento medio"),
        dict(nome="pioggia_1h", titolo="Pioggia nell'ultima ora", unita="mm", dati=c["P"], scala="pioggia",
             fmt="{:.1f}", livelli=LIVELLI_PIOGGIA_1H),
        dict(nome="pioggia_3h", titolo="Pioggia nelle ultime 3 ore", unita="mm", dati=c["P3"], scala="pioggia",
             fmt="{:.1f}", livelli=[0.2, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30, 40, 60, 80]),
        dict(nome="pioggia_6h", titolo="Pioggia nelle ultime 6 ore", unita="mm", dati=c["P6"], scala="pioggia",
             fmt="{:.1f}", livelli=[0.5, 1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128]),
        dict(nome="pioggia_12h", titolo="Pioggia nelle ultime 12 ore", unita="mm", dati=c.get("P12"), scala="pioggia",
             fmt="{:.1f}", livelli=[1, 2, 4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256], opzionale=True),
        dict(nome="pioggia_24h", titolo="Pioggia nelle ultime 24 ore", unita="mm", dati=c.get("P24"), scala="pioggia",
             fmt="{:.1f}", livelli=[1, 2, 5, 10, 15, 20, 30, 40, 60, 80, 120, 160, 240, 320],
             nota="all'inizio, solo le ore già calcolate", opzionale=True),
        dict(nome="neve_6h", titolo="Neve fresca nelle ultime 6 ore", unita="cm", dati=c.get("N6"), scala="neve",
             fmt="{:.1f}", livelli=[0.1, 0.5, 1, 2, 5, 10, 15, 20, 30, 50],
             nota="dalla precipitazione solida del modello, sotto i 3 °C", opzionale=True,
             vuoto_se=0.1, vuoto_msg="Nessuna neve prevista"),
        dict(nome="neve_24h", titolo="Neve fresca nelle ultime 24 ore", unita="cm", dati=c.get("N24"), scala="neve",
             fmt="{:.1f}", livelli=[0.1, 0.5, 1, 2, 5, 10, 15, 20, 30, 50, 80],
             nota="dalla precipitazione solida del modello, sotto i 3 °C", opzionale=True,
             vuoto_se=0.1, vuoto_msg="Nessuna neve prevista"),
        dict(nome="pressione", titolo="Pressione al livello del mare", unita="hPa", dati=c["p"], scala="arcobaleno",
             fmt="{:.0f}", span=4),
        dict(nome="nubi_totale", titolo="Copertura nuvolosa totale", unita="%", dati=c.get("nubi_tot"), scala="nubi",
             fmt="{:.0f}", livelli=[10, 20, 30, 40, 50, 60, 70, 80, 90], opzionale=True),
        dict(nome="nubi_basse", titolo="Nubi basse", unita="%", dati=c.get("nubi_bassa"), scala="nubi",
             fmt="{:.0f}", livelli=[10, 20, 30, 40, 50, 60, 70, 80, 90], nota="dal suolo a circa 2 km", opzionale=True),
        dict(nome="nubi_medie", titolo="Nubi medie", unita="%", dati=c.get("nubi_media"), scala="nubi",
             fmt="{:.0f}", livelli=[10, 20, 30, 40, 50, 60, 70, 80, 90], nota="da circa 2 a 6 km", opzionale=True),
        dict(nome="nubi_alte", titolo="Nubi alte", unita="%", dati=c.get("nubi_alta"), scala="nubi",
             fmt="{:.0f}", livelli=[10, 20, 30, 40, 50, 60, 70, 80, 90], nota="oltre circa 6 km", opzionale=True),
    ]
    if "cape_mu" in c:
        fogli += [
            dict(nome="cape_mu", titolo="CAPE della parcella più instabile", unita="J/kg", dati=c["cape_mu"],
                 scala="cape", fmt="{:.0f}", livelli=LIVELLI_CAPE, nota="parcella dai primi 300 hPa sopra il suolo"),
            dict(nome="cape_ml", titolo="CAPE dello strato mescolato", unita="J/kg", dati=c["cape_ml"],
                 scala="cape", fmt="{:.0f}", livelli=LIVELLI_CAPE, nota="parcella media dei primi 100 hPa"),
            dict(nome="cin", titolo="Inibizione convettiva (CIN)", unita="J/kg", dati=c["cin"], scala="cin",
                 fmt="{:.0f}", livelli=[10, 25, 50, 100, 150, 200, 300],
                 nota="strato mescolato, solo dove c'è CAPE; più alta = temporali più difficili da innescare"),
        ]
    else:                                                 # dati senza le parcelle (Open-Meteo): solo il CAPE di superficie
        fogli.append(dict(nome="cape", titolo="Instabilità (CAPE)", unita="J/kg", dati=c.get("cape"), scala="cape",
                          fmt="{:.0f}", livelli=[50, 100, 200, 300, 500, 750, 1000, 1500]))
    fogli += [
        dict(nome="shear_01", titolo="Shear del vento 0-1 km", unita="m/s", dati=c.get("shear_01"), scala="shear",
             fmt="{:.0f}", livelli=[2, 4, 6, 8, 10, 12, 15, 20], nota="differenza di vento tra 10 m e 1 km",
             opzionale=True),
        dict(nome="shear_03", titolo="Shear del vento 0-3 km", unita="m/s", dati=c.get("shear_03"), scala="shear",
             fmt="{:.0f}", livelli=[4, 8, 12, 16, 20, 25, 30, 40], nota="differenza di vento tra 10 m e 3 km",
             opzionale=True),
        dict(nome="shear_06", titolo="Shear del vento 0-6 km", unita="m/s", dati=c.get("shear_06"), scala="shear",
             fmt="{:.0f}", livelli=[6, 10, 14, 18, 22, 26, 30, 40], nota="da 18 m/s in su, temporali organizzati",
             opzionale=True),
        dict(nome="srh_01", titolo="Elicità relativa alla tempesta 0-1 km", unita="m²/s²", dati=c.get("srh_01"),
             scala="shear", fmt="{:.0f}", livelli=[25, 50, 100, 150, 200, 300, 400, 500],
             nota="SRH, per una tempesta che devia a destra (Bunkers)", opzionale=True),
        dict(nome="srh_03", titolo="Elicità relativa alla tempesta 0-3 km", unita="m²/s²", dati=c.get("srh_03"),
             scala="shear", fmt="{:.0f}", livelli=[50, 100, 150, 200, 300, 400, 500, 700],
             nota="SRH, per una tempesta che devia a destra (Bunkers)", opzionale=True),
        dict(nome="fulmini", titolo="Rischio fulmini", unita="indice", dati=c.get("fulmini"), scala="rischio",
             fmt="{:.1f}", livelli=[0.1, 0.25, 0.5, 1, 2, 4, 8],
             nota="correnti ascendenti, graupel e LPI; 1 = fulmini probabili; non è una probabilità", opzionale=True,
             vuoto_se=0.1, vuoto_msg="Nessun rischio di fulmini"),
        dict(nome="supercelle", titolo="Rischio supercelle", unita="indice", dati=c.get("supercelle"), scala="rischio",
             fmt="{:.1f}", livelli=[0.25, 0.5, 1, 2, 4, 8, 16],
             nota="SCP e rotazione delle correnti ascendenti; da 1 in su l'ambiente è favorevole; non è una probabilità",
             opzionale=True, vuoto_se=0.25, vuoto_msg="Nessun rischio di supercelle"),
        dict(nome="tornado", titolo="Rischio tornado", unita="indice", dati=c.get("tornado"), scala="rischio",
             fmt="{:.1f}", livelli=[0.1, 0.25, 0.5, 1, 2, 4, 8],
             nota="STP; da 1 in su l'ambiente è favorevole; il modello non vede il tornado, solo l'ambiente", opzionale=True,
             vuoto_se=0.1, vuoto_msg="Nessun rischio di tornado"),
    ]
    return fogli


def disegna_fogli(c, z_f, lat_f, lon_f, tempi, banner, modello_txt, out):
    """Un'immagine per variabile: 9 riquadri, uno ogni 6 ore, tutti con la stessa scala di colori."""
    idx = list(range(0, len(tempi), 6))[:9]
    LON, LAT = np.meshgrid(lon_f, lat_f)
    iy = int(np.abs(lat_f - STAZIONE[1]).argmin())
    ix = int(np.abs(lon_f - STAZIONE[2]).argmin())
    halo = [pe.withStroke(linewidth=2.5, foreground=SFONDO)]
    asciutto = float(c["P6"].max()) < 0.1

    fogli = []
    for f in definisci_fogli(c):
        if f["dati"] is None:
            if not f.get("opzionale"):
                print(f"Salto «{f['titolo']}»: questo modello non fornisce il dato.", file=sys.stderr)
        elif f["nome"].startswith("pioggia") and asciutto:
            if f["nome"] == "pioggia_1h":
                fogli.append(dict(nome="pioggia", vuoto=True, titolo="Pioggia nelle prossime 48 ore",
                                  sotto="ultima ora, 3, 6, 12 e 24 ore", msg="Nessuna pioggia prevista",
                                  dettaglio=f"Massimo sul dominio: {c['P'].max():.1f} mm in 1 ora, {c['P6'].max():.1f} mm in 6 ore."))
        elif f.get("vuoto_se") is not None and float(np.nanmax(f["dati"])) < f["vuoto_se"]:
            fogli.append(dict(nome=f["nome"], vuoto=True, titolo=f"{f['titolo']} ({f['unita']})", sotto="nelle prossime 48 ore",
                              msg=f["vuoto_msg"], dettaglio=f"Massimo sul dominio: {float(np.nanmax(f['dati'])):.2f} {f['unita']}."))
        else:
            fogli.append(f)

    W, SIDE, GAP, MARG, ETICH, HEAD, CB = 6.4, 1.88, 0.08, 0.3, 0.34, 1.55, 0.95
    PASSO = SIDE + ETICH + 0.06
    righe_tile = -(-len(idx) // 3)
    wrap = lambda righe: "\n".join(textwrap.fill(r, 74) for r in righe)
    testo = wrap(["★ = Velletri: il suo valore è scritto in basso a destra di ogni riquadro.",
                  "Stessi colori in tutti i riquadri; linee grigie: quota ogni 500 m.", modello_txt,
                  "Non sostituisce le previsioni ufficiali."])
    testo_secco = wrap([modello_txt, "Non sostituisce le previsioni ufficiali."])
    foot = lambda t: 0.55 + 0.19 * (t.count("\n") + 1)
    simulato = "SIMULAZIONE" in banner

    def intestazione(fig, H, titolo, sotto):
        fig.text(MARG / W, 1 - 0.40 / H, titolo, fontsize=16, weight="bold", color=INK, va="center")
        fig.text(MARG / W, 1 - 0.80 / H, sotto, fontsize=11, color=SEC, va="center")
        if simulato:
            fig.text(MARG / W, 1 - 1.20 / H, banner, fontsize=11, weight="bold", color=INK, va="center",
                     bbox=dict(boxstyle="round,pad=0.4", fc="#ffffff", ec=INK, lw=1.2))
        else:
            fig.text(MARG / W, 1 - 1.20 / H, banner, fontsize=10.5, color=SEC, va="center")

    creati = []
    for n, f in enumerate(fogli, start=1):
        percorso = Path(out) / f"{n:02d}_{f['nome']}.png"

        if f.get("vuoto"):
            H = 2.9 + foot(testo_secco)
            fig = plt.figure(figsize=(W, H), facecolor=SFONDO)
            intestazione(fig, H, f["titolo"], f["sotto"])
            fig.text(MARG / W, 1 - 1.85 / H, f["msg"], fontsize=15, weight="bold", color=INK, va="center")
            fig.text(MARG / W, 1 - 2.30 / H, f["dettaglio"], fontsize=11, color=SEC, va="center")
            fig.text(MARG / W, 1 - 2.95 / H, testo_secco, fontsize=9.5, color=SEC, va="top", linespacing=1.4)
            fig.savefig(percorso, dpi=150, facecolor=SFONDO)
            plt.close(fig)
            creati.append(percorso)
            continue

        dati = np.asarray(f["dati"], float)
        sel = dati[idx]
        if "livelli" in f:                               # fasce a livelli fissi (pioggia, CAPE)
            liv, fisse = list(f["livelli"]), True
            cols = colori_fasce(f["scala"], len(liv))
        elif f["scala"] == "arcobaleno":                 # segue i valori del giorno (pressione)
            liv, cols = scala_arcobaleno(sel, n=8, span_min=f.get("span", 1.5))
            fisse = False
        else:                                            # un colore per valore, uguale ogni giorno
            liv, cols = scala_assoluta(f["scala"], sel, f["passo"], massimo=f.get("massimo"),
                                       max_fasce=f.get("max_fasce", 12))
            fisse = False

        def riempi(ax, campo):
            if not fisse:
                return ax.contourf(LON, LAT, campo, levels=liv, colors=cols, zorder=1)
            if campo.max() < liv[0]:
                return None
            cs = ax.contourf(LON, LAT, campo, levels=liv, colors=cols[:-1], extend="max", zorder=1)
            cs.cmap.set_over(cols[-1])
            cs.changed()
            return cs

        H = HEAD + righe_tile * PASSO + CB + foot(testo)
        fig = plt.figure(figsize=(W, H), facecolor=SFONDO)
        sotto = f"ogni 6 ore da {GIORNI[tempi[0].weekday()]} {tempi[0]:%H:%M}"
        if f.get("nota"):
            sotto += " · " + f["nota"]
        intestazione(fig, H, f"{f['titolo']} ({f['unita']})", sotto)

        frecce = f.get("frecce")

        for i, k in enumerate(idx):
            r, col = divmod(i, 3)
            x0 = MARG + col * (SIDE + GAP)
            alto = HEAD + r * PASSO + ETICH
            ax = fig.add_axes([x0 / W, 1 - (alto + SIDE) / H, SIDE / W, SIDE / H])
            ax.set_facecolor(SFONDO)
            ax.set_xlim(LON_MIN, LON_MAX)
            ax.set_ylim(LAT_MIN, LAT_MAX)
            ax.set_aspect(1 / np.cos(np.radians(LAT0)))
            ax.set_xticks([])
            ax.set_yticks([])
            for sp_ in ax.spines.values():
                sp_.set_color(LINEA)
            riempi(ax, dati[k])
            ax.contour(lon_f, lat_f, z_f, levels=LIVELLI_QUOTA, colors=TENUE,
                       linewidths=0.4, alpha=0.8, zorder=2)
            if frecce is not None:
                disegna_flusso(ax, lon_f, lat_f, frecce[0][k], frecce[1][k], densita=0.9, spessore=0.45, scala=1.7)
            ax.plot(STAZIONE[2], STAZIONE[1], "*", ms=8, color=INK, mec=SFONDO, mew=0.6, zorder=8)
            ax.text(0.96, 0.04, f["fmt"].format(dati[k][iy, ix]), transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=11, weight="bold", color=INK, zorder=9, path_effects=halo)
            fig.text(x0 / W, 1 - (alto - 0.05) / H, f"{GIORNI[tempi[k].weekday()]} {tempi[k]:%H:%M} · +{k} h",
                     fontsize=10.5, weight="bold", color=INK, va="bottom")

        # barra dei colori: stessi livelli dei riquadri, disegnata su un asse nascosto
        asse = fig.add_axes([0, 0, 0.01, 0.01])
        asse.set_visible(False)
        ref = np.linspace(liv[0], liv[-1] * (1.5 if fisse else 1.0), LON.size).reshape(LON.shape)
        cs = riempi(asse, ref)
        y_cb = HEAD + righe_tile * PASSO + 0.35
        cax = fig.add_axes([(W - 5.0) / 2 / W, 1 - (y_cb + 0.17) / H, 5.0 / W, 0.17 / H])
        cb = fig.colorbar(cs, cax=cax, orientation="horizontal")
        tk = tick_utili(liv, 14 if fisse else 10)
        cb.set_ticks(tk)
        cb.set_ticklabels([f"{t:g}" for t in tk])
        cb.ax.tick_params(labelsize=10 if len(tk) > 10 else 11, colors=SEC, length=3)
        cb.outline.set_visible(False)

        fig.text(MARG / W, 1 - (HEAD + righe_tile * PASSO + CB + 0.05) / H, testo, fontsize=9.5, color=SEC,
                 va="top", linespacing=1.4)
        fig.savefig(percorso, dpi=150, facecolor=SFONDO)
        plt.close(fig)
        creati.append(percorso)
    return creati


def main():
    ap = argparse.ArgumentParser(description="Mappe meteo 48 h sul Lazio")
    ap.add_argument("--simula", action="store_true", help="usa dati inventati invece di Open-Meteo")
    ap.add_argument("--modello", default="best_match",
                    help="modello Open-Meteo (es. italia_meteo_arpae_icon_2i = ICON-2I a ~2 km, icon_eu, ecmwf_ifs025)")
    ap.add_argument("--grossa", type=int, default=0,
                    help="punti per lato con cui si legge il modello (0 = automatico: 31, uno ogni ~8,5 km, "
                         "per i modelli a 2 km; 13 per gli altri)")
    ap.add_argument("--ora", type=int, default=None, help="se indicato, 2 immagini di dettaglio (con i paesi) a quell'ora (0-48)")
    ap.add_argument("--n", type=int, default=80, help="punti per lato della griglia fine (default 80, ~3 km; con WRF si usano 129, ~2 km)")
    ap.add_argument("--inizio", default=None, help="solo con --simula: ora di partenza ISO, es. 2026-10-16T08:00")
    ap.add_argument("--out", default="output", help="cartella di destinazione delle immagini")
    ap.add_argument("--senza-stazioni", action="store_true", help="non usare le stazioni Wunderground")
    ap.add_argument("--max-stazioni", type=int, default=100000, help="massimo di stazioni Wunderground da leggere (default: tutte)")
    a = ap.parse_args()

    global N_GROSSA
    if a.grossa:
        N_GROSSA = a.grossa
    elif not a.simula and any(s in a.modello for s in ("icon_2i", "icon_d2", "arome")):
        N_GROSSA = 31                      # un punto ogni ~8,5 km su 256 km: tanti punti, ma Open-Meteo è gratuito e a pacchetti

    Path(a.out).mkdir(parents=True, exist_ok=True)
    lat_f, lon_f = griglia_fine(a.n)
    passo_km = 2 * MEZZO_LAT * 111.32 / (a.n - 1)
    passo_modello_km = 2 * MEZZO_LAT * 111.32 / (N_GROSSA - 1)

    if a.simula:
        ora0 = datetime.fromisoformat(a.inizio) if a.inizio else datetime.now().replace(minute=0, second=0, microsecond=0)
        d = dati_simulati(ora0)
        z_f = dem_simulato(*np.meshgrid(lat_f, lon_f, indexing="ij"))
        banner = "SIMULAZIONE: DATI INVENTATI, NON È UNA PREVISIONE"
        modello_txt = "Campi e terreno generati al computer per provare codice e grafica."
    else:
        try:
            import requests
        except ImportError:
            sys.exit("Manca il modulo requests:  pip install requests")
        try:
            d = scarica_open_meteo(a.modello)
        except (requests.RequestException, KeyError, ValueError) as e:
            sys.exit("Impossibile scaricare i dati: " + _pulisci(str(e)))
        try:
            z_f = quote_reali(lat_f, lon_f)
            quote_ok = True
        except (requests.RequestException, KeyError, ValueError) as e:
            print("Quote del terreno non scaricabili, uso quelle interpolate dal modello:",
                  _pulisci(str(e))[:120], file=sys.stderr)
            z_f = np.clip(interpola(d["z_c"][None], d["lat_c"], d["lon_c"], lat_f, lon_f)[0], 0, None)
            quote_ok = False
        banner = f"Dati reali Open-Meteo · modello {a.modello}"
        modello_txt = (f"Griglia a {passo_km:.1f} km; il modello è letto ogni {passo_modello_km:.1f} km, "
                       "i dettagli più fini vengono dalla correzione di quota "
                       "(T −6,5 °C/km, rugiada −1,8 °C/km, vento più forte in quota).")
        if not quote_ok:
            modello_txt += " Quote del terreno approssimate: i dettagli del rilievo sono meno fedeli."

    tempi = d["tempi"]
    campi = downscaling(d, z_f, lat_f, lon_f)

    if not a.simula and not a.senza_stazioni:
        chiave = os.environ.get("WU_API_KEY")
        if chiave:
            SEGRETI.append(chiave)
            try:
                scelte = diradamento(cerca_stazioni(chiave), a.max_stazioni)
                campi, info = correggi_con_stazioni(campi, tempi, leggi_stazioni(chiave, scelte), z_f, lat_f, lon_f)
                print(info["testo"])
                if info["n"] >= 3:
                    banner = f"Open-Meteo ({a.modello}) + {info['n']} stazioni WU"
                    modello_txt += " " + info["nota"]
            except Exception as e:                     # la correzione è facoltativa
                print("Stazioni Wunderground non disponibili:", _pulisci(str(e)), file=sys.stderr)
        else:
            print("Nessuna WU_API_KEY impostata: salto la correzione con le stazioni.", file=sys.stderr)
    if a.ora is None:
        creati = disegna_fogli(campi, z_f, lat_f, lon_f, tempi, banner, modello_txt, a.out)
    else:
        k = max(0, min(a.ora, len(tempi) - 1))
        creati = disegna_mappe(campi, z_f, lat_f, lon_f, k, tempi, banner, modello_txt, a.out, a.simula)
    for f in creati:
        print("Creato:", f)


if __name__ == "__main__":
    main()
