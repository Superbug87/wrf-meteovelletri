#!/usr/bin/env python3
"""
Stazioni ufficiali per il nudging di WRF, e la loro fusione con quelle di Weather Underground.

Lo usano zone/<zona>/wrf/stazioni_wrf.py (variabile d'ambiente NUDGING_FONTI):
  wu         solo Wunderground, com'era prima (questo modulo non entra in gioco);
  ufficiali  solo Regione Lazio e Aeronautica Militare;
  tutte      le ufficiali piu' le Wunderground, controllate contro le ufficiali e riassunte in "superosservazioni"
             (una per riquadro di qualche km), cosi' le centinaia di stazioni amatoriali non coprono le poche ufficiali.

Fonti (elenco e coordinate in strumenti/stazioni_verifica.json, lo stesso della verifica giornaliera):
  - Regione Lazio, rete del Centro Funzionale (portale pubblico, account AegisPubblico): temperatura ogni 15 minuti
    su circa 170 stazioni, umidita' su una trentina; arrivano con 20-30 minuti di ritardo;
  - Aeronautica Militare: messaggi SYNOP orari (o triorari) da OGIMET, temperatura e punto di rugiada al decimo di grado
    e pressione al livello del mare; se OGIMET non risponde, i METAR degli aeroporti (aviationweather.gov, gradi interi).

Il vento non si scrive (come per le Wunderground). La temperatura delle ufficiali e delle superosservazioni si riporta
alla quota del terreno di WRF con 6,5 K/km: WRF confronta ogni osservazione di superficie con il primo livello del
modello sopra il suo terreno, senza correggere la differenza di quota (share/wrf_fddaobs_in.F mette rko=1).

Variabili d'ambiente facoltative:
  OBS_CONTROLLO=1   tiene fuori dal nudging un quarto fisso delle stazioni della Regione (scelte con un codice di controllo dell'id),
                    per misurare l'effetto del nudging su stazioni che il modello non ha visto (solo per le prove);
  OBS_FINO_A        AAAAMMGGHHMM UTC: ignora le osservazioni dopo quest'ora (per rifare un ciclo passato come se
                    fosse girato a quell'ora).
"""
import json
import os
import time
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PORTALE = "https://temporeale.regione.lazio.it/datascapeA"
OGIMET = "https://www.ogimet.com/cgi-bin/getsynop"
METAR = "https://aviationweather.gov/api/data/metar"
CATALOGO = Path(__file__).with_name("stazioni_verifica.json")
PASSO_MIN = 15
MANCANTE = -888888.0
GRADIENTE = 0.0065                       # K/m
METAR_ICAO = {"16239": "LIRA", "16242": "LIRF", "16245": "LIRE", "16235": "LIRU", "16216": "LIRV", "16253": "LIRM"}
FONTE_REGIONE, FONTE_AM, FONTE_WU = "REGIONE LAZIO", "AERONAUTICA", "WUNDERGROUND"


def log(*a):
    print(*a, flush=True)


def _ricevi(metodo, url, tentativi=3, **kw):
    import requests

    kw.setdefault("timeout", 40)
    for i in range(tentativi):
        try:
            r = requests.request(metodo, url, **kw)
            if r.status_code == 200 or r.status_code in (400, 401, 403, 404):
                return r
        except requests.RequestException:
            pass
        time.sleep(3 * (i + 1))
    return None


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ur_da_rugiada(t, td):
    es = lambda x: 6.112 * np.exp(17.67 * x / (x + 243.5))
    return float(min(100.0, 100.0 * es(td) / es(t)))


