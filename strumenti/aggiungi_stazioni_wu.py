#!/usr/bin/env python3
"""Aggiunge stazioni Weather Underground "a norma" alla verifica (lista "wu" di strumenti/stazioni_verifica.json).

  WU_API_KEY=... aggiungi_stazioni_wu.py IROMA123 ICASTE45 ...
  WU_API_KEY=... aggiungi_stazioni_wu.py --campione 150      una prova: 150 stazioni WU sparse sul Lazio (non controllate!)

Per ogni ID legge da WU posizione e quota (osservazione attuale) e li scrive nella lista. Da quel momento: gli estratti dei nuovi cicli
includono quei punti (estrai_verifica.py) e la verifica del mattino li confronta in indici a parte (t2_wu, td2_wu, vento_wu), senza
mescolarli con la rete regionale e i SYNOP. I cicli passati non hanno quei punti. Chi usa le WU per il nudging (opzione "wu") le ha gia'
viste il modello nelle prime ore: la verifica conta solo le ore dopo la fine del nudging.
"""
import json
import os
import sys

import requests

QUI = os.path.dirname(os.path.abspath(__file__))
URL = "https://api.weather.com/v2/pws/observations/current"


def campione(chiave, quante):
    """ID di stazioni WU sparse sul Lazio: la piu' vicina a ogni punto di una griglia di circa 20 km, poi le seguenti fino a `quante`.
    Sono stazioni qualsiasi, senza controllo di qualita': servono solo a una prova."""
    import numpy as np
    punti = [(la, lo) for la in np.arange(40.8, 42.95, 0.18) for lo in np.arange(11.3, 14.0, 0.24)]
    trovate, per_punto = [], []
    for la, lo in punti:
        r = requests.get("https://api.weather.com/v3/location/near", params={"geocode": f"{la:.4f},{lo:.4f}", "product": "pws",
                                                                              "format": "json", "apiKey": chiave}, timeout=30)
        try:
            loc = r.json()["location"]
            ids = [i for i in loc["stationId"] if i]
        except (KeyError, ValueError, TypeError):
            continue
        per_punto.append(ids)
        trovate += ids
    scelte, viste = [], set()
    for livello in range(10):
        for ids in per_punto:
            if livello < len(ids) and ids[livello] not in viste:
                viste.add(ids[livello]); scelte.append(ids[livello])
    print(len(punti), "punti,", len(viste), "stazioni diverse")
    return scelte[:quante]


def _chiave_wu(segreto):
    """Chiave WU da usare per le richieste: il segreto, o una trovata nella pagina pubblica di una stazione se WU lo rifiuta
    (strumenti/wu_chiave.py). Non solleva mai eccezioni: nel peggiore dei casi resta il segreto."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from wu_chiave import chiave_funzionante
        return chiave_funzionante(segreto)
    except Exception:
        return segreto


def main():
    chiave = os.environ.get("WU_API_KEY")
    if not chiave or len(sys.argv) < 2:
        raise SystemExit(__doc__)
    chiave = _chiave_wu(chiave)
    if sys.argv[1] == "--campione":
        sys.argv = [sys.argv[0]] + campione(chiave, int(sys.argv[2]))
    p = os.path.join(QUI, "stazioni_verifica.json")
    testo = open(p).read()
    st = json.loads(testo)
    gia = {s["id"] for s in st.get("wu", [])}
    for wid in sys.argv[1:]:
        wid = wid.strip().upper()
        if "WU_" + wid in gia:
            print(wid, "gia' presente")
            continue
        r = requests.get(URL, params={"stationId": wid, "format": "json", "units": "m", "numericPrecision": "decimal", "apiKey": chiave}, timeout=30)
        try:
            o = r.json()["observations"][0]
            elev = o.get("metric", {}).get("elev")
            st.setdefault("wu", []).append({"id": "WU_" + wid, "nome": wid, "lat": float(o["lat"]), "lon": float(o["lon"]),
                                            "quota": None if elev is None else float(elev), "fonte": "Weather Underground (a norma)"})
            print(wid, "aggiunta", o["lat"], o["lon"], elev)
        except (KeyError, IndexError, ValueError, TypeError):
            print(wid, "non trovata o senza dati (HTTP", r.status_code, ")")
    # si riscrive solo la sezione "wu", lasciando il resto del file com'e'
    i = testo.rindex('"wu"')
    nuovo = testo[:i] + '"wu": ' + json.dumps(st["wu"], ensure_ascii=False, indent=1) + "\n}\n"
    json.loads(nuovo)
    open(p, "w").write(nuovo)


if __name__ == "__main__":
    main()
