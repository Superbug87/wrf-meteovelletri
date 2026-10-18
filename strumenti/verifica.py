#!/usr/bin/env python3
"""Verifica giornaliera delle previsioni WRF contro le osservazioni, e pagella.

  verifica.py --giorno AAAA-MM-GG [--estratti CARTELLA] --archivio CARTELLA_DEL_RAMO_VERIFICA

Per il giorno indicato (00-24 UTC; di default ieri):
  1. prende gli estratti dei run che lo coprono (rami risultati-<zona>-<modello>, cartella estratti/, oppure --estratti);
  2. scarica le osservazioni: rete del Centro Funzionale della Regione Lazio (temperatura, pioggia, vento, umidita'),
     SYNOP dell'Aeronautica Militare via OGIMET (temperatura, rugiada, vento, pressione) e radar della Protezione Civile
     (accumuli orari SRT1); come riferimento, le previsioni a breve di ECMWF e ICON-2I originali (Open-Meteo, nei punti
     delle stazioni);
  3. calcola, per ogni zona e modello e per scadenza (primo e secondo giorno di previsione), le somme che servono agli
     indici (errori di temperatura, vento, pressione; tabelle di contingenza della pioggia ai pluviometri; confronto
     con il radar con 10 km di tolleranza) e le salva in giorni/AAAA-MM-GG.json;
  4. rifà pagella.json con gli ultimi 7 e 30 giorni.
Le somme (non le medie) permettono di rifare gli indici su qualunque periodo senza errori.
"""
import argparse
import glob
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

QUI = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("GITHUB_REPOSITORY", "Superbug87/wrf-meteovelletri")
ZONE = ["castelli", "lazio"]
PILOTI = ["ecmwf", "icon2i"]
PORTALE = "https://temporeale.regione.lazio.it/datascapeA"
SOGLIE_PIOGGIA = (0.2, 1.0, 5.0)         # mm in 6 ore, ai pluviometri
SOGLIE_RADAR = (0.5, 2.0)                # mm in 6 ore, sul radar
TOLLERANZA_KM = 10.0
NOTTE = set(range(18, 24)) | set(range(0, 6))   # ore UTC
# correzione del vento a 10 m (prova): classi del vento previsto, in m/s; per ogni classe il rapporto osservato/previsto
# dei 30 giorni prima (minimo MIN_CORREZIONE confronti), calcolato senza il giorno che si verifica
CLASSI_VENTO = (0.0, 1.0, 2.0, 3.0, 4.0, 6.0, 9.0, 1e9)
MIN_CORREZIONE = 30
GIORNI_CORREZIONE = 30
SESSIONE = requests.Session()
SESSIONE.headers["User-Agent"] = "wrf-meteovelletri-verifica (GitHub Actions)"


def log(*a):
    print(*a, flush=True)


def ricevi(metodo, url, tentativi=4, **kw):
    for i in range(tentativi):
        try:
            r = SESSIONE.request(metodo, url, timeout=60, **kw)
            if r.status_code < 500:
                return r
        except requests.RequestException as e:
            log("  rete:", url[:80], e)
        time.sleep(5 * (i + 1))
    return None


# ---------------------------------------------------------------------------------------------------------------
# Estratti dei run WRF
# ---------------------------------------------------------------------------------------------------------------
def cicli_utili(giorno):
    """I cicli (00 e 12 UTC) che coprono almeno un'ora del giorno con previsioni fino a 48 ore."""
    g0 = datetime.combine(giorno, datetime.min.time())
    return [(g0 - timedelta(hours=h)).strftime("%Y%m%d%H") for h in range(-12, 49, 12)]