# ---------------------------------------------------------------------------------------------
# Lettura
# ---------------------------------------------------------------------------------------------
def leggi_regione(elenco, da, a, thread=4):
    """{id: {istante: [T, UR]}} per le stazioni della Regione Lazio dell'elenco (dati 'plausibili' del portale)."""
    r = _ricevi("POST", f"{PORTALE}/connect/token", data={"username": "AegisPubblico", "password": "AegisPubblico",
                                                         "grant_type": "password", "client_id": "Aegis",
                                                         "client_instance": "wrf-meteovelletri-nudging"})
    if r is None or r.status_code != 200:
        log("!! portale della Regione Lazio non raggiungibile: niente stazioni regionali")
        return {}
    hdr = {"Authorization": "Bearer " + r.json()["access_token"]}
    lavori = [(s["id"], k, s["elementi"][k]) for s in elenco for k in ("T", "UR") if k in s.get("elementi", {})]
    q = [("from", _iso(da)), ("to", _iso(a)), ("part", "EpochTime"), ("part", "ValueWithInvalid"),
         ("type", "Plausible"), ("timing", "Original")]

    def uno(lavoro):
        sid, k, eid = lavoro
        rr = _ricevi("GET", f"{PORTALE}/v3/data-combo/{eid}", tentativi=2, params=q, headers=hdr)
        if rr is None or rr.status_code != 200:
            return sid, k, None
        try:
            return sid, k, [(datetime.fromtimestamp(t / 1000, timezone.utc), float(v))
                            for t, v in (rr.json().get("plausibleData") or []) if v is not None and abs(v) < 1e5]
        except (ValueError, TypeError):
            return sid, k, None

    out, falliti = {}, 0
    with ThreadPoolExecutor(thread) as pool:
        for sid, k, serie in pool.map(uno, lavori):
            if serie is None:
                falliti += 1
                continue
            d = out.setdefault(sid, {})
            for t, v in serie:
                d.setdefault(t, [None, None])[0 if k == "T" else 1] = v
    log(f"Regione Lazio: {len(out)} stazioni con dati ({len(lavori)} richieste, {falliti} fallite)")
    return out


def _t_synop(gr):
    if len(gr) == 5 and gr[1] in "01" and gr[2:].isdigit():
        return (-1 if gr[1] == "1" else 1) * int(gr[2:]) / 10.0
    return None


def leggi_synop(wmo_validi, da, a):
    """{wmo: {istante: (T, Td, pressione al livello del mare)}} dai SYNOP del blocco 162 (OGIMET)."""
    r = _ricevi("GET", OGIMET, params={"block": "162", "begin": da.strftime("%Y%m%d%H%M"), "end": a.strftime("%Y%m%d%H%M")},
                timeout=90)
    if r is None or r.status_code != 200:
        log("!! OGIMET non raggiungibile")
        return {}
    out = {}
    for riga in r.text.splitlines():
        p = riga.split(",")
        if len(p) < 7 or p[0] not in wmo_validi:
            continue
        try:
            ora = datetime(int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5]), tzinfo=timezone.utc)
        except ValueError:
            continue
        gruppi = p[6].replace("=", "").split()
        if len(gruppi) < 6 or gruppi[0] != "AAXX":
            continue
        sez1 = []
        for gr in gruppi[3:]:
            if gr == "333" or gr.startswith("555"):
                break
            sez1.append(gr)
        resto = sez1[2:]
        if resto and resto[0].startswith("00"):
            resto = resto[1:]
        t = td = slp = None
        for gr in resto:
            if gr[0] == "1" and t is None:
                t = _t_synop(gr)
            elif gr[0] == "2" and gr[1] in "01" and td is None:
                td = _t_synop(gr)
            elif gr[0] == "4" and gr[1:].isdigit() and gr[1] in "09" and slp is None:      # 4PPPP (non 4a3hhh)
                v = int(gr[1:]) / 10.0
                slp = v + 1000 if v < 100 else v
        if t is not None:
            out.setdefault(p[0], {})[ora] = (t, td, slp)
    return out


