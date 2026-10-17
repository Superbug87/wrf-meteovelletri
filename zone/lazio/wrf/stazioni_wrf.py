#!/usr/bin/env python3
"""
Stazioni Weather Underground -> file delle osservazioni per il nudging di WRF (OBS_DOMAIN101/201/301).

  WU_API_KEY=... stazioni_wrf.py --ciclo 2026101600 --geo ~/wrf-geo --out run

Cosa fa:
  1. trova TUTTE le stazioni di Wunderground nell'area del dominio a 2 km (il Lazio e un margine attorno): la ricerca
     dà al massimo 10 stazioni per volta, quindi si divide l'area in riquadri e si suddivide ogni riquadro in cui le
     10 stazioni più vicine non bastano a coprirlo tutto (nei centri abitati si scende fino a riquadri di mezzo km);
  2. di ognuna legge la posizione/quota attuale e lo storico delle ultime 24 ore (dati ogni 5 minuti), con molte
     chiamate in parallelo;
  3. scarta le stazioni ferme da ore, con la quota dichiarata incoerente col terreno di WRF o senza storico; poi
     scarta i singoli valori sballati (confronto con le stazioni vicine riportate alla stessa quota, salti improvvisi)
     e le stazioni con più della metà dei valori scartati o con il sensore bloccato; il resto si riduce a una media
     ogni 15 minuti;
  4. scrive per ogni dominio un file nel formato che WRF legge da solo (temperatura, umidità, pressione);
  5. scrive `obs_fdda_end.txt`: i minuti dall'inizio del ciclo fino all'ultima osservazione, cioè quando
     il nudging smette e il modello prosegue libero.

Nessun tetto al numero di stazioni. Chiamate: circa un decimo del numero di stazioni per la ricerca, due per ogni
stazione per la lettura. Variabili d'ambiente facoltative: WU_CHIAMATE_AL_SECONDO (default 40), WU_THREAD (default 24).
Se il servizio risponde "troppe richieste" più volte di fila si smette di chiamare e si usa quel che si è letto.

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
LAT0, LON0 = 41.82, 12.72
PASSO_MIN = 15                       # minuti: media delle misure ogni 5 minuti in finestre da 15

CHIAMATE_AL_SECONDO = float(os.environ.get("WU_CHIAMATE_AL_SECONDO", "40"))
THREAD = int(os.environ.get("WU_THREAD", "24"))
TOLLERANZA_QUOTA = {3: 400.0, 2: 600.0, 1: 800.0}      # m: quota dichiarata - terreno del modello (a 3 km il rilievo è levigato)
ETA_MASSIMA_ELENCO = timedelta(hours=48)               # dall'elenco della ricerca si scartano solo le stazioni mute da due giorni
ETA_MASSIMA_DATO = timedelta(hours=3)                  # l'ultimo dato letto deve essere di meno di 3 ore fa
LATO_INIZIALE = 0.30                                   # gradi: riquadri di partenza della ricerca (circa 33 x 25 km)
LATO_MINIMO = 0.005                                    # gradi: sotto questo (circa mezzo km) non si suddivide più
MAX_RICERCHE = 8000                                    # rete di sicurezza


def pulisci(testo):
    for s in SEGRETI:
        testo = testo.replace(s, "***")
    return re.sub(r"apiKey=[^&\s'\"]+", "apiKey=***", testo)


# ---------------------------------------------------------------------------------------------
# Chiamate a Wunderground: in parallelo, con un limite comune di chiamate al secondo
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
        """Punto in poligono (raggi), con numpy: serve per migliaia di stazioni."""
        x, y = lon, lat
        px, py = self.bordo[:, 0], self.bordo[:, 1]
        qx, qy = np.roll(px, 1), np.roll(py, 1)             # il vertice precedente
        taglia = (py > y) != (qy > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = (qx - px) * (y - py) / (qy - py) + px
        return bool(np.count_nonzero(taglia & (x < xi)) % 2)

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


def cerca(chiave, domini, adesso):
    """
    Tutte le stazioni nell'area del dominio a 2 km. Ogni ricerca dà le 10 stazioni più vicine al punto: si sa quindi che
    dentro il cerchio che arriva alla decima non ne manca nessuna. Si parte da riquadri grandi, si interroga il centro e,
    se il cerchio non copre tutto il riquadro, lo si divide in 4 e si ripete (a ondate, in parallelo), saltando i riquadri
    già coperti da cerchi di altre ricerche. Ritorna {id: (lat, lon)} e un riassunto per il resoconto.
    """
    d3 = domini[3]
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
def controlla(stazioni, vicine=12):
    """
    stazioni: {sid: {"lat", "lon", "quota": m, "blocchi": {k: (istante, T, RH, P)}}}  (modificato sul posto)

    Per ogni blocco di 15 minuti ogni valore è confrontato con quello delle `vicine` stazioni più vicine che hanno il dato
    (temperatura riportata alla stessa quota con 6,5 K/km): si scarta se si discosta dalla loro mediana più di 3 volte lo
    scarto tipico (MAD) e più di una soglia minima che cresce con la distanza delle vicine. Una stazione isolata o senza
    vicine con dati si confronta con la mediana di tutta l'area, con una soglia larga. Si scartano anche i salti
    improvvisi nel tempo (un valore lontano dai due vicini nel tempo, che tra loro concordano) e la pressione fuori
    dalle vicine (di solito è la pressione non riportata al mare).
    Poi si eliminano le stazioni con più della metà di temperature e umidità scartate, quelle senza più temperature e
    quelle con il sensore bloccato (temperatura identica per 3 ore). Ritorna (stazioni eliminate, valori scartati).
    """
    ids = list(stazioni)
    n = len(ids)
    if n == 0:
        return 0, 0
    ks = sorted({k for st in stazioni.values() for k in st["blocchi"]})
    col = {k: j for j, k in enumerate(ks)}
    nb = len(ks)
    T, RH, P = (np.full((n, nb), np.nan) for _ in range(3))
    for i, sid in enumerate(ids):
        for k, (_ist, t, rh, pr) in stazioni[sid]["blocchi"].items():
            j = col[k]
            T[i, j] = np.nan if t is None else t
            RH[i, j] = np.nan if rh is None else rh
            P[i, j] = np.nan if pr is None else pr
    quota = np.array([stazioni[i]["quota"] for i in ids], dtype=float)
    x = np.array([(stazioni[i]["lon"] - LON0) * 111.19 * np.cos(np.radians(LAT0)) for i in ids])
    y = np.array([(stazioni[i]["lat"] - LAT0) * 111.19 for i in ids])
    valori_prima = (~np.isnan(T)).sum(1) + (~np.isnan(RH)).sum(1)
    T_iniziale = T.copy()

    # le `vicine` più vicine di ognuna (a pezzi per non riempire la memoria)
    k_v = min(vicine, n - 1)
    idx = np.zeros((n, max(k_v, 0)), dtype=int)
    dmedia = np.full(n, 99.0)
    if k_v > 0:
        for i0 in range(0, n, 400):
            d = np.hypot(x[i0:i0 + 400, None] - x[None, :], y[i0:i0 + 400, None] - y[None, :])
            d[np.arange(d.shape[0]), np.arange(i0, i0 + d.shape[0])] = np.inf
            o = np.argsort(d, axis=1)[:, :k_v]
            idx[i0:i0 + 400] = o
            dmedia[i0:i0 + 400] = np.take_along_axis(d, o, axis=1).mean(axis=1)

    def contro_le_vicine(V, soglia_min, soglia_larga, minimo=4):
        """Matrice booleana: True dove il valore sta troppo lontano dalle vicine (o, se ne ha meno di `minimo`, dall'area)."""
        mediana_area = np.nanmedian(V, axis=0)
        mad_area = 1.4826 * np.nanmedian(np.abs(V - mediana_area), axis=0)
        fuori = np.abs(V - mediana_area) > np.maximum(3 * mad_area, soglia_larga)
        if k_v > 0:
            NV = V[idx]                                         # (n, vicine, blocchi)
            quante = (~np.isnan(NV)).sum(axis=1)
            med = np.nanmedian(NV, axis=1)
            mad = 1.4826 * np.nanmedian(np.abs(NV - med[:, None, :]), axis=1)
            vicino = np.abs(V - med) > np.maximum(3 * mad, soglia_min[:, None])
            fuori = np.where(quante >= minimo, vicino, fuori)
        return fuori & ~np.isnan(V)

    import warnings
    scartati = np.zeros(n)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)         # blocchi dove nessuna vicina ha il dato
        # temperatura (riportata a livello del mare)
        red = T + 0.0065 * quota[:, None]
        fuori_t = contro_le_vicine(red, np.clip(3.0 + 0.15 * dmedia, 3.0, 8.0), 10.0)
        # salti nel tempo
        if nb >= 3:
            a_, b_, c_ = T[:, :-2], T[:, 1:-1], T[:, 2:]
            salto = (np.abs(b_ - (a_ + c_) / 2) > 4.0) & (np.abs(a_ - c_) < 2.5)
            fuori_t[:, 1:-1] |= salto & ~np.isnan(b_)
        T[fuori_t] = np.nan
        scartati += fuori_t.sum(1)
        # umidità
        fuori_u = contro_le_vicine(RH, np.clip(25.0 + 0.5 * dmedia, 25.0, 40.0), 45.0)
        RH[fuori_u] = np.nan
        scartati += fuori_u.sum(1)
        # pressione (non conta per eliminare la stazione)
        fuori_p = contro_le_vicine(P, np.clip(2.0 + 0.05 * dmedia, 2.0, 4.0), 8.0)
        P[fuori_p] = np.nan

    bloccate = np.array([(~np.isnan(T_iniziale[i])).sum() >= 12 and np.ptp(T_iniziale[i][~np.isnan(T_iniziale[i])]) == 0
                         for i in range(n)])
    da_eliminare = (scartati * 2 > valori_prima) | ((~np.isnan(T)).sum(1) == 0) | bloccate
    for i, sid in enumerate(ids):
        if da_eliminare[i]:
            del stazioni[sid]
            continue
        bl = stazioni[sid]["blocchi"]
        for k, j in col.items():
            if k in bl:
                ist = bl[k][0]
                bl[k] = (ist, None if np.isnan(T[i, j]) else float(T[i, j]), None if np.isnan(RH[i, j]) else float(RH[i, j]),
                         None if np.isnan(P[i, j]) else float(P[i, j]))
    return int(da_eliminare.sum()), int(fuori_t.sum() + fuori_u.sum() + fuori_p.sum())


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

    if a.prova:
        prova_ricerca(domini, adesso)
        stazioni, difetti = stazioni_finte(domini, t0, adesso)
        n_prima = len(stazioni)
    else:
        chiave = os.environ.get("WU_API_KEY", "")
        if not chiave:
            print("WU_API_KEY non impostata: niente stazioni, WRF girerà senza nudging.")
            return
        SEGRETI.append(chiave)
        if adesso - t0 > timedelta(hours=23):
            print("Il ciclo è più vecchio di 23 ore: lo storico a 24 ore di Wunderground non basta, niente nudging.")
            return
        inizio = time.monotonic()
        print("Ricerca delle stazioni...", flush=True)
        trovate, riass = cerca(chiave, domini, adesso)
        print(f"Stazioni trovate: {len(trovate)} con {riass['ricerche']} ricerche in {time.monotonic() - inizio:.0f} s"
              f" (mute da più di 2 giorni: {riass['mute']}; ricerche fallite: {riass['fallite']}; "
              f"riquadri troppo fitti per essere esauriti: {riass['non_esaurite']})", flush=True)
        ids = [sid for sid, (la, lo) in trovate.items() if dominio_fine(domini, la, lo)]
        for n in (3, 2, 1):
            print(f"  nel dominio {n}: {sum(1 for sid in ids if dominio_fine(domini, *trovate[sid]) == n)}", flush=True)
        inizio = time.monotonic()
        stazioni, motivi = leggi_tutte(chiave, ids, domini, t0, adesso)
        print("Scartate prima del controllo: " + ", ".join(f"{v} {k}" for k, v in motivi.items() if v) +
              f"; restano {len(stazioni)} (lettura in {time.monotonic() - inizio:.0f} s, "
              f"{_stato['chiamate']} chiamate in tutto)", flush=True)

    if not stazioni:
        print("Nessuna stazione utilizzabile: WRF girerà senza nudging.")
        return
    eliminate, valori = controlla(stazioni)
    print(f"Controllo di qualità: eliminate {eliminate} stazioni, scartati {valori} valori singoli; restano {len(stazioni)}")
    if a.prova:
        verifica_prova(stazioni, difetti, n_prima)

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
          f"({n3} nel dominio a 2 km); nudging fino a +{minuti // 60} h {minuti % 60:02d} min "
          f"({ultimo:%H:%M} UTC).")


