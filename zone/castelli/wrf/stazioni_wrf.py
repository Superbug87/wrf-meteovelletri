#!/usr/bin/env python3
"""
Stazioni Weather Underground -> file delle osservazioni per il nudging di WRF (OBS_DOMAIN101/201/301).

  WU_API_KEY=... stazioni_wrf.py --ciclo 2026101600 --geo ~/wrf-geo --out run

Cosa fa:
  1. cerca le stazioni intorno ai Castelli Romani: TUTTE nel dominio a 1 km (ricerca a riquadri che si dividono,
     in parallelo, ~40 chiamate/s; WU_CHIAMATE_AL_SECONDO e WU_THREAD le regolano), più rade nei due più grandi;
  2. di ognuna legge la posizione/quota attuale e lo storico delle ultime 24 ore (dati ogni 5 minuti);
  3. controlla i dati (valori plausibili, quota dichiarata coerente col terreno di WRF, scarto dalle stazioni
     vicine riportate alla stessa quota) e li riduce a una media ogni 15 minuti;
  4. scrive per ogni dominio un file nel formato che WRF legge da solo (temperatura, umidità, pressione);
  5. scrive `obs_fdda_end.txt`: i minuti dall'inizio del ciclo fino all'ultima osservazione, cioè quando
     il nudging smette e il modello prosegue libero.

Il vento non viene scritto di default: gli anemometri amatoriali sono quasi sempre riparati da case e alberi.
Se non ci sono la chiave, stazioni o dati utili, non scrive nulla e WRF gira senza nudging (esito 0).
"""
import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

URL_VICINE = "https://api.weather.com/v3/location/near"
URL_ATTUALE = "https://api.weather.com/v2/pws/observations/current"
URL_GIORNO = "https://api.weather.com/v2/pws/observations/all/1day"
MANCANTE = -888888.0
SEGRETI = []
LAT0, LON0 = 41.71, 12.73
PASSO_MIN = 15                       # minuti: media delle misure ogni 5 minuti in finestre da 15

# quanti punti al massimo per dominio: nel dominio a 1 km (Castelli) si usano TUTTE le stazioni trovate,
# nei due più grandi si dirada (le stazioni rare vanno tenute, le fitte no)
MAX_D03, MAX_D02, MAX_D01 = 10**6, 30, 20
CHIAMATE_AL_SECONDO = float(os.environ.get("WU_CHIAMATE_AL_SECONDO", "40"))
THREAD = int(os.environ.get("WU_THREAD", "24"))
ETA_MASSIMA_ELENCO = timedelta(hours=48)    # dall'elenco della ricerca si scartano solo le stazioni mute da due giorni
ETA_MASSIMA_DATO = timedelta(hours=3)       # l'ultimo dato letto deve essere di meno di 3 ore fa
LATO_INIZIALE = 0.15                        # gradi: riquadri di partenza della ricerca nel dominio a 1 km
LATO_MINIMO = 0.005                         # gradi: sotto questo (circa mezzo km) non si suddivide più
MAX_RICERCHE = 8000                         # rete di sicurezza
TOLLERANZA_QUOTA = {3: 250.0, 2: 400.0, 1: 600.0}      # m: quota dichiarata - terreno del modello
CELLE_KM = {3: 3.0, 2: 9.0, 1: 27.0}                   # superosservazioni WU con NUDGING_FONTI=tutte
RISOLUZIONI = {3: "1 km", 2: "3 km", 1: "9 km"}


def pulisci(testo):
    for s in SEGRETI:
        testo = testo.replace(s, "***")
    return re.sub(r"apiKey=[^&\s'\"]+", "apiKey=***", testo)


# ---------------------------------------------------------------------------------------------
# Chiamate a Wunderground (massimo 30 al minuto)
# ---------------------------------------------------------------------------------------------


class Limitatore:
    """Distanzia le chiamate di tutti i thread (al massimo `al_secondo`) e permette una pausa generale."""

    def __init__(self, al_secondo):
        self.passo = 1.0 / max(al_secondo, 0.1)
        self.prossimo = 0.0
        self.fino_a = 0.0
        self.lucchetto = threading.Lock()

    def aspetta(self):
        with self.lucchetto:
            ora = time.monotonic()
            inizio = max(ora, self.prossimo, self.fino_a)
            self.prossimo = inizio + self.passo
        if inizio > ora:
            time.sleep(inizio - ora)

    def pausa(self, secondi):
        with self.lucchetto:
            self.fino_a = max(self.fino_a, time.monotonic() + secondi)