def leggi_metar(wmo_lista, da, a):
    """Riserva per i SYNOP: {wmo: {istante: (T, Td, None)}} dai METAR (gradi interi)."""
    icao = {METAR_ICAO[w]: w for w in wmo_lista if w in METAR_ICAO}
    if not icao:
        return {}
    ore = int((datetime.now(timezone.utc) - da).total_seconds() // 3600) + 2
    r = _ricevi("GET", METAR, params={"ids": ",".join(icao), "format": "json", "hours": min(ore, 48)})
    out = {}
    if r is None or r.status_code != 200:
        log("!! anche i METAR non sono raggiungibili")
        return out
    try:
        for m in r.json():
            w = icao.get(m.get("icaoId"))
            t, td = m.get("temp"), m.get("dewp")
            if w is None or t is None:
                continue
            ora = datetime.fromtimestamp(int(m["obsTime"]), timezone.utc)
            if da <= ora <= a:
                out.setdefault(w, {})[ora] = (float(t), None if td is None else float(td), None)
    except (ValueError, TypeError, KeyError):
        return {}
    return out


# ---------------------------------------------------------------------------------------------
# Stazioni ufficiali nel formato degli script delle zone
# ---------------------------------------------------------------------------------------------
def dominio_fine(domini, lat, lon):
    for n in (3, 2, 1):
        if domini[n].contiene(lat, lon):
            return n
    return 0


def fine_osservazioni(adesso):
    v = os.environ.get("OBS_FINO_A", "").strip()
    if v:
        return min(adesso, datetime.strptime(v, "%Y%m%d%H%M").replace(tzinfo=timezone.utc))
    return adesso


def di_controllo(sid):
    """Le stazioni della Regione tenute fuori dal nudging con OBS_CONTROLLO=1 (sempre le stesse)."""
    return sid.startswith("RL") and zlib.crc32(sid.encode()) % 4 == 0


def ufficiali(domini, t0, fine, tolleranza_quota, catalogo=CATALOGO):
    """
    {id: {"lat", "lon", "quota", "terreno", "dominio", "fonte", "blocchi": {k: (istante, T, RH, P)}}}
    Ogni osservazione resta al suo istante (la Regione misura ogni 15 minuti, i SYNOP ogni ora): blocco k = quarto d'ora
    dall'inizio del ciclo. Si scartano le stazioni fuori dai domini o con quota troppo diversa dal terreno del modello.
    """
    cat = json.load(open(catalogo))
    controllo = os.environ.get("OBS_CONTROLLO", "") == "1"
    da = t0 - timedelta(minutes=1)
    scelte, motivi = {}, {"fuori dai domini": 0, "quota incoerente": 0, "senza temperatura": 0, "di controllo": 0}
    for gruppo, fonte in (("regione", FONTE_REGIONE), ("aeronautica", FONTE_AM)):
        for s in cat.get(gruppo, []):
            if gruppo == "regione" and "T" not in s.get("elementi", {}):
                motivi["senza temperatura"] += 1
                continue
            if controllo and di_controllo(s["id"]):
                motivi["di controllo"] += 1
                continue
            n = dominio_fine(domini, s["lat"], s["lon"])
            if not n:
                motivi["fuori dai domini"] += 1
                continue
            terreno = domini[n].quota(s["lat"], s["lon"])
            if abs(float(s["quota"]) - terreno) > tolleranza_quota[n]:
                motivi["quota incoerente"] += 1
                continue
            scelte[s["id"]] = dict(s, dominio=n, terreno=terreno, fonte=fonte)
    log(f"Stazioni ufficiali nei domini: {len(scelte)} (scartate: " + ", ".join(f"{v} {k}" for k, v in motivi.items() if v) + ")")

    reg = leggi_regione([s for s in scelte.values() if s["fonte"] == FONTE_REGIONE], da, fine)
    wmo = {s["wmo"]: sid for sid, s in scelte.items() if s["fonte"] == FONTE_AM}
    syn = leggi_synop(set(wmo), da, fine)
    mancanti = [w for w in wmo if w not in syn]
    if mancanti:
        met = leggi_metar(mancanti, da, fine)
        if met:
            log(f"METAR al posto dei SYNOP per {len(met)} stazioni: {', '.join(METAR_ICAO[w] for w in met)}")
        syn.update(met)
    log(f"Aeronautica: {len(syn)} stazioni con dati su {len(wmo)}")

    out = {}
    for sid, s in scelte.items():
        oss = []
        if s["fonte"] == FONTE_REGIONE:
            for t, (T, ur) in reg.get(sid, {}).items():
                oss.append((t, T, ur, None))
        else:
            for t, (T, td, slp) in syn.get(s["wmo"], {}).items():
                ur = ur_da_rugiada(T, td) if td is not None and td <= T + 0.5 else None
                oss.append((t, T, ur, slp))
        blocchi = {}
        for t, T, ur, p in sorted(oss):
            if not (t0 <= t <= fine):
                continue
            T = T if T is not None and -30.0 <= T <= 50.0 else None
            ur = ur if ur is not None and 3.0 <= ur <= 100.0 else None
            p = p if p is not None and 950.0 <= p <= 1060.0 else None
            if T is None:
                continue
            blocchi[int((t - t0).total_seconds() // (PASSO_MIN * 60))] = (t, T, ur, p)
        if blocchi:
            out[sid] = {"lat": float(s["lat"]), "lon": float(s["lon"]), "quota": float(s["quota"]), "terreno": s["terreno"],
                        "dominio": s["dominio"], "fonte": s["fonte"], "nome": s.get("nome", sid), "blocchi": blocchi}
    return out


# ---------------------------------------------------------------------------------------------
# Controllo di qualita' (stesso metodo di zone/lazio/wrf/stazioni_wrf.py, con stazioni "fisse")
# ---------------------------------------------------------------------------------------------
def controlla(stazioni, lat0, lon0, fisse=(), vicine=12, soglie_t=(3.0, 0.15, 8.0, 10.0), soglie_u=(25.0, 0.5, 40.0, 45.0)):
    """
    stazioni: {sid: {"lat", "lon", "quota", "blocchi": {k: (istante, T, RH, P)}}}  (modificato sul posto)

    Ogni valore e' confrontato con le `vicine` stazioni piu' vicine che hanno il dato nello stesso quarto d'ora
    (temperatura riportata al livello del mare con 6,5 K/km): si scarta se si discosta dalla loro mediana piu' di 3 volte
    lo scarto tipico e piu' di una soglia minima che cresce con la distanza delle vicine (soglie_t: minimo, crescita per
    km, massimo, soglia larga contro tutta l'area). Si scartano anche i salti improvvisi nel tempo e i sensori bloccati,
    poi le stazioni con piu' della meta' dei valori scartati.
    Le stazioni in `fisse` fanno da riferimento per le altre ma non vengono mai toccate (si controllano a parte).
    Ritorna (stazioni eliminate, valori scartati).
    """
    ids = list(stazioni)
    n = len(ids)
    if n == 0:
        return 0, 0
    fissa = np.array([i in fisse for i in ids])
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
    x = np.array([(stazioni[i]["lon"] - lon0) * 111.19 * np.cos(np.radians(lat0)) for i in ids])
    y = np.array([(stazioni[i]["lat"] - lat0) * 111.19 for i in ids])
    valori_prima = (~np.isnan(T)).sum(1) + (~np.isnan(RH)).sum(1)
    T_iniziale = T.copy()

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
        mediana_area = np.nanmedian(V, axis=0)
        mad_area = 1.4826 * np.nanmedian(np.abs(V - mediana_area), axis=0)
        fuori = np.abs(V - mediana_area) > np.maximum(3 * mad_area, soglia_larga)
        if k_v > 0:
            NV = V[idx]
            quante = (~np.isnan(NV)).sum(axis=1)
            med = np.nanmedian(NV, axis=1)
            mad = 1.4826 * np.nanmedian(np.abs(NV - med[:, None, :]), axis=1)
            vicino = np.abs(V - med) > np.maximum(3 * mad, soglia_min[:, None])
            fuori = np.where(quante >= minimo, vicino, fuori)
        return fuori & ~np.isnan(V) & ~fissa[:, None]

    scartati = np.zeros(n)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        red = T + GRADIENTE * quota[:, None]
        a, b, c, larga = soglie_t
        fuori_t = contro_le_vicine(red, np.clip(a + b * dmedia, a, c), larga)
        if nb >= 3:
            a_, b_, c_ = T[:, :-2], T[:, 1:-1], T[:, 2:]
            salto = (np.abs(b_ - (a_ + c_) / 2) > 4.0) & (np.abs(a_ - c_) < 2.5)
            fuori_t[:, 1:-1] |= salto & ~np.isnan(b_) & ~fissa[:, None]
        T[fuori_t] = np.nan
        scartati += fuori_t.sum(1)
        a, b, c, larga = soglie_u
        fuori_u = contro_le_vicine(RH, np.clip(a + b * dmedia, a, c), larga)
        RH[fuori_u] = np.nan
        scartati += fuori_u.sum(1)
        fuori_p = contro_le_vicine(P, np.clip(2.0 + 0.05 * dmedia, 2.0, 4.0), 8.0)
        P[fuori_p] = np.nan

    bloccate = np.array([(~np.isnan(T_iniziale[i])).sum() >= 12 and np.ptp(T_iniziale[i][~np.isnan(T_iniziale[i])]) == 0
                         for i in range(n)]) & ~fissa
    da_eliminare = ((scartati * 2 > valori_prima) | ((~np.isnan(T)).sum(1) == 0) | bloccate) & ~fissa
    for i, sid in enumerate(ids):
        if da_eliminare[i]:
            del stazioni[sid]
            continue
        bl = stazioni[sid]["blocchi"]
        for k, j in col.items():
            if k in bl:
                bl[k] = (bl[k][0], None if np.isnan(T[i, j]) else float(T[i, j]),
                         None if np.isnan(RH[i, j]) else float(RH[i, j]), None if np.isnan(P[i, j]) else float(P[i, j]))
    return int(da_eliminare.sum()), int(fuori_t.sum() + fuori_u.sum() + fuori_p.sum())


def controlla_ufficiali(uff, lat0, lon0):
    """Le ufficiali tra loro, con soglie larghe: devono cadere solo i guasti veri, non le inversioni notturne
    (una stazione in collina puo' essere 6-8 gradi piu' calda di una in pianura, riportata alla stessa quota)."""
    return controlla(uff, lat0, lon0, vicine=8, soglie_t=(8.0, 0.1, 12.0, 15.0), soglie_u=(35.0, 0.5, 50.0, 60.0))


# ---------------------------------------------------------------------------------------------
# Fusione con Wunderground
# ---------------------------------------------------------------------------------------------
def superosservazioni(wu, domini, uff, celle_km, lat0, lon0):
    """
    Le stazioni WU (gia' controllate) riassunte in una per riquadro: mediana dei valori, ognuno riportato alla quota
    mediana delle stazioni del riquadro. I riquadri che contengono una stazione ufficiale si saltano.
    celle_km: lato del riquadro per dominio, {3: km, 2: km, 1: km}.
    """
    kx = 111.19 * np.cos(np.radians(lat0))

    def cella(n, la, lo):
        return n, int(np.floor((lo - lon0) * kx / celle_km[n])), int(np.floor((la - lat0) * 111.19 / celle_km[n]))

    occupate = set()
    for s in uff.values():
        for n in (3, 2, 1):
            if domini[n].contiene(s["lat"], s["lon"]):
                occupate.add(cella(n, s["lat"], s["lon"]))
    gruppi = {}
    for sid, s in wu.items():
        n = s.get("dominio") or dominio_fine(domini, s["lat"], s["lon"])
        if n:
            gruppi.setdefault(cella(n, s["lat"], s["lon"]), []).append(s)
    out, saltate = {}, 0
    for (n, ix, iy), membri in gruppi.items():
        if (n, ix, iy) in occupate:
            saltate += len(membri)
            continue
        qc = float(np.median([m["quota"] for m in membri]))
        la, lo = float(np.mean([m["lat"] for m in membri])), float(np.mean([m["lon"] for m in membri]))
        blocchi = {}
        for k in sorted({k for m in membri for k in m["blocchi"]}):
            vals = [m["blocchi"][k] + (m["quota"],) for m in membri if k in m["blocchi"]]
            ts = [v[1] + GRADIENTE * (v[4] - qc) for v in vals if v[1] is not None]
            if not ts:
                continue
            rh = [v[2] for v in vals if v[2] is not None]
            pr = [v[3] for v in vals if v[3] is not None]
            ist = datetime.fromtimestamp(round(float(np.median([v[0].timestamp() for v in vals])) / 60) * 60, tz=timezone.utc)
            blocchi[k] = (ist, float(np.median(ts)), float(np.median(rh)) if rh else None, float(np.median(pr)) if pr else None)
        if blocchi:
            out[f"WU{n}_{ix}_{iy}"] = {"lat": la, "lon": lo, "quota": qc, "terreno": domini[n].quota(la, lo), "dominio": n,
                                      "fonte": FONTE_WU, "nome": f"{len(membri)} " + ("stazione WU" if len(membri) == 1 else "stazioni WU"), "membri": len(membri),
                                      "blocchi": blocchi}
    return out, saltate


# ---------------------------------------------------------------------------------------------
# Scrittura nel formato di WRF (share/wrf_fddaobs_in.F, subroutine in4dob)
# ---------------------------------------------------------------------------------------------
def _f(x):
    return f"{x:11.3f}"


def record(istante, lat, lon, sid, nome, fonte, quota, T, rh, p):
    """Cinque righe per un'osservazione di superficie (FM-12 SYNOP). T in °C, rh in %, p al livello del mare in hPa."""
    if T is None:
        return ""
    slp, q_s = (_f(p * 100.0), _f(0.0)) if p is not None else (_f(MANCANTE), _f(MANCANTE))
    h, q_h = (_f(min(rh, 99.0)), _f(0.0)) if rh is not None else (_f(MANCANTE), _f(MANCANTE))
    mis = _f(MANCANTE)
    riga1 = f" {istante:%Y%m%d%H%M%S}"
    riga2 = f"  {lat:9.4f} {lon:9.4f} "
    riga3 = "  " + sid[:40].ljust(40) + "   " + nome[:40].ljust(40) + "   "
    riga4 = ("  " + "FM-12 SYNOP".ljust(16) + "  " + fonte[:16].ljust(16) + "  " + f"{quota:8.1f}" + "  "
             + "   F" + "  " + "   F" + "  " + f"{1:5d}")
    campi = [slp, q_s, mis, mis, mis, mis, _f(T + 273.15), _f(0.0), mis, mis, mis, mis, h, q_h, mis, mis, mis, mis]
    riga5 = " " + "".join(c + " " for c in campi)
    return "\n".join((riga1, riga2, riga3, riga4, riga5)) + "\n"


def scrivi(stazioni, domini, t0, out, risoluzioni, tolleranza_quota):
    """
    OBS_DOMAIN101/201/301 (in ordine di tempo, come vuole WRF), obs_fdda_end.txt (minuti dall'inizio del ciclo fino
    all'ultima osservazione) e obs_fonti.txt (quanti punti per fonte nel dominio fine). In ogni dominio la temperatura e'
    riportata alla quota del terreno di quel dominio nel punto (6,5 K/km) e la quota scritta e' quella del terreno;
    se la differenza supera la tolleranza del dominio, li' il punto non si usa.
    Ritorna il numero di osservazioni scritte, o 0 (e nessun file) se non c'e' nulla di utile.
    """
    out = Path(out)
    righe = []
    for sid, s in stazioni.items():
        for n in (1, 2, 3):
            if not domini[n].contiene(s["lat"], s["lon"]):
                continue
            terreno = domini[n].quota(s["lat"], s["lon"])
            dz = s["quota"] - terreno
            if abs(dz) > tolleranza_quota[n]:
                continue
            for k, (ist, T, rh, p) in s["blocchi"].items():
                if T is not None:
                    righe.append((ist, sid, n, s, terreno, T + GRADIENTE * dz, rh, p))
    righe.sort(key=lambda r: (r[0], r[1], r[2]))
    testi = {1: [], 2: [], 3: []}
    ultimo = None
    for ist, sid, n, s, terreno, T, rh, p in righe:
        testi[n].append(record(ist, s["lat"], s["lon"], sid, s.get("nome", sid), s["fonte"], terreno, T, rh, p))
        ultimo = ist if ultimo is None or ist > ultimo else ultimo
    if ultimo is None or not testi[3] and not testi[2]:
        return 0
    for n in (1, 2, 3):
        (out / f"OBS_DOMAIN{n}01").write_text("".join(testi[n]))
    minuti = int((ultimo - t0).total_seconds() // 60)
    (out / "obs_fdda_end.txt").write_text(f"{minuti}\n")
    conta = {}
    nel_fine = {r[1] for r in righe if r[2] == 3}
    for sid in nel_fine:
        s = stazioni[sid]
        chiave = {FONTE_REGIONE: "regione", FONTE_AM: "aeronautica", FONTE_WU: "wu"}[s["fonte"]]
        conta[chiave] = conta.get(chiave, 0) + s.get("membri", 1)
    (out / "obs_fonti.txt").write_text(" ".join(f"{k}={v}" for k, v in sorted(conta.items())) + "\n")
    log(f"Scritte {len(righe)} osservazioni (d01 {len(testi[1])}, d02 {len(testi[2])}, d03 {len(testi[3])}) di "
        f"{len(stazioni)} punti, {len(nel_fine)} nel dominio a {risoluzioni[3]}; nudging fino a +{minuti // 60} h "
        f"{minuti % 60:02d} min ({ultimo:%H:%M} UTC). Stazioni nel dominio fine: "
        + ", ".join(f"{k} {v}" for k, v in sorted(conta.items())))
    return len(righe)


# ---------------------------------------------------------------------------------------------
# Il percorso completo, chiamato dagli script delle zone
# ---------------------------------------------------------------------------------------------
def prepara(modo, wu, domini, t0, adesso, out, tolleranza_quota, celle_km, lat0, lon0, risoluzioni):
    """
    modo 'ufficiali' o 'tutte'; wu: stazioni Wunderground lette e gia' controllate dallo script della zona (o {}).
    Scrive i file per WRF; ritorna il numero di osservazioni scritte (0 = WRF gira senza nudging).
    """
    fine = fine_osservazioni(adesso)
    inizio = time.monotonic()
    uff = ufficiali(domini, t0, fine, tolleranza_quota)
    el, sc = controlla_ufficiali(uff, lat0, lon0)
    log(f"Ufficiali: {len(uff)} con dati dopo il controllo (eliminate {el}, valori scartati {sc}) in "
        f"{time.monotonic() - inizio:.0f} s")
    stazioni = dict(uff)
    if modo == "tutte" and wu:
        for v in wu.values():                       # stesso limite di tempo delle ufficiali
            v["blocchi"] = {k: b for k, b in v["blocchi"].items() if b[0] <= fine}
        tutte = {**{f"W:{k}": v for k, v in wu.items()}, **uff}
        el, sc = controlla(tutte, lat0, lon0, fisse=set(uff))
        wu_ok = {k[2:]: v for k, v in tutte.items() if k.startswith("W:")}
        log(f"Wunderground contro le ufficiali: eliminate {el} stazioni, scartati {sc} valori; restano {len(wu_ok)}")
        so, saltate = superosservazioni(wu_ok, domini, uff, celle_km, lat0, lon0)
        log(f"Superosservazioni Wunderground: {len(so)} (riquadri di {celle_km[3]} km nel dominio fine); "
            f"{saltate} stazioni WU non usate perche' nel riquadro c'e' una stazione ufficiale")
        stazioni.update(so)
    if fine < adesso:
        log(f"Osservazioni fino alle {fine:%H:%M} UTC (OBS_FINO_A)")
    if not stazioni:
        log("Nessuna stazione utilizzabile: WRF girera' senza nudging.")
        return 0
    n = scrivi(stazioni, domini, t0, out, risoluzioni, tolleranza_quota)
    if not n:
        log("Nessuna osservazione utile: WRF girera' senza nudging.")
    return n