def carica_estratti(giorno, cartella):
    """{(zona, pilota): [estratti]}; un estratto e' un dict di array (vedi estrai_verifica.py)."""
    out = {}
    cicli = set(cicli_utili(giorno))
    token = os.environ.get("GITHUB_TOKEN")
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    for z in ZONE:
        for p in PILOTI:
            nomi = []
            if cartella:
                nomi = [f for f in glob.glob(os.path.join(cartella, f"*{z}-{p}_*.npz")) if f[-14:-4] in cicli]
            else:
                r = ricevi("GET", f"https://api.github.com/repos/{REPO}/contents/estratti?ref=risultati-{z}-{p}", headers=hdr)
                if r is not None and r.status_code == 200:
                    for v in r.json():
                        if v["name"].endswith(".npz") and v["name"][-14:-4] in cicli:
                            nomi.append(v["download_url"])
            lista = []
            for n in sorted(nomi):
                try:
                    if n.startswith("http"):
                        r = ricevi("GET", n, headers=hdr)
                        dati = np.load(io.BytesIO(r.content))
                    else:
                        dati = np.load(n)
                    lista.append({k: dati[k] for k in dati.files})
                except Exception as e:               # un estratto rovinato non ferma la verifica
                    log("  estratto non leggibile:", n, e)
            out[(z, p)] = lista
            log(f"{z}-{p}: estratti {[str(e['ciclo']) for e in lista]}")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Osservazioni