def stazioni_finte(domini, t0, adesso, quante=600):
    """Stazioni inventate per collaudare controlli, scrittura e lettura (nessuna rete). Ritorna (stazioni, id dei difetti):
    alcune hanno guasti voluti, che il controllo deve eliminare del tutto o in parte."""
    rng = np.random.default_rng(1)
    d3, d2 = domini[3], domini[2]
    stazioni = {}
    n_blocchi = 9 * 60 // PASSO_MIN                    # nove ore di dati, comunque sia l'ora di adesso
    for i in range(quante):
        dom = d3 if i < quante - 40 else d2
        j, k = rng.integers(8, dom.lat.shape[0] - 8, 2)
        la, lo = float(dom.lat[j, k]), float(dom.lon[j, k])
        quota = float(dom.hgt[j, k]) + float(rng.normal(0, 20))
        blocchi = {}
        for b in range(n_blocchi):
            ist = t0 + timedelta(minutes=PASSO_MIN * b + 7)
            ora = ist.hour + ist.minute / 60
            T = 14.0 + 4.0 * np.sin(2 * np.pi * (ora - 9) / 24) - 0.0065 * quota + float(rng.normal(0, 0.4))
            rh = float(np.clip(75 - 3.0 * (T - 14) + rng.normal(0, 3), 20, 98))
            blocchi[b] = (ist, float(T), rh, float(1017 + rng.normal(0, 0.5)))
        stazioni[f"IPROVA{i:03d}"] = {"lat": la, "lon": lo, "quota": quota, "dominio": 3 if i < quante - 40 else 2,
                                      "blocchi": blocchi}

    def modifica(sid, f):
        st = stazioni[sid]
        st["blocchi"] = {b: f(b, *v) for b, v in st["blocchi"].items()}

    difetti = {"eliminare": [], "parziali": [], "pressione": []}
    for i in range(0, 5):                               # sempre 12 K troppo calde (sensore al sole)
        sid = f"IPROVA{100 + i:03d}"
        modifica(sid, lambda b, ist, t, rh, p: (ist, t + 12.0, rh, p))
        difetti["eliminare"].append(sid)
    for i in range(0, 3):                               # sensore bloccato
        sid = f"IPROVA{110 + i:03d}"
        modifica(sid, lambda b, ist, t, rh, p: (ist, 17.3, rh, p))
        difetti["eliminare"].append(sid)
    sid = "IPROVA120"                                   # umidità impossibile e temperatura di -40 per tutta la serie
    modifica(sid, lambda b, ist, t, rh, p: (ist, t - 15.0, 5.0, p))
    difetti["eliminare"].append(sid)
    for i in range(0, 4):                               # un solo valore sballato (salto) in mezzo alla serie
        sid = f"IPROVA{130 + i:03d}"
        modifica(sid, lambda b, ist, t, rh, p: (ist, t + 9.0 if b == 11 else t, rh, p))
        difetti["parziali"].append((sid, 11))
    for i in range(0, 3):                               # pressione non riportata al mare
        sid = f"IPROVA{140 + i:03d}"
        modifica(sid, lambda b, ist, t, rh, p: (ist, t, rh, p - 14.0))
        difetti["pressione"].append(sid)
    return stazioni, difetti