LIMITE = Limitatore(CHIAMATE_AL_SECONDO)
_locale = threading.local()
_stato = {"di_fila": 0, "esaurita": False, "chiamate": 0}
_lucchetto_stato = threading.Lock()


def troppe_richieste():
    """True se il servizio ha rifiutato più volte di fila: da qui in poi non si chiama più."""
    return _stato["esaurita"]


def wu_get(url, params, tentativi=4):
    """Ritorna il JSON, None se non ci sono dati (204/404) o il servizio insiste a non rispondere."""
    import requests

    for i in range(tentativi):
        if _stato["esaurita"]:
            return None
        LIMITE.aspetta()
        try:
            if getattr(_locale, "sessione", None) is None:
                _locale.sessione = requests.Session()
            r = _locale.sessione.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            _locale.sessione = None
            print(f"Wunderground non risponde ({pulisci(str(e))[:120]}), riprovo", file=sys.stderr)
            time.sleep(3 * (i + 1))
            continue
        with _lucchetto_stato:
            _stato["chiamate"] += 1
        if r.status_code in (204, 404):
            return None
        if r.status_code in (401, 403):
            sys.exit(f"Wunderground rifiuta la chiave (HTTP {r.status_code}): controlla il segreto WU_API_KEY.")
        if r.status_code == 429:
            with _lucchetto_stato:
                _stato["di_fila"] += 1
                if _stato["di_fila"] >= 4 and not _stato["esaurita"]:
                    _stato["esaurita"] = True
                    print("Wunderground rifiuta ancora per troppe richieste: smetto di chiamare "
                          "(limite giornaliero?) e uso quel che ho.", file=sys.stderr)
            attesa = min(int(r.headers.get("Retry-After", 0) or 0) or 20 * (i + 1), 120)
            print(f"Wunderground risponde 429, pausa generale di {attesa} s", file=sys.stderr)
            LIMITE.pausa(attesa)
            continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(5 * (i + 1))
            continue
        try:
            r.raise_for_status()
            dati = r.json()
        except (requests.RequestException, ValueError):
            return None
        _stato["di_fila"] = 0
        return dati
    return None


# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# Domini di WRF (da geo_em.d0N.nc) e posizione delle stazioni
# ---------------------------------------------------------------------------------------------
class Dominio:
    def __init__(self, numero, percorso):
        from netCDF4 import Dataset

        with Dataset(percorso) as ds:
            self.lat = np.array(ds["XLAT_M"][0], dtype=float)
            self.lon = np.array(ds["XLONG_M"][0], dtype=float)
            self.hgt = np.array(ds["HGT_M"][0], dtype=float)
        self.n = numero
        self.bordo = np.concatenate([
            np.c_[self.lon[0, :], self.lat[0, :]], np.c_[self.lon[:, -1], self.lat[:, -1]],
            np.c_[self.lon[-1, ::-1], self.lat[-1, ::-1]], np.c_[self.lon[::-1, 0], self.lat[::-1, 0]]])

    def contiene(self, lat, lon):
        x, y = lon, lat
        px, py = self.bordo[:, 0], self.bordo[:, 1]
        dentro = False
        j = len(px) - 1
        for i in range(len(px)):                         # punto in poligono (raggi)
            if (py[i] > y) != (py[j] > y) and x < (px[j] - px[i]) * (y - py[i]) / (py[j] - py[i]) + px[i]:
                dentro = not dentro
            j = i
        return dentro

    def quota(self, lat, lon):
        d = (self.lat - lat) ** 2 + ((self.lon - lon) * np.cos(np.radians(lat))) ** 2
        return float(self.hgt.flat[int(np.argmin(d))])


def carica_domini(geo):
    out = {}
    for n in (1, 2, 3):
        p = Path(geo) / f"geo_em.d0{n}.nc"
        if p.exists():
            out[n] = Dominio(n, p)
    if set(out) != {1, 2, 3}:
        sys.exit(f"Mancano i file geo_em.d0N.nc in {geo}")
    return out


def dominio_fine(domini, lat, lon):
    """Il dominio più piccolo (numero più alto) che contiene il punto; 0 se è fuori da tutti."""
    for n in (3, 2, 1):
        if domini[n].contiene(lat, lon):
            return n
    return 0