# ---------------------------------------------------------------------------------------------------------------
def osservazioni_regione(stazioni, giorno, servono):
    """{id: {'t2': {ora: v}, 'ur': ..., 'vento': ..., 'pioggia': {ora_fine: mm}}}; ore come datetime UTC."""
    r = ricevi("POST", f"{PORTALE}/connect/token", data={"username": "AegisPubblico", "password": "AegisPubblico",
                                                        "grant_type": "password", "client_id": "Aegis",
                                                        "client_instance": "wrf-meteovelletri-verifica"})
    if r is None or r.status_code != 200:
        log("!! portale della Regione non raggiungibile: niente osservazioni regionali")
        return {}
    hdr = {"Authorization": "Bearer " + r.json()["access_token"]}
    g0 = datetime.combine(giorno, datetime.min.time(), tzinfo=timezone.utc)
    da, a = (g0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), (g0 + timedelta(hours=24, minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, richieste = {}, 0
    for s in stazioni:
        if s["id"] not in servono:
            continue
        for chiave, eid in s["elementi"].items():
            if chiave not in ("T", "pioggia", "vento", "UR"):
                continue
            q = [("from", da), ("to", a), ("part", "EpochTime"), ("part", "ValueWithInvalid"), ("type", "Plausible"), ("timing", "Original")]
            rr = ricevi("GET", f"{PORTALE}/v3/data-combo/{eid}", params=q, headers=hdr)
            richieste += 1
            time.sleep(0.25)                        # gentilezza verso il portale
            if rr is None or rr.status_code != 200:
                continue
            serie = [(datetime.fromtimestamp(t / 1000, timezone.utc), v) for t, v in (rr.json().get("plausibleData") or [])
                     if v is not None and abs(v) < 1e5]
            d = out.setdefault(s["id"], {})
            if chiave == "pioggia":                 # cumulata: si sommano gli aumenti, ora per ora (ora di fine)
                ore = {}
                for (t0, v0), (t1, v1) in zip(serie, serie[1:]):
                    if v1 > v0 and v1 - v0 < 60:
                        fine = (t1 - timedelta(seconds=1)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                        ore[fine] = ore.get(fine, 0.0) + (v1 - v0)
                # le ore senza aumenti ma coperte dai dati valgono 0
                coperte = {(t - timedelta(seconds=1)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1) for t, _ in serie}
                d["pioggia"] = {h: ore.get(h, 0.0) for h in coperte}
            else:
                nome = {"T": "t2", "vento": "vento", "UR": "ur"}[chiave]
                vals = {}
                for t, v in serie:                  # valore piu' vicino all'ora esatta (entro 10 minuti)
                    h = (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
                    scarto = abs((t - h).total_seconds())
                    if scarto <= 600 and (h not in vals or scarto < vals[h][1]):
                        vals[h] = (v, scarto)
                d[nome] = {h: v for h, (v, _) in vals.items()}
    log(f"Regione Lazio: {len(out)} stazioni lette con {richieste} richieste")
    return out


def _syn_t(gr):
    if len(gr) == 5 and gr[1] in "01" and gr[2:].isdigit():
        return (-1 if gr[1] == "1" else 1) * int(gr[2:]) / 10.0
    return None


def osservazioni_synop(stazioni, giorno):
    """SYNOP delle stazioni dell'Aeronautica (OGIMET): {id: {'t2','td2','vento','mslp': {ora: v}}}."""
    g0 = datetime.combine(giorno, datetime.min.time(), tzinfo=timezone.utc)
    r = ricevi("GET", "https://www.ogimet.com/cgi-bin/getsynop",
               params={"block": "162", "begin": g0.strftime("%Y%m%d0000"), "end": g0.strftime("%Y%m%d2300")})
    if r is None or r.status_code != 200:
        log("!! OGIMET non raggiungibile: niente SYNOP")
        return {}
    wmo = {s["wmo"]: s["id"] for s in stazioni}
    out = {}
    for riga in r.text.splitlines():
        p = riga.split(",")
        if len(p) < 7 or p[0] not in wmo:
            continue
        try:
            ora = datetime(int(p[1]), int(p[2]), int(p[3]), int(p[4]), tzinfo=timezone.utc)
        except ValueError:
            continue
        gruppi = p[6].replace("=", "").split()
        if len(gruppi) < 5 or gruppi[0] != "AAXX":
            continue
        iw = gruppi[1][-1]                          # 3/4: vento in nodi, 0/1: in m/s
        sez1 = []
        for gr in gruppi[3:]:
            if gr == "333" or gr.startswith("555"):
                break
            sez1.append(gr)
        if len(sez1) < 2:
            continue
        d = out.setdefault(wmo[p[0]], {"t2": {}, "td2": {}, "vento": {}, "mslp": {}})
        nddff = sez1[1]
        if nddff[3:].isdigit() and nddff[3:] != "99":
            ff = int(nddff[3:])
            d["vento"][ora] = ff * 0.5144 if iw in "34" else float(ff)
        resto = sez1[2:]
        if resto and resto[0].startswith("00"):
            resto = resto[1:]
        for gr in resto:
            if gr[0] == "1" and _syn_t(gr) is not None and ora not in d["t2"]:
                d["t2"][ora] = _syn_t(gr)
            elif gr[0] == "2" and gr[1] in "01":
                d["td2"][ora] = _syn_t(gr)
            elif gr[0] == "4" and gr[1:].isdigit():
                v = int(gr[1:]) / 10.0
                d["mslp"][ora] = v + 1000 if v < 100 else v
    log(f"SYNOP: {len(out)} stazioni dell'Aeronautica")
    return out


def radar_giorno(giorno):
    """{ora_fine: mosaico SRT1 (mm nell'ora)} per le 24 ore del giorno."""
    sys.path.insert(0, os.path.join(QUI, "..", "zone", "castelli", "wrf"))   # radar.py (uguale nelle due zone)
    sys.path.insert(0, os.path.join(QUI, "..", "wrf"))      # ... o quella copiata nella radice dal workflow
    try:
        import radar
    except ImportError:
        log("!! radar.py non trovato: niente confronto col radar")
        return {}, None
    g0 = datetime.combine(giorno, datetime.min.time(), tzinfo=timezone.utc)
    out = {}
    for h in range(1, 25):
        t = g0 + timedelta(hours=h)
        try:
            a = radar.scarica("SRT1", int(t.timestamp() * 1000))
        except Exception as e:
            log("  radar", t, e)
            a = None
        if a is not None:
            out[t] = a
    log(f"radar: {len(out)} accumuli orari su 24")
    return out, radar


def riferimento_openmeteo(punti, giorno):
    """Previsioni a breve di ECMWF e ICON-2I originali nei punti: {modello: {id: {'t2','vento','pioggia': {ora: v}}}}."""
    out = {"ecmwf": {}, "icon2i": {}}
    modelli = {"ecmwf": "ecmwf_ifs", "icon2i": "italia_meteo_arpae_icon_2i"}
    g = giorno.strftime("%Y-%m-%d")
    ids = list(punti)
    for i in range(0, len(ids), 50):
        pezzo = ids[i:i + 50]
        params = {"latitude": ",".join(f"{punti[k][0]:.4f}" for k in pezzo), "longitude": ",".join(f"{punti[k][1]:.4f}" for k in pezzo),
                  "elevation": ",".join(f"{punti[k][2]:.0f}" for k in pezzo), "hourly": "temperature_2m,precipitation,wind_speed_10m",
                  "models": ",".join(modelli.values()), "wind_speed_unit": "ms", "timezone": "GMT", "start_date": g, "end_date": g}
        r = ricevi("GET", "https://historical-forecast-api.open-meteo.com/v1/forecast", params=params)
        if r is None or r.status_code != 200:
            log("!! Open-Meteo non raggiungibile:", None if r is None else r.text[:200])
            continue
        risposte = r.json() if isinstance(r.json(), list) else [r.json()]
        for k, ris in zip(pezzo, risposte):
            h = ris.get("hourly", {})
            tempi = [datetime.strptime(t, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc) for t in h.get("time", [])]
            for m, nome in modelli.items():
                d = out[m].setdefault(k, {"t2": {}, "vento": {}, "pioggia": {}})
                for var, chiave in (("temperature_2m", "t2"), ("wind_speed_10m", "vento"), ("precipitation", "pioggia")):
                    vals = h.get(f"{var}_{nome}") or []
                    for t, v in zip(tempi, vals):
                        if v is not None:
                            # Open-Meteo da' la pioggia dell'ora che finisce all'istante indicato
                            d[chiave][t] = v
        time.sleep(1)
    log(f"Open-Meteo: ECMWF {len(out['ecmwf'])} punti, ICON-2I {len(out['icon2i'])} punti")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Indici
# ---------------------------------------------------------------------------------------------------------------
def vuoto():
    return {"t2": [0, 0.0, 0.0], "t2_giorno": [0, 0.0, 0.0], "t2_notte": [0, 0.0, 0.0], "td2": [0, 0.0, 0.0],
            "vento": [0, 0.0, 0.0], "vento_corr": [0, 0.0, 0.0], "mslp": [0, 0.0, 0.0],
            "vento_classi": {str(i): [0, 0.0, 0.0] for i in range(len(CLASSI_VENTO) - 1)},
            "pioggia": {str(s): [0, 0, 0, 0] for s in SOGLIE_PIOGGIA},
            "radar": {str(s): [0, 0, 0, 0] for s in SOGLIE_RADAR}, "cicli": []}


def aggiungi(acc, chiave, err):
    acc[chiave][0] += 1
    acc[chiave][1] += float(err)
    acc[chiave][2] += float(err) ** 2


def classe_vento(v):
    for i in range(len(CLASSI_VENTO) - 1):
        if v < CLASSI_VENTO[i + 1]:
            return str(i)
    return str(len(CLASSI_VENTO) - 2)


def correzioni_vento(archivio, giorno, fino_a_incluso=False):
    """{'<zona>-<pilota>': {classe: fattore}} dai giorni/ dei GIORNI_CORREZIONE giorni prima di `giorno`
    (con fino_a_incluso anche `giorno`). Classi con pochi confronti: fattore 1 (nessuna correzione)."""
    tot = {}
    for f in glob.glob(os.path.join(archivio, "giorni", "*.json")):
        g = os.path.basename(f)[:-5]
        inizio = (giorno - timedelta(days=GIORNI_CORREZIONE - (1 if fino_a_incluso else 0))).isoformat()
        fine = giorno.isoformat()
        if not (inizio <= g < fine or (fino_a_incluso and g == fine)):
            continue
        for chiave, per_scad in json.load(open(f))["modelli"].items():
            if "-rif-" in chiave:
                continue
            for acc in per_scad.values():
                for c, (n, sm, so) in acc.get("vento_classi", {}).items():
                    t = tot.setdefault(chiave, {}).setdefault(c, [0, 0.0, 0.0])
                    t[0] += n; t[1] += sm; t[2] += so
    return {k: {c: (round(so / sm, 3) if n >= MIN_CORREZIONE and sm > 0 else 1.0) for c, (n, sm, so) in v.items()}
            for k, v in tot.items()}


def scadenza(ore):
    return "g1" if ore <= 24 else "g2"


def finestre(giorno):
    g0 = datetime.combine(giorno, datetime.min.time(), tzinfo=timezone.utc)
    return [g0 + timedelta(hours=h) for h in (6, 12, 18, 24)]


def conta_pioggia(acc, prev, oss):
    for s in SOGLIE_PIOGGIA:
        f, o = prev >= s, oss >= s
        c = acc["pioggia"][str(s)]
        c[0] += int(f and o); c[1] += int(f and not o); c[2] += int(o and not f); c[3] += int(not f and not o)


def verifica_estratto(e, oss, quote, radar_ore, radar_mod, acc_per_scad, corr_vento=None):
    ciclo = datetime.strptime(str(e["ciclo"]), "%Y%m%d%H").replace(tzinfo=timezone.utc)
    # le ore fino alla fine del nudging erano gia' passate quando il modello ha smesso di vedere le stazioni:
    # non sono previsione (e con le stazioni ufficiali il modello le ha viste proprio qui). Si contano solo le successive.
    fine_nudging = int(e["fine_nudging"]) if "fine_nudging" in e else -1
    libera_da = ciclo + timedelta(minutes=max(fine_nudging, 0))
    valide = [ciclo + timedelta(hours=int(h)) for h in e["ore"]]
    indice = {t: k for k, t in enumerate(valide)}
    ids = [str(x) for x in e["stazioni"]]
    giorno_ore = set()
    for t_fine in finestre(GIORNO):
        giorno_ore.update(t_fine - timedelta(hours=h) for h in range(6))
    usati = set()
    for j, sid in enumerate(ids):
        o = oss.get(sid)
        if not o:
            continue
        dz = float(e["quota_modello"][j]) - quote.get(sid, float(e["quota_modello"][j]))
        for t, k in indice.items():
            if t not in giorno_ore:
                continue
            ore_prev = int(round((t - ciclo).total_seconds() / 3600))
            if ore_prev < 1 or t <= libera_da:
                continue                             # l'istante iniziale e le ore assimilate non sono previsione
            acc = acc_per_scad[scadenza(ore_prev)]
            usati.add(scadenza(ore_prev))
            ot = o.get("t2", {}).get(t)
            if ot is not None and -40 < ot < 50 and np.isfinite(e["t2"][k, j]):
                err = float(e["t2"][k, j]) + 0.0065 * dz - ot
                aggiungi(acc, "t2", err)
                aggiungi(acc, "t2_notte" if t.hour in NOTTE else "t2_giorno", err)
            otd = o.get("td2", {}).get(t)
            if otd is not None and np.isfinite(e["td2"][k, j]):
                aggiungi(acc, "td2", float(e["td2"][k, j]) - otd)
            ov = o.get("vento", {}).get(t)
            if ov is not None and 0 <= ov < 60 and np.isfinite(e["vento"][k, j]):
                vm = float(e["vento"][k, j])
                aggiungi(acc, "vento", vm - ov)
                c = classe_vento(vm)
                cl = acc["vento_classi"][c]
                cl[0] += 1; cl[1] += vm; cl[2] += ov
                aggiungi(acc, "vento_corr", vm * (corr_vento or {}).get(c, 1.0) - ov)
            op = o.get("mslp", {}).get(t)
            if op is not None and 950 < op < 1060 and np.isfinite(e["mslp"][k, j]):
                aggiungi(acc, "mslp", float(e["mslp"][k, j]) - op)
        # pioggia in 6 ore ai pluviometri
        op = o.get("pioggia", {})
        for t_fine in finestre(GIORNO):
            ore6 = [t_fine - timedelta(hours=h) for h in range(6)]
            if not all(t in op for t in ore6) or not all(t in indice for t in ore6):
                continue
            ore_prev = int(round((t_fine - ciclo).total_seconds() / 3600))
            if ore_prev < 6 or t_fine - timedelta(hours=6) < libera_da:
                continue
            prev = sum(float(e["pioggia"][indice[t], j]) for t in ore6)
            if not np.isfinite(prev):
                continue
            conta_pioggia(acc_per_scad[scadenza(ore_prev)], prev, sum(op[t] for t in ore6))
    # radar, sulla griglia ridotta, con tolleranza
    if radar_ore and radar_mod is not None:
        from scipy.ndimage import maximum_filter
        glat, glon = e["griglia_lat"].astype(float), e["griglia_lon"].astype(float)
        passo_km = float(np.hypot(np.diff(glat[:, 0]).mean() * 111.2, 0.0))
        raggio = max(1, int(round(TOLLERANZA_KM / max(passo_km, 0.5))))
        yy, xx = np.mgrid[-raggio:raggio + 1, -raggio:raggio + 1]
        disco = (yy ** 2 + xx ** 2) <= raggio ** 2
        for t_fine in finestre(GIORNO):
            ore6 = [t_fine - timedelta(hours=h) for h in range(6)]
            if not all(t in radar_ore for t in ore6) or not all(t in indice for t in ore6):
                continue
            ore_prev = int(round((t_fine - ciclo).total_seconds() / 3600))
            if ore_prev < 6 or t_fine - timedelta(hours=6) < libera_da:
                continue
            prev = sum(e["griglia_pioggia"][indice[t]].astype(float) for t in ore6)
            osservata = sum(radar_mod.campiona(radar_ore[t], glat, glon) for t in ore6)
            ok = np.isfinite(osservata) & np.isfinite(prev)
            for s in SOGLIE_RADAR:
                f, o = (prev >= s) & ok, (osservata >= s) & ok
                o_vic = maximum_filter(o, footprint=disco)
                f_vic = maximum_filter(f, footprint=disco)
                c = acc_per_scad[scadenza(ore_prev)]["radar"][str(s)]
                c[0] += int((f & o_vic).sum()); c[1] += int(f.sum()); c[2] += int((o & f_vic).sum()); c[3] += int(o.sum())
    for s in usati:
        acc_per_scad[s]["cicli"].append(str(e["ciclo"]))


def verifica_riferimento(rif, oss, ids, acc):
    for sid in ids:
        o, m = oss.get(sid), rif.get(sid)
        if not o or not m:
            continue
        for t, v in m["t2"].items():
            ot = o.get("t2", {}).get(t)
            if ot is not None and -40 < ot < 50 and t.date() == GIORNO:
                aggiungi(acc, "t2", v - ot)
                aggiungi(acc, "t2_notte" if t.hour in NOTTE else "t2_giorno", v - ot)
        for t, v in m["vento"].items():
            ov = o.get("vento", {}).get(t)
            if ov is not None and 0 <= ov < 60 and t.date() == GIORNO:
                aggiungi(acc, "vento", v - ov)
        op = o.get("pioggia", {})
        for t_fine in finestre(GIORNO):
            ore6 = [t_fine - timedelta(hours=h) for h in range(6)]
            if all(t in op for t in ore6) and all(t in m["pioggia"] for t in ore6):
                conta_pioggia(acc, sum(m["pioggia"][t] for t in ore6), sum(op[t] for t in ore6))


def indici(acc):
    """Indici leggibili da un insieme di somme."""
    out = {}
    for k in ("t2", "t2_giorno", "t2_notte", "td2", "vento", "vento_corr", "mslp"):
        n, s, s2 = acc.get(k, [0, 0.0, 0.0])
        out[k] = None if n == 0 else {"n": n, "medio": round(s / n, 2), "tipico": round((s2 / n) ** 0.5, 2)}
    out["pioggia"] = {}
    for s, (a, b, c, d) in acc["pioggia"].items():
        out["pioggia"][s] = {"casi": a + b + c + d, "piovuto": a + c, "previsto": a + b,
                             "presa": None if a + c == 0 else round(100 * a / (a + c)),          # piogge vere previste
                             "falsi": None if a + b == 0 else round(100 * b / (a + b)),          # previsioni senza pioggia vera
                             "csi": None if a + b + c == 0 else round(100 * a / (a + b + c))}
    out["radar"] = {}
    for s, (fo, ft, oo, ot) in acc["radar"].items():
        out["radar"][s] = {"prevista_giusta": None if ft == 0 else round(100 * fo / ft),
                           "osservata_presa": None if ot == 0 else round(100 * oo / ot), "celle_previste": ft, "celle_osservate": ot}
    return out


def somma(a, b):
    for k, v in b.items():
        if k == "cicli":
            a[k] = a.get(k, []) + v
        elif isinstance(v, dict):
            for s, lst in v.items():
                a[k][s] = [x + y for x, y in zip(a[k][s], lst)]
        else:
            a[k] = [x + y for x, y in zip(a[k], v)]
    return a


NOMI = {"ecmwf": "WRF con ECMWF", "icon2i": "WRF con ICON-2I", "rif-ecmwf": "ECMWF originale", "rif-icon2i": "ICON-2I originale"}


def pagella(archivio, oggi):
    giorni = sorted(glob.glob(os.path.join(archivio, "giorni", "*.json")))
    out = {"aggiornata": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "ultimo_giorno": oggi.isoformat(),
           "periodi": {}, "nomi": NOMI,
           "note": "Errori: medio = previsto meno osservato (positivo = modello troppo caldo/ventoso), tipico = scarto quadratico medio. "
                   "Pioggia ai pluviometri, in 6 ore: presa = piogge vere previste (%), falsi = previsioni senza pioggia vera (%). "
                   "Radar, in 6 ore, con 10 km di tolleranza: prevista giusta = pioggia prevista con pioggia vera vicina (%), "
                   "osservata presa = pioggia vera con pioggia prevista vicina (%). Riferimento: previsioni a breve dei modelli originali (Open-Meteo)."}
    for periodo in (7, 30):
        limite = (oggi - timedelta(days=periodo - 1)).isoformat()
        tot, nd = {}, 0
        for f in giorni:
            g = json.load(open(f))
            if g["giorno"] < limite or g["giorno"] > oggi.isoformat():
                continue
            nd += 1
            for chiave, per_scad in g["modelli"].items():
                for scad, acc in per_scad.items():
                    dst = tot.setdefault(chiave, {}).setdefault(scad, vuoto())
                    somma(dst, acc)
        out["periodi"][str(periodo)] = {"giorni": nd, "modelli": {k: {s: dict(indici(a), cicli=len(set(a["cicli"])))
                                                                        for s, a in v.items()} for k, v in tot.items()}}
    json.dump(out, open(os.path.join(archivio, "pagella.json"), "w"), ensure_ascii=False, indent=1)
    return out


def main():
    global GIORNO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--giorno", default=(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--estratti", default=None, help="cartella con gli estratti (per le prove); altrimenti dai rami risultati-*")
    ap.add_argument("--archivio", required=True, help="cartella del ramo verifica (giorni/ e pagella.json)")
    ap.add_argument("--senza-radar", action="store_true")
    a = ap.parse_args()
    GIORNO = datetime.strptime(a.giorno, "%Y-%m-%d").date()
    log(f"== Verifica del {GIORNO} (00-24 UTC)")

    st = json.load(open(os.path.join(QUI, "stazioni_verifica.json")))
    quote = {s["id"]: float(s["quota"]) for s in st["regione"] + st["aeronautica"] if s.get("quota") is not None}
    pos = {s["id"]: (s["lat"], s["lon"], float(s.get("quota") or 0)) for s in st["regione"] + st["aeronautica"]}

    estratti = carica_estratti(GIORNO, a.estratti)
    servono = {str(x) for lst in estratti.values() for e in lst for x in e["stazioni"]}
    if not servono:
        raise SystemExit("nessun estratto per questo giorno: niente da verificare")
    oss = osservazioni_regione(st["regione"], GIORNO, servono)
    for k, v in osservazioni_synop(st["aeronautica"], GIORNO).items():
        oss.setdefault(k, {}).update(v)
    radar_ore, radar_mod = ({}, None) if a.senza_radar else radar_giorno(GIORNO)
    rif = riferimento_openmeteo({k: pos[k] for k in servono if k in pos}, GIORNO)

    corr = correzioni_vento(a.archivio, GIORNO)
    risultato = {"giorno": GIORNO.isoformat(), "modelli": {}, "correzioni_vento": corr}
    for (z, p), lista in estratti.items():
        acc = {"g1": vuoto(), "g2": vuoto()}
        for e in lista:
            verifica_estratto(e, oss, quote, radar_ore, radar_mod, acc, corr.get(f"{z}-{p}"))
        if any(acc[s]["cicli"] for s in acc):
            risultato["modelli"][f"{z}-{p}"] = acc
    for z in ZONE:
        ids = {str(x) for (zz, _), lst in estratti.items() if zz == z for e in lst for x in e["stazioni"]}
        for p in PILOTI:
            acc = vuoto()
            verifica_riferimento(rif[p], oss, ids, acc)
            if acc["t2"][0]:
                risultato["modelli"][f"{z}-rif-{p}"] = {"breve": acc}

    os.makedirs(os.path.join(a.archivio, "giorni"), exist_ok=True)
    json.dump(risultato, open(os.path.join(a.archivio, "giorni", f"{GIORNO}.json"), "w"), ensure_ascii=False)
    for k, per_scad in risultato["modelli"].items():
        for s, acc in per_scad.items():
            i = indici(acc)
            t = i["t2"]
            pg, rd = i["pioggia"]["1.0"], i["radar"]["0.5"]
            log(f"  {k:22s} {s:5s} T2 {('%+.1f/%.1f' % (t['medio'], t['tipico'])) if t else '-':>10s} su {t['n'] if t else 0:4d} | "
                f"pioggia>=1mm presa {pg['presa']} falsi {pg['falsi']} ({pg['casi']} casi) | radar giusta {rd['prevista_giusta']} presa {rd['osservata_presa']}")
    pagella(a.archivio, GIORNO)
    # fattori per le mappe (se un giorno si decidera' di correggerle): ultimi 30 giorni, oggi compreso
    nuove = correzioni_vento(a.archivio, GIORNO, fino_a_incluso=True)
    json.dump({"giorno": GIORNO.isoformat(), "classi_m_s": CLASSI_VENTO[:-1], "fattori": nuove},
              open(os.path.join(a.archivio, "correzione_vento.json"), "w"), indent=1)
    log(f"pagella aggiornata; correzione del vento: {nuove}")


if __name__ == "__main__":
    main()