def verifica_prova(stazioni, difetti, n_prima):
    """I guasti inventati sono stati tolti e le stazioni buone sono rimaste?"""
    errori = []
    errori += [f"{s} doveva essere eliminata" for s in difetti["eliminare"] if s in stazioni]
    for sid, b in difetti["parziali"]:
        if sid not in stazioni:
            errori.append(f"{sid} aveva un solo valore sballato e non andava eliminata")
        elif stazioni[sid]["blocchi"][b][1] is not None:
            errori.append(f"{sid}: il valore sballato del blocco {b} è rimasto")
    for sid in difetti["pressione"]:
        if sid not in stazioni:
            errori.append(f"{sid} aveva solo la pressione sbagliata e non andava eliminata")
        elif any(v[3] is not None for v in stazioni[sid]["blocchi"].values()):
            errori.append(f"{sid}: la pressione sbagliata è rimasta")
    cattive = len(difetti["eliminare"])
    buone_rimaste = len(stazioni) - len(difetti["parziali"]) - len(difetti["pressione"])
    buone_prima = n_prima - cattive - len(difetti["parziali"]) - len(difetti["pressione"])
    if buone_rimaste < 0.97 * buone_prima:
        errori.append(f"troppe stazioni buone eliminate: restano {buone_rimaste} su {buone_prima}")
    if errori:
        sys.exit("!! Controllo di qualità sbagliato:\n  " + "\n  ".join(errori))
    print(f"Prova del controllo di qualità ok: {cattive} stazioni guaste eliminate, valori sballati tolti, "
          f"{buone_rimaste} stazioni buone su {buone_prima} rimaste")


class ApiFinta:
    """Fa le veci di `location/near` con stazioni inventate: le 10 più vicine al punto, con la distanza in km."""

    def __init__(self, domini, adesso, n_citta=1500, n_sparse=400, seme=2):
        rng = np.random.default_rng(seme)
        d3 = domini[3]
        pos = []
        while len(pos) < n_citta + n_sparse:
            if len(pos) < n_citta:                       # un grande centro abitato molto fitto, altri più radi
                la = 41.9 + rng.normal(0, 0.10) if rng.random() < 0.6 else 41.3 + rng.normal(0, 0.15)
                lo = 12.5 + rng.normal(0, 0.12) if rng.random() < 0.6 else 13.0 + rng.normal(0, 0.15)
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