# ---------------------------------------------------------------------------------------------
# Ricerca e lettura delle stazioni
# ---------------------------------------------------------------------------------------------
def _km(la, lo, la_c, lo_c):
    return np.hypot((la - la_c) * 111.19, (lo - lo_c) * 111.19 * np.cos(np.radians(la_c)))


def _coperta(cella, dischi):
    """True se il riquadro sta tutto dentro uno dei cerchi già interrogati (un cerchio è convesso: bastano i 4 angoli)."""
    cla, clo, r = dischi
    if len(cla) == 0:
        return False
    a, b, c, d = cella
    dentro = np.ones(len(cla), bool)
    for la in (a, b):
        for lo in (c, d):
            dentro &= _km(la, lo, cla, clo) <= r * 0.97
    return bool(dentro.any())


def _tocca(dominio, cella):
    a, b, c, d = cella
    return any(dominio.contiene(la, lo) for la, lo in ((a, c), (a, d), (b, c), (b, d), ((a + b) / 2, (c + d) / 2)))


def vicine_a(chiave, la, lo):
    """Le (fino a) 10 stazioni più vicine al punto: ([(id, lat, lon, ultimo aggiornamento)], raggio coperto in km) o None."""
    p = {"geocode": f"{la:.5f},{lo:.5f}", "product": "pws", "format": "json", "apiKey": chiave}
    r = wu_get(URL_VICINE, p)
    try:
        loc = r["location"]
        agg = loc.get("updateTimeUtc") or [None] * len(loc["stationId"])
        elenco = []
        for sid, sla, slo, t in zip(loc["stationId"], loc["latitude"], loc["longitude"], agg):
            if sid and sla is not None and slo is not None:
                elenco.append((sid, float(sla), float(slo), t))
        dist = [d for d in loc.get("distanceKm", []) if d is not None]
        raggio = max(dist) if len(elenco) >= 10 and dist else float("inf")
        return elenco, raggio
    except (KeyError, TypeError, ValueError):
        return None


def cerca_fitta(chiave, dominio, adesso):
    """
    Tutte le stazioni nell'area di un dominio. Ogni ricerca dà le 10 stazioni più vicine al punto: dentro il cerchio che arriva
    alla decima non ne manca nessuna. Si parte da riquadri grandi e, se il cerchio non li copre, si dividono in 4 (a ondate,
    in parallelo), saltando i riquadri già coperti. Ritorna {id: (lat, lon)} e un riassunto.
    """
    d3 = dominio
    la0, la1, lo0, lo1 = d3.lat.min(), d3.lat.max(), d3.lon.min(), d3.lon.max()
    lato = LATO_INIZIALE
    celle = [(a, a + lato, b, b + lato) for a in np.arange(la0 - lato / 2, la1, lato) for b in np.arange(lo0 - lato / 2, lo1, lato)]
    celle = [c for c in celle if _tocca(d3, c)]
    trovate, dischi_l = {}, []
    ricerche = fallite = non_esaurite = 0
    limite_eta = adesso - ETA_MASSIMA_ELENCO
    vecchie = 0
    with ThreadPoolExecutor(THREAD) as pool:
        while celle and not troppe_richieste():
            dischi = tuple(np.array(x) for x in zip(*dischi_l)) if dischi_l else (np.array([]),) * 3
            da_fare = [c for c in celle if not _coperta(c, dischi)]
            if ricerche + len(da_fare) > MAX_RICERCHE:
                print(f"!! Ricerca interrotta a {ricerche} interrogazioni (rete di sicurezza)", file=sys.stderr)
                break
            centri = [((c[0] + c[1]) / 2, (c[2] + c[3]) / 2) for c in da_fare]
            risposte = list(pool.map(lambda x: vicine_a(chiave, *x), centri))
            ricerche += len(da_fare)
            prossime = []
            for cella, (cla, clo), ris in zip(da_fare, centri, risposte):
                if ris is None:
                    fallite += 1
                    continue
                elenco, raggio = ris
                dischi_l.append((cla, clo, raggio))
                for sid, sla, slo, t in elenco:
                    if t is not None and datetime.fromtimestamp(int(t), tz=timezone.utc) < limite_eta:
                        if sid not in trovate:
                            vecchie += 1
                        continue
                    trovate[sid] = (sla, slo)
                a, b, c, d = cella
                if max(_km(la, lo, cla, clo) for la in (a, b) for lo in (c, d)) <= raggio * 0.97:
                    continue                                   # il cerchio copre tutto il riquadro: finito
                if b - a <= LATO_MINIMO * 1.5:
                    non_esaurite += 1                          # troppo fitto anche così: ci si ferma
                    continue
                m1, m2 = (a + b) / 2, (c + d) / 2
                prossime += [x for x in ((a, m1, c, m2), (a, m1, m2, d), (m1, b, c, m2), (m1, b, m2, d)) if _tocca(d3, x)]
            celle = prossime
    return trovate, {"ricerche": ricerche, "fallite": fallite, "non_esaurite": non_esaurite, "mute": vecchie}


def cerca(chiave, domini, adesso):
    """Dominio a 1 km: tutte le stazioni (ricerca a riquadri). Domini a 3 e 9 km: griglia rada (poi si dirada)."""
    trovate, riass = cerca_fitta(chiave, domini[3], adesso)
    box = {n: (domini[n].lat.min(), domini[n].lat.max(), domini[n].lon.min(), domini[n].lon.max()) for n in (2, 1)}
    punti = []
    for n, passo in ((2, 0.30), (1, 0.80)):
        la0, la1, lo0, lo1 = box[n]
        punti += [(la, lo) for la in np.arange(la0 + passo / 2, la1, passo) for lo in np.arange(lo0 + passo / 2, lo1, passo)]
    with ThreadPoolExecutor(THREAD) as pool:
        for ris in pool.map(lambda x: vicine_a(chiave, *x), punti):
            if ris:
                for sid, sla, slo, _ in ris[0]:
                    trovate.setdefault(sid, (sla, slo))
    return trovate, riass


def diradamento(ids, pos, massimo):
    """Se sono più di `massimo` ne tiene `massimo`, il più possibile distanziate tra loro."""
    ids = list(ids)
    if len(ids) <= massimo:
        return ids
    k = 111.32
    xy = {i: ((pos[i][1] - LON0) * k * np.cos(np.radians(LAT0)), (pos[i][0] - LAT0) * k) for i in ids}
    scelte = [min(ids, key=lambda i: np.hypot(*xy[i]))]
    dmin = {i: np.hypot(xy[i][0] - xy[scelte[0]][0], xy[i][1] - xy[scelte[0]][1]) for i in ids}
    while len(scelte) < massimo:
        nuovo = max((i for i in ids if i not in scelte), key=lambda i: dmin[i])
        scelte.append(nuovo)
        for i in ids:
            dmin[i] = min(dmin[i], np.hypot(xy[i][0] - xy[nuovo][0], xy[i][1] - xy[nuovo][1]))
    return scelte


def numero(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def leggi_attuale(chiave, sid):
    """(lat, lon, quota in m, ora UTC dell'ultimo dato) oppure None."""
    p = {"stationId": sid, "format": "json", "units": "m", "numericPrecision": "decimal", "apiKey": chiave}
    r = wu_get(URL_ATTUALE, p)
    try:
        o = r["observations"][0]
        m = o.get("metric", {})
        ora = datetime.fromisoformat(o["obsTimeUtc"].replace("Z", "+00:00"))
        return float(o["lat"]), float(o["lon"]), numero(m.get("elev")), ora
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def leggi_giorno(chiave, sid):
    """Storico delle ultime 24 ore: lista di (istante UTC, T °C, umidità %, pressione hPa), valori mancanti = None."""
    p = {"stationId": sid, "format": "json", "units": "m", "numericPrecision": "decimal", "apiKey": chiave}
    r = wu_get(URL_GIORNO, p)
    out = []
    try:
        for o in r["observations"]:
            m = o.get("metric", {})
            t = numero(m.get("tempAvg", m.get("temp")))
            rh = numero(o.get("humidityAvg", o.get("humidity")))
            pmax, pmin = numero(m.get("pressureMax")), numero(m.get("pressureMin"))
            pr = (pmax + pmin) / 2 if pmax is not None and pmin is not None else numero(m.get("pressure"))
            istante = datetime.fromtimestamp(int(o["epoch"]), tz=timezone.utc)
            out.append((istante, t, rh, pr))
    except (KeyError, TypeError, ValueError):
        return []
    return out


def a_blocchi(serie, t0):
    """Media su blocchi da PASSO_MIN minuti a partire da t0. Ritorna {indice_blocco: (istante medio, T, RH, P)}."""
    blocchi = {}
    for istante, t, rh, pr in serie:
        if istante < t0:
            continue
        k = int((istante - t0).total_seconds() // (PASSO_MIN * 60))
        blocchi.setdefault(k, []).append((istante, t, rh, pr))
    out = {}
    for k, v in blocchi.items():
        def media(i, lo, hi):
            x = [e[i] for e in v if e[i] is not None and lo <= e[i] <= hi]
            return float(np.mean(x)) if x else None
        sec = np.mean([e[0].timestamp() for e in v])
        istante = datetime.fromtimestamp(round(sec / 60) * 60, tz=timezone.utc)
        out[k] = (istante, media(1, -30.0, 50.0), media(2, 3.0, 100.0), media(3, 980.0, 1045.0))
    return out


# ---------------------------------------------------------------------------------------------
# Controlli di qualità
# ---------------------------------------------------------------------------------------------
def controlla(stazioni):
    """
    stazioni: {sid: {"quota": m, "blocchi": {k: (istante, T, RH, P)}}}
    Scarta i singoli valori che si discostano dalle altre stazioni (riportate a livello del mare con 6,5 K/km)
    e poi le stazioni che ne hanno scartati più della metà. Ritorna quante ne ha eliminate.
    """
    tutti_k = sorted({k for s in stazioni.values() for k in s["blocchi"]})
    scartati = {sid: 0 for sid in stazioni}
    totale = {sid: len(s["blocchi"]) for sid, s in stazioni.items()}
    for k in tutti_k:
        ids = [sid for sid, s in stazioni.items() if k in s["blocchi"] and s["blocchi"][k][1] is not None]
        if len(ids) >= 5:
            red = np.array([stazioni[i]["blocchi"][k][1] + 0.0065 * stazioni[i]["quota"] for i in ids])
            med = np.median(red)
            mad = 1.4826 * np.median(np.abs(red - med))
            for i, v in zip(ids, red):
                if abs(v - med) > max(3 * mad, 4.0):
                    istante, _t, rh, pr = stazioni[i]["blocchi"][k]
                    stazioni[i]["blocchi"][k] = (istante, None, rh, pr)
                    scartati[i] += 1
        ids = [sid for sid, s in stazioni.items() if k in s["blocchi"] and s["blocchi"][k][2] is not None]
        if len(ids) >= 5:
            rh = np.array([stazioni[i]["blocchi"][k][2] for i in ids])
            med = np.median(rh)
            mad = 1.4826 * np.median(np.abs(rh - med))
            for i, v in zip(ids, rh):
                if abs(v - med) > max(3 * mad, 35.0):
                    istante, t, _rh, pr = stazioni[i]["blocchi"][k]
                    stazioni[i]["blocchi"][k] = (istante, t, None, pr)
                    scartati[i] += 1
    eliminate = [sid for sid in stazioni if totale[sid] == 0 or scartati[sid] > totale[sid]]
    for sid in eliminate:
        del stazioni[sid]
    return len(eliminate)


# ---------------------------------------------------------------------------------------------
# Scrittura nel formato di WRF (vedi share/wrf_fddaobs_in.F, subroutine in4dob)
# ---------------------------------------------------------------------------------------------
def f(x):
    return f"{x:11.3f}"


def record(istante, lat, lon, sid, quota, T, rh, p, vento=None):
    """Cinque righe per ogni osservazione di superficie. T in °C, rh in %, p (livello del mare) in hPa."""
    if T is None:                       # senza temperatura l'umidità non serve a WRF
        return ""
    t_k, q_t = f(T + 273.15), f(0.0)
    slp = f(p * 100.0) if p is not None else f(MANCANTE)
    q_s = f(0.0) if p is not None else f(MANCANTE)
    h = f(min(rh, 99.0)) if rh is not None else f(MANCANTE)
    q_h = f(0.0) if rh is not None else f(MANCANTE)
    u = v = f(MANCANTE)
    q_u = q_v = f(MANCANTE)
    if vento is not None:
        u, v, q_u, q_v = f(vento[0]), f(vento[1]), f(129.0), f(129.0)      # 129 = vento riferito ai punti cardinali
    mis = f(MANCANTE)
    riga1 = f" {istante:%Y%m%d%H%M%S}"
    riga2 = f"  {lat:9.4f} {lon:9.4f} "
    riga3 = "  " + sid[:40].ljust(40) + "   " + ("WU " + sid)[:40].ljust(40) + "   "
    riga4 = ("  " + "FM-12 SYNOP".ljust(16) + "  " + "WUNDERGROUND".ljust(16) + "  " + f"{quota:8.1f}" + "  "
             + "   F" + "  " + "   F" + "  " + f"{1:5d}")
    campi = [slp, q_s, mis, mis, mis, mis, t_k, q_t, u, q_u, v, q_v, h, q_h, mis, mis, mis, mis]
    riga5 = " " + "".join(c + " " for c in campi)
    return "\n".join((riga1, riga2, riga3, riga4, riga5)) + "\n"


def in_ordine(stazioni):
    """Tutte le osservazioni, in ordine di tempo (WRF lo pretende)."""
    righe = []
    for sid, s in stazioni.items():
        for k, (istante, T, rh, pr) in s["blocchi"].items():
            righe.append((istante, sid, s, T, rh, pr))
    righe.sort(key=lambda r: (r[0], r[1]))
    return righe


def leggi_stazione(chiave, sid, domini, t0, adesso):
    """Legge una stazione: ({dati della stazione}, None) oppure (None, motivo dello scarto). Gira in un thread."""
    att = leggi_attuale(chiave, sid)
    if troppe_richieste():
        return None, "non lette"
    if att is None or adesso - att[3] > ETA_MASSIMA_DATO:
        return None, "senza dati recenti"
    la, lo, quota, _ = att
    n = dominio_fine(domini, la, lo)
    if not n:
        return None, "fuori dai domini"
    terreno = domini[n].quota(la, lo)
    if quota is None or not (-50 < quota < 2500):
        quota = terreno
    elif abs(quota - terreno) > TOLLERANZA_QUOTA[n]:
        return None, "quota incoerente"
    blocchi = a_blocchi(leggi_giorno(chiave, sid), t0)
    if not blocchi:
        return None, "senza storico"
    return {"lat": la, "lon": lo, "quota": quota, "dominio": n, "blocchi": blocchi}, None


def leggi_tutte(chiave, ids, domini, t0, adesso):
    stazioni = {}
    motivi = {"senza dati recenti": 0, "quota incoerente": 0, "senza storico": 0, "fuori dai domini": 0, "non lette": 0}
    fatte = 0
    passo = max(len(ids) // 10, 1)
    with ThreadPoolExecutor(THREAD) as pool:
        for sid, (dati, motivo) in zip(ids, pool.map(lambda i: leggi_stazione(chiave, i, domini, t0, adesso), ids)):
            fatte += 1
            if fatte % passo == 0:
                print(f"  lette {fatte} stazioni su {len(ids)}", flush=True)
            if dati is not None:
                stazioni[sid] = dati
            else:
                motivi[motivo] += 1
    return stazioni, motivi


def leggi_wu(chiave, domini, t0, adesso):
    """Ricerca e lettura delle stazioni Wunderground nei domini: {sid: dati} (prima del controllo di qualità)."""
    inizio = time.monotonic()
    print("Ricerca delle stazioni...", flush=True)
    trovate, riass = cerca(chiave, domini, adesso)
    print(f"Stazioni trovate: {len(trovate)} con {riass['ricerche']} ricerche nel dominio a 1 km in "
          f"{time.monotonic() - inizio:.0f} s (mute da più di 2 giorni: {riass['mute']}; ricerche fallite: "
          f"{riass['fallite']}; riquadri troppo fitti per essere esauriti: {riass['non_esaurite']})", flush=True)
    per_dom = {1: [], 2: [], 3: []}
    for sid, (la, lo) in trovate.items():
        n = dominio_fine(domini, la, lo)
        if n:
            per_dom[n].append(sid)
    scelte = (diradamento(per_dom[3], {s: trovate[s] for s in per_dom[3]}, MAX_D03)
              + diradamento(per_dom[2], {s: trovate[s] for s in per_dom[2]}, MAX_D02)
              + diradamento(per_dom[1], {s: trovate[s] for s in per_dom[1]}, MAX_D01))
    print(f"Stazioni scelte: {len(scelte)} (dominio 3: {min(len(per_dom[3]), MAX_D03)}, "
          f"dominio 2: {min(len(per_dom[2]), MAX_D02)}, dominio 1: {min(len(per_dom[1]), MAX_D01)})", flush=True)
    inizio = time.monotonic()
    stazioni, motivi = leggi_tutte(chiave, scelte, domini, t0, adesso)
    print("Scartate prima del controllo: " + ", ".join(f"{v} {k}" for k, v in motivi.items() if v) +
          f"; restano {len(stazioni)} (lettura in {time.monotonic() - inizio:.0f} s, "
          f"{_stato['chiamate']} chiamate in tutto)", flush=True)

    return stazioni


def importa_ufficiali():
    """strumenti/stazioni_ufficiali.py: accanto a wrf/ nel run di GitHub, tre livelli sopra nel repository."""
    qui = Path(__file__).resolve().parent
    for c in (qui.parent / "strumenti", qui.parents[2] / "strumenti"):
        if (c / "stazioni_ufficiali.py").exists():
            sys.path.insert(0, str(c))
            import stazioni_ufficiali
            return stazioni_ufficiali
    raise FileNotFoundError("strumenti/stazioni_ufficiali.py")


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
    ap.add_argument("--ciclo", required=True, help="AAAAMMGGHH, inizio della previsione (UTC)")
    ap.add_argument("--geo", required=True, help="cartella con geo_em.d01.nc, geo_em.d02.nc, geo_em.d03.nc")
    ap.add_argument("--out", default=".", help="dove scrivere OBS_DOMAIN101/201/301 e obs_fdda_end.txt")
    ap.add_argument("--vento", action="store_true", help="scrive anche il vento (sconsigliato)")
    ap.add_argument("--prova", action="store_true", help="usa stazioni inventate (solo per collaudo)")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = datetime.strptime(a.ciclo, "%Y%m%d%H").replace(tzinfo=timezone.utc)
    adesso = datetime.now(timezone.utc)
    domini = carica_domini(a.geo)
    modo = (os.environ.get("NUDGING_FONTI", "") or "wu").strip().lower()
    if modo not in ("wu", "ufficiali", "tutte"):
        print(f"NUDGING_FONTI={modo} non previsto: uso wu")
        modo = "wu"
    print(f"Stazioni per il nudging: {dict(wu='Wunderground', ufficiali='Regione Lazio e Aeronautica', tutte='ufficiali + Wunderground')[modo]}", flush=True)

    stazioni = {}
    if a.prova:
        prova_ricerca(domini, adesso)
        stazioni = stazioni_finte(domini, t0, adesso)
    elif modo != "ufficiali":
        chiave = os.environ.get("WU_API_KEY", "")
        if not chiave:
            print("WU_API_KEY non impostata: niente stazioni Wunderground.")
        elif adesso - t0 > timedelta(hours=23):
            print("Il ciclo è più vecchio di 23 ore: lo storico a 24 ore di Wunderground non basta, niente stazioni WU.")
        else:
            chiave = _chiave_wu(chiave)
            SEGRETI.append(chiave)
            stazioni = leggi_wu(chiave, domini, t0, adesso)
    if stazioni:
        eliminate = controlla(stazioni)
        print(f"Eliminate dal confronto con le vicine: {eliminate}; restano {len(stazioni)}")

    if modo != "wu" and not a.prova:
        try:
            SU = importa_ufficiali()
            SU.prepara(modo, stazioni, domini, t0, adesso, out, TOLLERANZA_QUOTA, CELLE_KM, LAT0, LON0, RISOLUZIONI)
            return
        except Exception as e:                       # le ufficiali non devono far perdere le Wunderground
            print(f"!! Stazioni ufficiali non utilizzabili ({type(e).__name__}: {e}): si usano solo le Wunderground", flush=True)
            for f_ in ("OBS_DOMAIN101", "OBS_DOMAIN201", "OBS_DOMAIN301", "obs_fdda_end.txt", "obs_fonti.txt"):
                (out / f_).unlink(missing_ok=True)
    if not stazioni:
        print("Nessuna stazione utilizzabile: WRF girerà senza nudging.")
        return

    righe = in_ordine(stazioni)
    testi = {1: [], 2: [], 3: []}
    ultimo = None
    for istante, sid, s, T, rh, pr in righe:
        rec = record(istante, s["lat"], s["lon"], sid, s["quota"], T, rh, pr)
        if not rec:
            continue
        ultimo = istante if ultimo is None or istante > ultimo else ultimo
        for n in (1, 2, 3):
            if domini[n].contiene(s["lat"], s["lon"]):
                testi[n].append(rec)
    if ultimo is None or not testi[3] and not testi[2]:
        print("Nessuna osservazione utile dopo i controlli: WRF girerà senza nudging.")
        return
    for n in (1, 2, 3):
        (out / f"OBS_DOMAIN{n}01").write_text("".join(testi[n]))
    minuti = int((ultimo - t0).total_seconds() // 60)
    (out / "obs_fdda_end.txt").write_text(f"{minuti}\n")
    n3 = sum(1 for s in stazioni.values() if domini[3].contiene(s["lat"], s["lon"]))
    print(f"Scritte {sum(len(v) for v in testi.values())} osservazioni di {len(stazioni)} stazioni "
          f"({n3} nel dominio a 1 km); nudging fino a +{minuti // 60} h {minuti % 60:02d} min "
          f"({ultimo:%H:%M} UTC).")


def stazioni_finte(domini, t0, adesso):
    """Stazioni inventate per collaudare scrittura e lettura (nessuna rete)."""
    rng = np.random.default_rng(1)
    d3, d2 = domini[3], domini[2]
    stazioni = {}
    for i in range(24):
        dom = d3 if i < 14 else d2
        j, k = rng.integers(8, dom.lat.shape[0] - 8, 2)
        la, lo = float(dom.lat[j, k]), float(dom.lon[j, k])
        quota = float(dom.hgt[j, k]) + float(rng.normal(0, 20))
        blocchi = {}
        n = int((min(adesso, t0 + timedelta(hours=9)) - t0).total_seconds() // (PASSO_MIN * 60))
        for b in range(n):
            ist = t0 + timedelta(minutes=PASSO_MIN * b + 7)
            ora = ist.hour + ist.minute / 60
            T = 14.0 + 4.0 * np.sin(2 * np.pi * (ora - 9) / 24) - 0.0065 * quota + float(rng.normal(0, 0.4))
            rh = float(np.clip(75 - 3.0 * (T - 14) + rng.normal(0, 3), 20, 98))
            blocchi[b] = (ist, float(T), rh, float(1017 + rng.normal(0, 0.5)))
        stazioni[f"IPROVA{i:03d}"] = {"lat": la, "lon": lo, "quota": quota, "dominio": 3 if i < 14 else 2,
                                      "blocchi": blocchi}
    return stazioni


class ApiFinta:
    """Fa le veci di `location/near` con stazioni inventate: le 10 più vicine al punto, con la distanza in km."""

    def __init__(self, domini, adesso, n_citta=900, n_sparse=400, seme=2):
        rng = np.random.default_rng(seme)
        d3 = domini[3]
        pos = []
        while len(pos) < n_citta + n_sparse:
            if len(pos) < n_citta:                       # un grande centro abitato molto fitto, altri più radi
                cla, clo = float(d3.lat.mean()), float(d3.lon.mean())
                sc = 0.04 if rng.random() < 0.6 else 0.12
                la = cla + rng.normal(0, sc)
                lo = clo + rng.normal(0, sc)
            else:
                la = rng.uniform(d3.lat.min(), d3.lat.max())
                lo = rng.uniform(d3.lon.min(), d3.lon.max())
            if d3.contiene(la, lo):
                pos.append((la, lo))
        self.pos = np.array(pos)
        self.adesso = adesso
        self.chiamate = 0
        self.lucchetto = threading.Lock()

    def __call__(self, url, params, tentativi=4):
        with self.lucchetto:
            self.chiamate += 1
        la, lo = (float(x) for x in params["geocode"].split(","))
        d = _km(self.pos[:, 0], self.pos[:, 1], la, lo)
        o = np.argsort(d)[:10]
        return {"location": {"stationId": [f"IF{i:05d}" for i in o], "latitude": [float(self.pos[i, 0]) for i in o],
                             "longitude": [float(self.pos[i, 1]) for i in o], "distanceKm": [round(float(d[i]), 2) for i in o],
                             "updateTimeUtc": [int(self.adesso.timestamp()) - 600] * len(o)}}


def prova_ricerca(domini, adesso):
    """La ricerca a riquadri trova tutte le stazioni inventate (anche quelle dentro un centro molto fitto)?"""
    global wu_get
    vera = wu_get
    api = ApiFinta(domini, adesso)
    wu_get = api
    try:
        trovate, riass = cerca("finta", domini, adesso)
    finally:
        wu_get = vera
    esistono = {f"IF{i:05d}" for i in range(len(api.pos))}
    mancano = esistono - set(trovate)
    print(f"Ricerca di prova: {len(trovate)} trovate su {len(esistono)} con {api.chiamate} interrogazioni "
          f"(non esaurite: {riass['non_esaurite']}), mancano {len(mancano)}")
    if len(mancano) > 0.005 * len(esistono):
        sys.exit(f"!! La ricerca perde troppe stazioni: {len(mancano)}")


if __name__ == "__main__":
    main()
